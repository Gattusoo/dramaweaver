"""多轨剪辑管线（timeline-editing，2026-11-24 组 6）。

三件事（design D5：复用 FFmpeg 渲染器，不重写）：
- import_composition：既有 VIDEO 合成产物一键导入为六轨工程初始态（自动上轨）
- validate_composition：导出前完整性校验（空引用/越界/已删资产阻断+明细）
- export_segments：工程 → FFmpeg 输入规格（轨参数化），由 worker 侧既有
  单一 filtergraph 一次编码管线消费（run_ffmpeg/_encode_with_fallback 等原语）

导入形状约定：合成 metadata.timeline（media_utils.build_timeline 产物）给出
片段起点/时长/shot_id；素材按镜头聚合——视频轨挂镜头 IMAGE（首帧静图）、
对白轨挂镜头 AUDIO、音乐轨挂 BGM、字幕轨挂手工文本条目（无 SRT asset 引用则空）。
"""
import logging

from ..models import (
    Asset, AssetStatus, AssetType, Clip, Composition, Shot, Track, TrackKind,
)

logger = logging.getLogger(__name__)


def import_composition(play, episode, source_asset, title=""):
    """自动合成产物导入为剪辑工程初始态。

    source_asset：既有 VIDEO 合成 Asset（READY）；返回 (composition, imported)：
    imported 为素材上轨计数（视频+对白+音乐片段数）。幂等：同一来源资产重复
    导入返回既有工程（import_source 唯一性）。
    """
    existing = Composition.objects.filter(
        play=play, episode=episode, import_source=source_asset.pk).first()
    if existing is not None:
        return existing, 0
    comp = Composition.objects.create(
        play=play, episode=episode, title=title or f"精修 · 第{episode.episode_number}集",
        import_source=source_asset.pk)
    # 六轨一次性铺满（空轨保留——时间线 UI 需要固定轨道面）
    tracks = {kind: Track.objects.create(composition=comp, kind=kind, order=i)
              for i, kind in enumerate(TrackKind.values)}
    timeline = (source_asset.metadata or {}).get("timeline") or []
    source_assets = set((source_asset.metadata or {}).get("source_assets") or [])
    imported = 0
    for entry in timeline:
        shot = Shot.objects.filter(pk=entry.get("shot_id")).first() if entry.get("shot_id") else None
        start = float(entry.get("start", 0))
        duration = max(0.0, float(entry.get("duration", 0)))
        if duration <= 0:
            continue
        if entry.get("role") != "body" or shot is None:
            continue  # 片头片尾轨外素材不进视频轨（保留在 Asset 体系可手工拖入）
        # 视频轨：镜头当前图片（同位置最高版本 READY）
        image = Asset.objects.filter(
            play=play, shot=shot, asset_type=AssetType.IMAGE,
            status=AssetStatus.READY).order_by("-version").first()
        if image is not None and image.pk in source_assets or image is not None:
            Clip.objects.create(
                track=tracks[TrackKind.VIDEO], asset=image,
                label=f"镜头{shot.shot_number}", start=start,
                in_point=0, out_point=duration, order=int(entry.get("index", 0)))
            imported += 1
        # 对白轨：镜头当前配音
        audio = Asset.objects.filter(
            play=play, shot=shot, asset_type=AssetType.AUDIO,
            status=AssetStatus.READY).order_by("-version").first()
        if audio is not None:
            Clip.objects.create(
                track=tracks[TrackKind.DIALOGUE], asset=audio,
                label=f"台词{shot.shot_number}", start=start,
                in_point=0, out_point=duration, order=int(entry.get("index", 0)))
            imported += 1
        # 字幕轨：对白文本手工条目（SRT 产物引用由前端按 SubtitleTrack 另行上轨）
        if shot.dialogue:
            Clip.objects.create(
                track=tracks[TrackKind.SUBTITLE],
                label=f"字幕{shot.shot_number}", start=start,
                in_point=0, out_point=duration, text=shot.dialogue,
                order=int(entry.get("index", 0)))
            imported += 1
    # 音乐轨：本集 BGM
    bgm = Asset.objects.filter(
        play=play, episode=episode, asset_type=AssetType.BGM,
        status=AssetStatus.READY).order_by("-version").first()
    if bgm is not None:
        Clip.objects.create(
            track=tracks[TrackKind.MUSIC], asset=bgm, label="BGM",
            start=0, in_point=0, out_point=0, order=0)  # out=0：导出按素材全长
        imported += 1
    return comp, imported


def validate_composition(comp):
    """导出前完整性校验。返回错误明细列表 []（空 = 可导出）。

    阻断项：空轨片段引用（非字幕轨无 asset）、入出点越界（超出素材实际时长）、
    引用资产被删除/非 READY（SRT 产物引用 SUPERSEDED 亦阻断）。
    素材实际时长来自 metadata.duration_seconds（导入来源）或生成本地探测——
    校验层不跑 ffprobe（快路径），metadata 缺失时跳过越界检查（导出侧兜底）。
    """
    problems = []
    clips = list(Clip.objects.filter(track__composition=comp)
                 .select_related("track", "asset").order_by("track__order", "order"))
    for clip in clips:
        where = f"{clip.track.get_kind_display()} #{clip.pk}"
        if clip.asset is None:
            if clip.track.kind == TrackKind.SUBTITLE and (clip.text or "").strip():
                continue
            problems.append({"clip_id": clip.pk, "track": clip.track.kind,
                             "code": "empty_reference", "detail": f"{where} 无素材引用"})
            continue
        if clip.asset.status not in (AssetStatus.READY,):
            problems.append({"clip_id": clip.pk, "track": clip.track.kind,
                             "code": "asset_unavailable",
                             "detail": f"{where} 引用素材 {clip.asset.pk} 状态 {clip.asset.status}"})
            continue
        if not clip.asset.artifact_url:
            problems.append({"clip_id": clip.pk, "track": clip.track.kind,
                             "code": "asset_unavailable", "detail": f"{where} 素材无文件"})
            continue
        # 越界：出点超出素材时长（入点 < 0 或 > 出点已在序列化器拒）
        meta_duration = float((clip.asset.metadata or {}).get("duration_seconds") or 0)
        if meta_duration > 0 and clip.out_point > meta_duration + 1e-6:
            problems.append({"clip_id": clip.pk, "track": clip.track.kind,
                             "code": "out_of_bounds",
                             "detail": f"{where} 出点 {clip.out_point:.2f}s 超素材时长 {meta_duration:.2f}s"})
        if clip.out_point == 0 and meta_duration > 0 and clip.track.kind != TrackKind.VIDEO:
            # 约定 out=0 表示按素材全长（导入 BGM 形态），不越界
            pass
    return problems


def export_input_specs(comp, tmp):
    """工程 → FFmpeg 渲染输入规格（轨参数化，design D5：参数进既有渲染原语）。

    返回 dict：
      segments：视频轨片段的输入规格列表（video kind input_args + filter）——
                复用 _compose 的 segment 形状（input_args/filter/duration），
                以 -ss/-t 裁剪实现入出点；
      voice_map：对白轨 {输入序号: 本地路径}；
      bgm：音乐轨素材本地路径或 None；
      subtitle_entries：字幕轨条目 [{start, end, text}]；
      total：时间线总长（视频轨最长片段终点）。

    下载走 _download（SSRF/跨剧守卫既有）；调用方在 worker 内（tasks._export_composition）。
    """
    from ..tasks import _download, _cover  # 复用既有原语（不重写渲染器）

    tracks = {t.kind: t for t in comp.tracks.all()}
    segments, voice_map, subtitle_entries = [], {}, []
    bgm_path = None
    next_input = 0

    video_track = tracks.get(TrackKind.VIDEO)
    total = 0.0
    if video_track is not None:
        for clip in video_track.clips.order_by("order"):
            if clip.asset is None or not clip.asset.artifact_url:
                continue  # 校验层已阻断；此处防御性跳过
            path = _download(clip.asset.artifact_url, tmp / f"v_{clip.pk}.mp4",
                              play_pk=clip.asset.play_id)
            duration = clip.duration
            if duration <= 0:
                duration = float((clip.asset.metadata or {}).get("duration_seconds") or 3)
            segments.append({
                "kind": "video", "role": "body", "path": path, "duration": duration,
                "input_args": ["-ss", f"{clip.in_point:.3f}", "-t", f"{duration:.3f}",
                               "-i", str(path)],
                "filter": _cover(None),
            })
            total = max(total, clip.start + duration)
            next_input += 1

    dialogue_track = tracks.get(TrackKind.DIALOGUE)
    if dialogue_track is not None:
        for clip in dialogue_track.clips.order_by("order"):
            if clip.asset is None or not clip.asset.artifact_url:
                continue
            path = _download(clip.asset.artifact_url, tmp / f"a_{clip.pk}.mp3",
                             play_pk=clip.asset.play_id)
            voice_map[next_input] = path  # 输入序号与视频轨错开由调用方重排
            next_input += 1

    music_track = tracks.get(TrackKind.MUSIC)
    if music_track is not None:
        first = music_track.clips.order_by("order").first()
        if first is not None and first.asset is not None and first.asset.artifact_url:
            bgm_path = _download(first.asset.artifact_url, tmp / "bgm.mp3",
                                 play_pk=first.asset.play_id)

    subtitle_track = tracks.get(TrackKind.SUBTITLE)
    if subtitle_track is not None:
        for clip in subtitle_track.clips.order_by("order"):
            text = (clip.text or "").strip()
            if not text:
                continue
            if clip.asset is not None and clip.asset.artifact_url:
                # SRT 产物引用：下载解析（字幕内容以产物为准）
                srt_path = _download(clip.asset.artifact_url, tmp / f"s_{clip.pk}.srt",
                                     play_pk=clip.asset.play_id)
                try:
                    text = srt_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
            subtitle_entries.append({"start": clip.start,
                                     "end": clip.start + max(clip.duration, 0.5),
                                     "text": text})
    if total <= 0:
        raise ValueError("视频轨无可导出片段")
    return {"segments": segments, "voice_map": voice_map, "bgm": bgm_path,
            "subtitle_entries": subtitle_entries, "total": total}
