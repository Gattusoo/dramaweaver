"""生产链路 Celery 任务（Step 1-7）。

每个任务接收 TaskRecord id，负责：
- 更新 TaskRecord 生命周期（PENDING -> RUNNING -> SUCCESS/FAILED）
- 通过 AI Gateway 调用对应适配器
- 产出写入 Asset（READY），触发 Dispatcher 派发下游
"""
import json
import hashlib
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
from celery import shared_task
from django.conf import settings
from django.utils import timezone

from dvadmin.utils import media as media_utils
from .models import (
    Asset, AssetDependency, AssetLibrary, AssetStatus, AssetType, Character, Episode,
    EpisodeStatus, MemberStatus, NovelChapter, PlayStatus, PlayMember, RelationType,
    Scene, Shot, TaskRecord, TaskStatus,
)
from .pipeline.gen_params import resolve_gen_params
from .pipeline.generation import fingerprint_of, normalize_spec, upstream_refs
from .pipeline.local_backend import PAID_CHECKPOINT, _backend
from apps.ai_gateway.gateway import AIGatewayError
from apps.distribution.models import Publication, SubtitleSourceType, SubtitleStatus, SubtitleTrack

logger = logging.getLogger(__name__)

# ─── 任务韧性（瞬时故障自动重试 + Celery 超时兜底）─────────────────────────

# 瞬时故障：网关候选链耗尽（含全链 503/超时）/ 网络传输错误 / 连接与超时错误。
# 请求被拒类（4xx 已在网关直接降级）、参数/校验错误不重试（同样错误会再失败）。
_TRANSIENT_ERRORS = (AIGatewayError, httpx.TimeoutException, httpx.TransportError,
                     ConnectionError, TimeoutError)

# @shared_task 统一韧性策略：
# - 瞬时故障指数退避自动重试（_run 镜像该判定，重试中不落 FAILED，见 _retry_pending）
# - soft/hard time_limit 兜底杀 hung 任务：ffmpeg/i2v 内部上限 1800s 优雅触发在先，
#   Celery 仅作最后手段（soft 1800 可被 _run 捕获落 FAILED；hard 1900 强杀释放 worker）
#
# 计费安全：IMAGE / VIDEO / TTS 服务端受理即计费，整任务重跑会重复付费生成
# （网关层的非幂等保护挡不住任务层重试）。因此生成类任务不自动重试——只在
# 确定性失败上落 FAILED，交由用户/重做入口显式重跑；仅纯文本 LLM 任务可重试。
_TASK_RETRY = dict(
    autoretry_for=_TRANSIENT_ERRORS,
    retry_backoff=True, retry_backoff_max=300, retry_jitter=True,
    max_retries=3,
    soft_time_limit=1800, time_limit=1900,
)

# 付费生成类任务：不自动重试（autoretry_for 置空），超时策略保留。
_TASK_RETRY_PAID = dict(_TASK_RETRY, autoretry_for=())


def _retry_pending(exc) -> bool:
    """镜像 Celery autoretry 的重试判定（celery/app/autoretry.py + task.retry）：
    异常在 autoretry_for 内、非直接调用、名额未耗尽。

    Celery 的判定是先自增再比较：`retries = request.retries + 1`，
    `if retries > max_retries: raise`（放弃重试）。因此本次仍会重试的条件是
    `request.retries + 1 <= max_retries`，即 `request.retries < max_retries`。
    多算一位会让“名额耗尽的那次失败”被误判为待重试 → TaskRecord 永不落 FAILED。

    True 时 Celery 会重新入队本次任务——TaskRecord 保持 RUNNING 不落 FAILED，
    避免重试中的状态闪烁与误判卡死；重试耗尽后那次失败由 _mark_failed 落终态。"""
    from celery import current_task
    task = current_task
    if task is None:
        # 本地 worker 直调（无 Celery 请求上下文）：瞬时异常同样视为待重试
        # （保持 RUNNING 不落 FAILED，与 shared 契约一致——任务 6.4）；
        # 名额是否耗尽由 run_local_worker 按注册表策略判定并显式落 FAILED。
        if _backend() == "local" and isinstance(exc, _TRANSIENT_ERRORS):
            return True
        return False
    retryable = tuple(getattr(task, "autoretry_for", ()) or ())
    if not retryable or not isinstance(exc, retryable):
        return False
    req = getattr(task, "request", None)
    if req is None or getattr(req, "called_directly", False):
        return False  # 直接调用（单测/管理脚本）不重试，Celery 会原样重抛
    try:
        return int(getattr(req, "retries", 0) or 0) < int(getattr(task, "max_retries", 3))
    except (TypeError, ValueError):
        return False


# ─── TaskRecord 生命周期辅助 ──────────────────────────────────────────────

def _mark_running(record_id):
    """置为 RUNNING；已被取消的记录不复活，返回 None。"""
    now = timezone.now()
    updated = (TaskRecord.objects.filter(pk=record_id)
               .exclude(status=TaskStatus.CANCELLED)
               .update(status=TaskStatus.RUNNING, started_at=now))
    if not updated:
        return None
    return TaskRecord.objects.get(pk=record_id)


def _finish(record, **fields):
    """终态写入不覆盖用户的取消（条件更新，避免 read-modify-write 竞态）。"""
    fields["completed_at"] = timezone.now()
    updated = (TaskRecord.objects.filter(pk=record.pk)
               .exclude(status=TaskStatus.CANCELLED).update(**fields))
    for k, v in fields.items():
        setattr(record, k, v)
    if not updated:
        record.status = TaskStatus.CANCELLED
    # 计费对账（billing 变更接线）：成功结算差额 / 失败取消整额退回（幂等，
    # 无 hold 时 no-op）；对账失败只记日志，不影响终态
    if record.play_id and (record.input_summary or {}).get("billing_hold"):
        from .pipeline.dispatcher import _billing_settle
        try:
            _billing_settle(record)
        except Exception:  # noqa: BLE001
            logger.exception("TaskRecord#%s 计费对账异常", record.pk)
    # 工作台 Stage B1（任务 4.2）：任务终态只标脏清单，延迟到保存/导出时刷新
    try:
        from .workspace.manifest import mark_manifest_dirty
        if record.play_id:
            mark_manifest_dirty(record.play_id)
    except Exception:
        logger.debug("清单标脏失败 task=%s", record.pk, exc_info=True)
    return bool(updated)


def _mark_success(record, **output):
    _finish(record, status=TaskStatus.SUCCESS, output_summary=output or record.output_summary)


def _mark_failed(record, error):
    if _finish(record, status=TaskStatus.FAILED, error_message=str(error)[:2000]):
        logger.exception("TaskRecord#%s 失败: %s", record.pk, error)


def _mark_cancelled(record):
    TaskRecord.objects.filter(pk=record.pk).update(status=TaskStatus.CANCELLED,
                                                   completed_at=timezone.now())
    record.status = TaskStatus.CANCELLED
    _billing_settle_safe(record)


def _billing_settle_safe(record):
    """取消路径的对账入口（_finish 已内置；这里覆盖不走 _finish 的取消）。"""
    if not record.play_id or not (record.input_summary or {}).get("billing_hold"):
        return
    from .pipeline.dispatcher import _billing_settle
    try:
        _billing_settle(record)
    except Exception:  # noqa: BLE001
        logger.exception("TaskRecord#%s 计费对账异常（取消）", record.pk)


def _run(record_id, work):
    """包装任务生命周期。取消（开始前或渲染中）记为 CANCELLED，不视为失败；
    瞬时失败且 Celery 将自动重试时保持 RUNNING（不落 FAILED，避免重试中状态闪烁）。"""
    record = _mark_running(record_id)
    if record is None:
        logger.info("TaskRecord#%s 已取消，跳过执行", record_id)
        return None
    try:
        result = work(record)
    except RenderCancelled:
        _mark_cancelled(record)
        logger.info("TaskRecord#%s 渲染被取消", record_id)
        return None
    except Exception as exc:  # noqa: BLE001
        if _retry_pending(exc):
            logger.warning("TaskRecord#%s 瞬时失败，待 Celery 自动重试: %s", record_id, exc)
            raise
        _mark_failed(record, exc)
        raise
    _mark_success(record, **(result or {}))
    return result


def _gateway(play):
    from apps.ai_gateway.gateway import AIGateway
    gw = AIGateway()
    # 工作台 Stage C1（design D2）：供应商调用前置位付费检查点——本地后端下
    # 该检查点决定崩溃后的恢复分类（paid_request_started → RECOVERY_REQUIRED）。
    if _backend() != "local":
        return gw
    return _PaidCheckpointGateway(gw)


class _PaidCheckpointGateway:
    """AIGateway 包装：call/chat/vision 携带 task_record 时置位付费检查点。"""

    def __init__(self, inner):
        self._inner = inner

    def _mark(self, task_record):
        if task_record is None:
            return
        try:
            from .models import LocalTaskExecution
            LocalTaskExecution.objects.filter(record_id=task_record.pk).update(
                last_checkpoint=PAID_CHECKPOINT)
        except Exception:  # noqa: BLE001 — 检查点失败不阻断生成（shared 无执行行）
            pass

    def call(self, provider_type, agent_config, *, play=None, task_record=None, **payload):
        self._mark(task_record)
        return self._inner.call(provider_type, agent_config, play=play,
                                task_record=task_record, **payload)

    def chat(self, *args, **kwargs):
        self._mark(kwargs.get("task_record"))
        return self._inner.chat(*args, **kwargs)

    def vision(self, *args, **kwargs):
        self._mark(kwargs.get("task_record"))
        return self._inner.vision(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _config(play):
    return play.agent_config


def _link_upstream(asset, upstream_assets):
    """批量落依赖边（upstream → asset），按 relation_type 推断（Stage A2 任务 2.4）。"""
    edges = []
    for up in upstream_assets:
        if up is None or up.pk == asset.pk:
            continue
        edges.append(AssetDependency(
            upstream=up, downstream=asset,
            relation_type=_infer_relation(up, asset),
        ))
    if edges:
        AssetDependency.objects.bulk_create(edges, ignore_conflicts=True)


def _infer_relation(up, down):
    """上游/下游类型 → RelationType（缺失时回 SCRIPT_TO_STORYBOARD 最弱语义）。"""
    pair = (up.asset_type, down.asset_type)
    mapping = {
        ("STORYBOARD", "IMAGE"): RelationType.STORYBOARD_TO_IMAGE,
        ("CHARACTER_REF", "IMAGE"): RelationType.CHARACTER_REF_TO_IMAGE,
        ("STORYBOARD", "AUDIO"): RelationType.DIALOGUE_TO_AUDIO,
        ("EPISODE_SCRIPT", "AUDIO"): RelationType.DIALOGUE_TO_AUDIO,
        ("EPISODE_SCRIPT", "STORYBOARD"): RelationType.SCRIPT_TO_STORYBOARD,
    }
    return mapping.get(pair, RelationType.SCRIPT_TO_STORYBOARD)


def _shot_upstream_assets(shot, include_script=False, include_char_ref=True):
    """镜头生成任务的上游依赖资产（Stage A2 任务 2.4）。

    IMAGE：本场分镜条目（STORYBOARD）+ 对白角色定妆照（CHARACTER_REF）；
    AUDIO：本场分镜条目 + 该集剧本（对白文本上游）；参考图与音频无关
    （include_char_ref=False）——否则改定妆照会误失效对白配音。
    现有 VIDEO 依赖（compose 的 IMAGE/AUDIO→VIDEO，_compose 落 upstream）不变。
    """
    ups = list(Asset.objects.filter(
        play=shot.scene.episode.play, shot=shot,
        asset_type=AssetType.STORYBOARD, status=AssetStatus.READY,
    ))
    if include_char_ref:
        ch = getattr(shot, "dialogue_character", None)
        ref = getattr(ch, "reference_image", None) if ch else None
        if ref is not None and ref.status == AssetStatus.READY:
            ups.append(ref)
    if include_script:
        eps = Asset.objects.filter(
            play=shot.scene.episode.play, episode=shot.scene.episode,
            asset_type=AssetType.EPISODE_SCRIPT, status=AssetStatus.READY,
        ).order_by("-version").first()
        if eps is not None:
            ups.append(eps)
    return ups


def _maybe_vision_review(play, asset, shot=None):
    """Phase 2.3：图片生成后自动质检（开关门控 + 失败静默）。

    低质量标记写入 metadata.quality（needs_regeneration / issues / suggestions），
    不直接触发重生成——重新生成会产生费用，仍走提案/用户确认路径。
    """
    from django.conf import settings
    if not (getattr(settings, "ENABLE_MULTIMODAL", False)
            and getattr(settings, "ENABLE_VISION_UNDERSTANDING", False)):
        return None
    try:
        from apps.creation.assistant.models import AssistantSession
        from apps.creation.assistant.vision import VisionUnavailable, review_asset
        session = AssistantSession.objects.filter(play=play).order_by("-updated_at").first()
        if session is None:
            return None
        expected = (shot.visual_prompt if shot is not None else "") or ""
        verdict = review_asset(session, asset, expected=expected)
        asset.metadata = {**(asset.metadata or {}), "quality": verdict}
        asset.save(update_fields=["metadata"])
        return verdict
    except VisionUnavailable as exc:
        logger.info("视觉质检跳过 asset=%s: %s", getattr(asset, "pk", None), exc)
        return None
    except Exception:  # noqa: BLE001 — 质检失败绝不影响生成任务
        logger.exception("视觉质检失败 asset=%s", getattr(asset, "pk", None))
        return None


def _save_image_result(result, filename):
    """从适配器结果保存图片，返回 artifact_url。保存字节前校验可解码（D12）。"""
    from dvadmin.utils.storage import save_artifact
    if result.get("url"):
        return result["url"]
    if result.get("image_bytes"):
        result["_media"] = _validate_media_bytes(result["image_bytes"], "image")
        return save_artifact(result["image_bytes"], filename, "images")
    # 空结果（适配器 200 空 data / 错误信封）：不能落一条 READY 但无 URL 的资产，
    # 否则素材"齐备"判定通过、合成时静默跳过该镜头，成片缺画面却仍被置为完成。
    raise ValueError("图片适配器未返回可用图片")


def _store_generated_bytes(play, data, asset_type):
    """生成的本地字节 → (storage_key, artifact_url, content_sha256)（任务 2.6）。

    S3 后端（shared 云部署）走 save_artifact（对象存储，无本地 storage_key）；
    local 磁盘后端用项目 UUID + 内容寻址路径（projects/<uuid>/assets/sha256/..），
    同项目同内容不互相覆盖、重名安全，旧 URL（/media/images/... 等扁平路径）
    的历史资产不受影响（不回填）。
    """
    from django.conf import settings
    if getattr(settings, "STORAGE_BACKEND", "local") == "s3":
        from dvadmin.utils.storage import save_artifact
        ext = {"IMAGE": "png", "AUDIO": "mp3", "VIDEO": "mp4"}.get(str(asset_type), "bin")
        url = save_artifact(data, f"{play.uuid}_{asset_type}.{ext}", "generated")
        return "", url, ""
    from .workspace.storage import save_asset_bytes
    return save_asset_bytes(play, data, asset_type)


def _validate_media_bytes(data, kind):
    """入库校验（D12）：ffprobe 探测字节；音频需有音轨且时长>0，图像需有画面。失败抛 ValueError。"""
    label = "音频" if kind == "audio" else "图像"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.bin"
        path.write_bytes(data)
        try:
            info = _probe_media(path)
        except ValueError as exc:
            raise ValueError(f"生成的{label}无法解码") from exc
    if kind == "audio" and not (info.get("has_audio") and info.get("duration", 0) > 0):
        raise ValueError(f"生成的{label}无有效音轨")
    if kind == "image" and not (info.get("has_video") and info.get("width", 0) > 0):
        raise ValueError(f"生成的{label}无有效画面")
    return info


def _find_balanced_json(text, start):
    """从 start 处（'{' 或 '['）取到配对的闭合括号（共用实现见
    apps.creation.assistant.llm_text.find_balanced_json，phase1 修复 6.1 抽出）。"""
    from apps.creation.assistant.llm_text import find_balanced_json
    return find_balanced_json(text, start)


def _parse_json(text):
    # 共用实现：apps.creation.assistant.llm_text.extract_json
    # （括号配平扫描 + 起点重试，兼容 ```json 围栏与前后说明文字。）
    from apps.creation.assistant.llm_text import extract_json
    return extract_json(text)


# ─── Step 1 大纲 ───────────────────────────────────────────────────────────

@shared_task(name="shortplay.tasks.generate_outline", **_TASK_RETRY)
def generate_outline(record_id):
    def work(record):
        play = record.play
        config = _config(play)
        templates = (config.prompt_templates or {}) if config else {}
        system = templates.get(
            "outline_system",
            "你是一位资深短剧编剧，请根据要求输出分集大纲，格式为 JSON。",
        )
        prompt = (
            f"主题：{play.title}\n类型：{play.genre}\n总集数：{play.total_episodes}\n"
            f"剧情简介：{play.synopsis}\n"
            f"请生成每集的标题、剧情梗概、主要冲突，以 JSON 返回 {{\"episodes\": [...]}}。"
        )
        result = _gateway(play).call(
            "LLM", config, play=play, task_record=record, prompt=prompt, system_prompt=system,
        )
        asset = Asset.objects.create(
            play=play, asset_type=AssetType.OUTLINE, version=1, status=AssetStatus.READY,
            metadata={"text": result["text"], "usage": result.get("usage", {})},
            ai_model=result.get("model", ""), cost=result.get("cost", 0),
        )
        return {"asset_id": asset.pk, "text_length": len(result["text"])}
    return _run(record_id, work)


# ─── Step 2 分集剧本 ───────────────────────────────────────────────────────

@shared_task(name="shortplay.tasks.generate_episode_script", **_TASK_RETRY)
def generate_episode_script(record_id):
    def work(record):
        episode = record.episode
        play = episode.play
        config = _config(play)
        outline = Asset.objects.filter(
            play=play, asset_type=AssetType.OUTLINE, status=AssetStatus.READY,
        ).order_by("-version").first()
        outline_text = (outline.metadata or {}).get("text", "") if outline else ""
        characters = "\n".join(
            f"- {c.name}（{c.role}）：{c.description}" for c in play.characters.all()
        )
        templates = (config.prompt_templates or {}) if config else {}
        system = templates.get(
            "episode_script_system",
            "你是短剧编剧，输出单集剧本，格式为 JSON {\"scenes\": [...]}。",
        )
        prompt = (
            f"短剧：{play.title}\n大纲：{outline_text}\n角色：\n{characters}\n"
            f"请生成第 {episode.episode_number} 集剧本，包含场景列表，"
            f"每个场景含 scene_number, location, time_of_day, description, mood。"
        )
        result = _gateway(play).call(
            "LLM", config, play=play, task_record=record, episode=episode,
            prompt=prompt, system_prompt=system, response_format={"type": "json_object"},
        )
        scenes = _parse_json(result["text"])
        scene_list = scenes.get("scenes", []) if isinstance(scenes, dict) else scenes
        if not isinstance(scene_list, list):
            scene_list = [{"scene_number": 1, "description": result["text"]}]
        if not scene_list:
            # LLM 返回 {"scenes": []} 或只含 error 字段：这是解析成功但内容为空的
            # 失败生成，绝不能当成“本集没有场景”去覆盖——下面的清理会删光既有场景
            # （级联删镜头、素材外键置空）且任务仍标成功，用户数据不可恢复。
            raise ValueError("剧本生成返回空场景列表，已保留原有剧本")
        created = []
        for i, sc in enumerate(scene_list, start=1):
            # update_or_create：重做/驳回后重跑时用新内容覆盖旧场景
            scene, _ = Scene.objects.update_or_create(
                episode=episode, scene_number=sc.get("scene_number", i),
                defaults={
                    "location": sc.get("location", ""),
                    "time_of_day": sc.get("time_of_day", "DAY"),
                    "description": sc.get("description", ""),
                    "mood": sc.get("mood", ""),
                },
            )
            created.append(scene.pk)
        # 新剧本变短时移除多余旧场景，避免残留场景继续被合成
        Scene.objects.filter(episode=episode).exclude(pk__in=created).delete()
        asset = Asset.objects.create(
            play=play, episode=episode, asset_type=AssetType.EPISODE_SCRIPT,
            status=AssetStatus.READY, metadata={"text": result["text"], "scenes": created},
            ai_model=result.get("model", ""), cost=result.get("cost", 0),
        )
        return {"asset_id": asset.pk, "scene_ids": created}
    return _run(record_id, work)


# ─── Step 3 角色定妆照 ─────────────────────────────────────────────────────

@shared_task(name="shortplay.tasks.generate_character_ref", **_TASK_RETRY_PAID)
def generate_character_ref(record_id):
    def work(record):
        play = record.play
        character_id = (record.input_summary or {}).get("character")
        character = Character.objects.get(pk=character_id) if character_id else None
        if character is None:
            character = play.characters.first()
        if character is None:
            return {"skipped": True, "reason": "no_character"}
        config = _config(play)
        prompt = character.appearance_prompt or f"{character.name} 定妆照"
        # 定妆照跟随 Play 有效比例（同一剧内图片比例一致；design D4 参数解析层）
        ref_resolved = resolve_gen_params(play, config)
        ref_payload = ({"aspect_ratio": ref_resolved["effective"]["aspect_ratio"]}
                       if ref_resolved["effective"].get("aspect_ratio") else {})
        result = _gateway(play).call(
            "IMAGE", config, play=play, task_record=record,
            prompt=prompt, reference_images=[],
            **ref_payload,
        )
        url = _save_image_result(result, f"play{play.pk}_char_{character.pk}.png")
        asset = Asset.objects.create(
            play=play, character=character, asset_type=AssetType.CHARACTER_REF,
            status=AssetStatus.READY, artifact_url=url,
            metadata={"usage": result.get("usage", {})}, cost=result.get("cost", 0),
        )
        character.reference_image = asset
        character.save(update_fields=["reference_image"])
        return {"asset_id": asset.pk, "url": url}
    return _run(record_id, work)


# ─── Step 4 分镜拆解 ───────────────────────────────────────────────────────

@shared_task(name="shortplay.tasks.generate_storyboard", **_TASK_RETRY)
def generate_storyboard(record_id):
    def work(record):
        scene = record.scene
        episode = scene.episode
        play = episode.play
        config = _config(play)
        templates = (config.prompt_templates or {}) if config else {}
        system = templates.get(
            "storyboard_system",
            "你是分镜师，输出镜头列表，格式为 JSON {\"shots\": [...]}。",
        )
        prompt = (
            f"场景 {scene.scene_number}：{scene.description}\n情绪：{scene.mood}\n"
            f"请拆解为分镜，每个镜头含 shot_number, shot_type, visual_prompt, "
            f"dialogue, action_note, emotion_tag, duration_estimate。"
        )
        result = _gateway(play).call(
            "LLM", config, play=play, task_record=record, episode=episode, scene=scene,
            prompt=prompt, system_prompt=system, response_format={"type": "json_object"},
        )
        parsed = _parse_json(result["text"])
        shots = parsed.get("shots", []) if isinstance(parsed, dict) else parsed
        if not isinstance(shots, list):
            shots = [{"shot_number": 1, "visual_prompt": result["text"]}]
        if not shots:
            # 同剧本任务：空镜头列表会让下面的清理删光本场既有镜头，直接判失败保留原数据
            raise ValueError("分镜生成返回空镜头列表，已保留原有分镜")
        shot_ids = []
        for i, sh in enumerate(shots, start=1):
            number = sh.get("shot_number", i)
            fields = {
                "shot_type": sh.get("shot_type", "MEDIUM"),
                "visual_prompt": sh.get("visual_prompt", ""),
                "dialogue": sh.get("dialogue", ""),
                "action_note": sh.get("action_note", ""),
                "emotion_tag": sh.get("emotion_tag", ""),
                "duration_estimate": sh.get("duration_estimate", 0) or 0,
            }
            old = Shot.objects.filter(scene=scene, shot_number=number).first()
            # update_or_create：重做后覆盖旧镜头内容，避免沿用旧画面/时长
            shot, _ = Shot.objects.update_or_create(scene=scene, shot_number=number, defaults=fields)
            if old is not None:
                # 画面/对白变了：旧图片/配音作废（级联失效成片），dispatch_image_and_tts 才会重新生成，
                # 否则会出现"字幕是新台词、画面和配音还是旧的"
                stale = []
                if old.visual_prompt != fields["visual_prompt"]:
                    stale.append(AssetType.IMAGE)
                if old.dialogue != fields["dialogue"]:
                    stale.append(AssetType.AUDIO)
                    # 对白变了：该集自动字幕（AUTO）过时，标废弃以便合成前重新生成；
                    # SubtitleTrack 非 Asset 无 supersede 链，需在此显式作废。
                    SubtitleTrack.objects.filter(
                        episode=episode, language="zh-CN",
                        source_type=SubtitleSourceType.AUTO, status=SubtitleStatus.READY,
                    ).update(status=SubtitleStatus.SUPERSEDED)
                for a in Asset.objects.filter(shot=shot, asset_type__in=stale, status=AssetStatus.READY):
                    a.mark_superseded_recursive()
            # 每个 Shot 一个 STORYBOARD Asset；重跑时先清理旧分镜资产，避免重复堆积
            Asset.objects.filter(
                play=play, episode=episode, scene=scene, shot=shot,
                asset_type=AssetType.STORYBOARD, status=AssetStatus.READY,
            ).delete()
            Asset.objects.create(
                play=play, episode=episode, scene=scene, shot=shot,
                asset_type=AssetType.STORYBOARD, status=AssetStatus.READY,
                metadata={"text": json.dumps(sh, ensure_ascii=False)},
            )
            shot_ids.append(shot.pk)
        # 新分镜镜头数变少时删除多余旧镜头，否则它们的旧画面/配音仍会被合成进成片
        for extra in scene.shots.exclude(pk__in=shot_ids):
            for a in Asset.objects.filter(shot=extra).exclude(status=AssetStatus.SUPERSEDED):
                a.mark_superseded_recursive()
            extra.delete()
        return {"shot_ids": shot_ids}
    return _run(record_id, work)


# ─── Step 5 素材生成 ───────────────────────────────────────────────────────

def _reusable_asset(asset_type, shot, spec_fingerprint, *, force=False):
    """指纹复用查找（工作台 Stage A2 任务 2.5，design D4）。

    条件：同目标位置（play/shot/类型）、input_fingerprint 相同、READY、
    本地文件存在或远程 URL 可用（artifact_url 非空）。显式 force/redo 不复用
    （须创建新版本）；空指纹（历史资产/规格缺失）不算命中——保守重建。
    """
    if force or not spec_fingerprint:
        return None
    return Asset.objects.filter(
        play=shot.scene.episode.play, shot=shot, asset_type=asset_type,
        status=AssetStatus.READY, input_fingerprint=spec_fingerprint,
    ).exclude(artifact_url="").order_by("-version").first()


def _mark_reused(record, asset, reason="fingerprint_match"):
    """复用不重复落新 Asset：任务成功、输出指向复用资产并注明原因。"""
    return {
        "asset_id": asset.pk, "reused": True, "reason": reason,
        "artifact_url": asset.artifact_url,
    }


def _content_digest(data) -> str:
    """生成内容 SHA-256（hex，64 字符）——内容寻址存储键与文件摘要校验的基础。"""
    if data is None:
        return ""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


@shared_task(name="shortplay.tasks.generate_image", **_TASK_RETRY_PAID)
def generate_image(record_id):
    def work(record):
        shot = record.shot
        play = shot.scene.episode.play
        config = _config(play)
        # 资产预览生成（主计划 3.5.3.5 / design D1-D3）：低分辨率草稿图，
        # 产出行不挂 shot/character FK（隔离版本链/主图指针/合成取图）。
        if (record.input_summary or {}).get("preview"):
            return _generate_image_preview(record, shot, play, config)
        # 有效创作参数（任务 1.4）：task > Play.gen_params > AgentConfig.style_params；
        # aspect_ratio 传入适配器参数；style 已在 _style() 层映射（见 _effective_style）。
        resolved = resolve_gen_params(play, config)
        payload_extra = {}
        if resolved["effective"].get("aspect_ratio"):
            payload_extra["aspect_ratio"] = resolved["effective"]["aspect_ratio"]
        # 生成规格 + 指纹（Stage A2 任务 2.2/2.5）：模型/参数/上游依赖（分镜+定妆照）
        # 定格为规格；同指纹 READY 资产直接复用，不重复调用付费模型。
        image_model = getattr(config, "image_model", None)
        spec = normalize_spec(
            target_type=AssetType.IMAGE,
            direct_content={"prompt": shot.visual_prompt or ""},
            model=image_model,
            effective_params=resolved["effective"],
            upstream=upstream_refs(_shot_upstream_assets(shot)),
        )
        fp = fingerprint_of(spec)
        force = bool((record.input_summary or {}).get("force")
                     or (record.input_summary or {}).get("redo"))
        reused = _reusable_asset(AssetType.IMAGE, shot, fp, force=force)
        if reused is not None:
            return _mark_reused(record, reused)
        ref_url = ""
        if shot.dialogue_character and shot.dialogue_character.reference_image:
            ref_url = shot.dialogue_character.reference_image.artifact_url
        result = _gateway(play).call(
            "IMAGE", config, play=play, task_record=record,
            shot=shot, prompt=shot.visual_prompt,
            reference_images=[ref_url] if ref_url else [],
            **payload_extra,
        )
        # 远程 URL 适配器：artifact_url 原样（供应商托管）；本地字节：内容寻址存储
        storage_key, url, content_sha = "", result.get("url", ""), ""
        if not url:
            url = _save_image_result(result, f"play{play.pk}_shot_{shot.pk}.png")
        if result.get("image_bytes"):
            storage_key, url, content_sha = _store_generated_bytes(
                play, result["image_bytes"], AssetType.IMAGE)
        metadata = {"usage": result.get("usage", {}), "media_validated": "_media" in result}
        if "_media" in result:
            metadata["media"] = {k: result["_media"][k] for k in ("width", "height", "vcodec")}
        # 有效参数回显（spec: 记录最终有效参数；unsupported 明示保留字段不静默）
        if resolved["effective"]:
            metadata["gen_params"] = resolved["effective"]
        if resolved["unsupported_params"]:
            metadata["unsupported_params"] = resolved["unsupported_params"]
        asset = Asset.objects.create(
            play=play, episode=shot.scene.episode, scene=shot.scene, shot=shot,
            asset_type=AssetType.IMAGE, status=AssetStatus.READY, artifact_url=url,
            metadata=metadata, cost=result.get("cost", 0),
            parameters={k: v for k, v in resolved["effective"].items()},
            generation_spec=spec, input_fingerprint=fp,
            storage_key=storage_key, content_sha256=content_sha,
        )
        # 依赖边补齐（工作台 Stage A2，design D5）：图片依赖本场分镜（结构/prompt 输入）
        # 与对白角色定妆照（参考图输入）——影响预检（任务 3.1）据此判定"改定妆照
        # 只失效引用镜头"，也让重做分镜时级联失效下游图片。
        _link_upstream(asset, _shot_upstream_assets(shot))
        _maybe_vision_review(play, asset, shot)
        return {"asset_id": asset.pk, "url": url}
    return _run(record_id, work)


def _generate_image_preview(record, shot, play, config):
    """预览草稿图（主计划 3.5.3.5 / design D2）。

    - 低分辨率参数下发（size 覆盖 ASSET_PREVIEW_SIZE、quality=low——
      OpenAI 兼容适配器已透传两者）；
    - 跳过指纹复用（预览就是要看新方向）；
    - 产出行不挂 shot/character FK、episode/scene 置空、空指纹——
      版本链（_scope_filter 不含它）、主图指针（_update_current_asset_fk
      的 self.shot 不成立）、合成取图（按 shot 查询取不到）、下游派发
      （handle_asset_ready 要求 episode 非空）四路全部隔离；
    - 不落依赖边、不做视觉质检（草稿不进生产影响面）。
    """
    size = getattr(settings, "ASSET_PREVIEW_SIZE", "512x512")
    if (resolve_gen_params(play, config)["effective"].get("aspect_ratio") == "9:16"):
        size = "512x896"
    ref_url = ""
    if shot.dialogue_character and shot.dialogue_character.reference_image:
        ref_url = shot.dialogue_character.reference_image.artifact_url
    result = _gateway(play).call(
        "IMAGE", config, play=play, task_record=record,
        shot=shot, prompt=shot.visual_prompt,
        reference_images=[ref_url] if ref_url else [],
        size=size, quality="low",
    )
    storage_key, url, content_sha = "", result.get("url", ""), ""
    if not url:
        url = _save_image_result(result, f"play{play.pk}_shot_{shot.pk}_preview.png")
    if result.get("image_bytes"):
        storage_key, url, content_sha = _store_generated_bytes(
            play, result["image_bytes"], AssetType.IMAGE)
    metadata = {
        "preview": True, "preview_of_shot": shot.pk,
        "preview_size": size, "usage": result.get("usage", {}),
        "cost_basis": "preview_low_res",
        "media_validated": "_media" in result,
    }
    if "_media" in result:
        metadata["media"] = {k: result["_media"][k] for k in ("width", "height", "vcodec")}
    asset = Asset.objects.create(
        play=play, asset_type=AssetType.IMAGE, status=AssetStatus.READY,
        artifact_url=url, metadata=metadata, cost=result.get("cost", 0),
        parameters={"size": size, "quality": "low"},
        input_fingerprint="",  # 空指纹：_reusable_asset 保守不命中（历史资产语义）
        storage_key=storage_key, content_sha256=content_sha,
    )
    return {"asset_id": asset.pk, "url": url, "preview": True,
            "preview_of_shot": shot.pk}


@shared_task(name="shortplay.tasks.generate_tts", **_TASK_RETRY_PAID)
def generate_tts(record_id):
    def work(record):
        shot = record.shot
        if not shot.dialogue:
            return {"skipped": True, "reason": "no_dialogue"}
        play = shot.scene.episode.play
        config = _config(play)
        voice_id = ""
        if shot.dialogue_character and shot.dialogue_character.voice_profile:
            voice_id = shot.dialogue_character.voice_profile.voice_id
        # 生成规格 + 指纹（Stage A2 任务 2.5）：对白文本+声线+模型定格；同指纹复用
        audio_ups = _shot_upstream_assets(shot, include_script=True, include_char_ref=False)
        spec = normalize_spec(
            target_type=AssetType.AUDIO,
            direct_content={"text": shot.dialogue, "voice_id": voice_id},
            model=getattr(config, "tts_model", None),
            upstream=upstream_refs(audio_ups),
        )
        fp = fingerprint_of(spec)
        force = bool((record.input_summary or {}).get("force")
                     or (record.input_summary or {}).get("redo"))
        reused = _reusable_asset(AssetType.AUDIO, shot, fp, force=force)
        if reused is not None:
            return _mark_reused(record, reused)
        result = _gateway(play).call(
            "TTS", config, play=play, task_record=record, shot=shot,
            text=shot.dialogue, voice_id=voice_id,
        )
        from dvadmin.utils.storage import save_artifact
        audio = result.get("audio_bytes")
        url = result.get("url", "")
        duration = result.get("duration_seconds", 0) or (result.get("usage") or {}).get("audio_seconds", 0)
        metadata = {"usage": result.get("usage", {})}
        storage_key, content_sha = "", ""
        if audio:
            # 入库前校验并以探测时长为准，不信任适配器自报（D12）
            info = _validate_media_bytes(audio, "audio")
            duration = info["duration"]
            metadata.update(media=info, media_validated=True)
            storage_key, url, content_sha = _store_generated_bytes(play, audio, AssetType.AUDIO)
        else:
            metadata["media_validated"] = False
        metadata["duration_seconds"] = duration
        asset = Asset.objects.create(
            play=play, episode=shot.scene.episode, scene=shot.scene, shot=shot,
            asset_type=AssetType.AUDIO, status=AssetStatus.READY, artifact_url=url,
            metadata=metadata, cost=result.get("cost", 0),
            generation_spec=spec, input_fingerprint=fp,
            storage_key=storage_key, content_sha256=content_sha,
        )
        # 依赖边补齐（Stage A2，design D5）：配音依赖其文本输入——分镜条目（对白来源）
        # 与剧本（该集文本上游）。对白变化时预检/级联据此只失效 AUDIO→VIDEO 链。
        _link_upstream(asset, audio_ups)
        return {"asset_id": asset.pk, "duration_seconds": duration}
    return _run(record_id, work)


@shared_task(name="shortplay.tasks.select_bgm", **_TASK_RETRY_PAID)
def select_bgm(record_id):
    def work(record):
        episode = record.episode
        play = episode.play
        moods = [s.mood for s in episode.scenes.all() if s.mood]
        bgm = None
        for mood in moods:
            bgm = AssetLibrary.objects.filter(
                asset_type=AssetType.BGM, is_public=True,
                metadata__mood=mood,
            ).first()
            if bgm:
                break
        if bgm is None:
            bgm = AssetLibrary.objects.filter(
                asset_type=AssetType.BGM, is_public=True,
            ).first()
        if bgm is None:
            return {"skipped": True, "reason": "no_bgm_in_library"}
        asset = Asset.objects.create(
            play=play, episode=episode, asset_type=AssetType.BGM,
            status=AssetStatus.READY, artifact_url=bgm.file_url,
            metadata={"library_id": bgm.pk, "name": bgm.name},
        )
        return {"asset_id": asset.pk}
    return _run(record_id, work)


# ─── Step 6 视频合成 ───────────────────────────────────────────────────────

W, H = 1080, 1920
FPS = 30
DEFAULT_CRF = 23
DEFAULT_PRESET = "medium"
DEFAULT_TD = 0.5
DEFAULT_TAIL = 0.3  # 配音结束后的呼吸留白（style_params.shot_tail_padding）
# 转场名 -> xfade transition 值（cut 用 concat 硬切）
_TRANSITION_FILTERS = {"fade": "fade", "slide": "slideleft", "wipe": "wipeleft"}
# 中文字体候选（Windows / Linux）—— 优先使用 settings.SUBTITLE_FONT_FILE
_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
]
_ENCODER_CACHE = None


class RenderCancelled(Exception):
    """渲染被用户取消。"""


def _style(config):
    return (config.style_params or {}) if config else {}


def _effective_style(config, play=None):
    """合成用有效风格（任务 1.4）：Play.gen_params.style 命中配方 grade 时
    以该风格配置覆盖 style_params 同名键；未命中/未配置时保持配方原样
    （非法风格不进 unsupported 合成——合成是本地确定性任务，不猜映射就沿用现状）。

    映射结果：style_params 与 {"grade": {style: {...}}} 顶层合并，风格配置作为
    生效 grade 传入 _resolve_color_grade（parse_color_grade 只认 grade 里的
    lut/intensity/brightness/... 键）。
    """
    base = dict(_style(config))
    if play is None:
        return base
    resolved = resolve_gen_params(play, config)
    mapping = resolved.get("style_mapping")
    if mapping and resolved["effective"].get("style"):
        # style → grade 配置：覆盖合成层读取的 grade 键（style_params.grade 优先）
        base.update(mapping)
    return base


# ── 字体解析 ────────────────────────────────────────────────────────────────

def _resolve_font_path():
    configured = getattr(settings, "SUBTITLE_FONT_FILE", "")
    if configured:
        return configured
    for cand in _FONT_CANDIDATES:
        if os.path.exists(cand):
            return cand
    return None


def _font_family(path):
    if not path:
        return "SimSun"
    name = Path(path).stem.lower()
    if "msyh" in name:
        return "Microsoft YaHei"
    if "simhei" in name:
        return "SimHei"
    if "simsun" in name or "song" in name:
        return "SimSun"
    if "noto" in name and "cjk" in name:
        return "Noto Sans CJK SC"
    if "wqy" in name:
        return "WenQuanYi Zen Hei"
    if "droid" in name:
        return "Droid Sans Fallback"
    return Path(path).stem


def _resolve_font():
    path = _resolve_font_path()
    if path:
        return str(Path(path).parent), _font_family(path)
    logger.warning("未找到中文字体，使用默认字体")
    return getattr(settings, "FONT_DIR", "") or "", "SimSun"


# ── ffmpeg 运行（进度 / 取消 / 超时）─────────────────────────────────────

def run_ffmpeg(args, timeout=600, progress_cb=None, cancel_check=None):
    """运行 ffmpeg CLI（可 mock）。

    args: ffmpeg 参数（不含可执行路径）。
    progress_cb(seconds)：进度回调；cancel_check()：返回 True 终止并抛 RenderCancelled。
    需要进度/取消时自动追加 -progress pipe:1 -nostats。
    """
    use_progress = bool(progress_cb or cancel_check)
    if use_progress:
        args = list(args) + ["-progress", "pipe:1", "-nostats"]
    cmd = [settings.FFMPEG_BIN] + args
    # 显式 UTF-8 + replace：中文 Windows 默认 cp936，遇到非 GBK 字节时 stderr 读线程会
    # 抛异常静默退出，管道写满后 ffmpeg 永久阻塞。stdin 置空防止 ffmpeg 读交互按键。
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    err_chunks = []

    if not use_progress:
        try:
            _, err = proc.communicate(timeout=timeout)
            err_chunks.append(err or "")
        except subprocess.TimeoutExpired:
            proc.terminate()
            raise
        if proc.returncode != 0:
            _raise_ffmpeg_error(proc.returncode, cmd, err_chunks)
        return proc.returncode

    def _read_stderr():
        try:
            for line in proc.stderr:
                err_chunks.append(line)
        except Exception:  # noqa: BLE001
            pass

    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stderr_thread.start()

    timed_out = threading.Event()

    def _watchdog():
        time.sleep(timeout)
        if proc.poll() is None:
            timed_out.set()
            proc.terminate()

    threading.Thread(target=_watchdog, daemon=True).start()
    last = 0.0
    try:
        while True:
            line = proc.stdout.readline()
            if line == "" and proc.poll() is not None:
                break
            if line.startswith("out_time_ms="):
                try:
                    last = int(line.split("=", 1)[1]) / 1_000_000.0
                except ValueError:
                    pass
                if progress_cb:
                    progress_cb(last)
            if cancel_check and cancel_check():
                proc.terminate()
                raise RenderCancelled("渲染被取消")
    finally:
        proc.stdout.close()
        stderr_thread.join(timeout=3)
    if progress_cb:
        progress_cb(last)
    proc.wait()
    if timed_out.is_set():
        # 超时与编码错误区分开：不得被当作编码失败触发硬编回退重跑
        raise subprocess.TimeoutExpired(cmd, timeout, output="".join(err_chunks))
    if proc.returncode != 0:
        _raise_ffmpeg_error(proc.returncode, cmd, err_chunks)
    return proc.returncode


def _raise_ffmpeg_error(returncode, cmd, err_chunks):
    """记录 ffmpeg stderr 尾部便于排查，并抛 CalledProcessError。"""
    stderr = "".join(err_chunks)
    logger.error("ffmpeg 失败 (code=%s): %s", returncode, stderr[-2000:])
    raise subprocess.CalledProcessError(returncode, cmd, output=stderr)


_MAX_REDIRECTS = 3


def _media_path(url, play_pk=None):
    """把 /media/... 解析为 MEDIA_ROOT 内的真实路径；越界（../ 等）或非媒体路径抛 ValueError。

    play_pk 给定且 URL 带剧前缀（play{id}_）时校验归属：非本剧文件拒绝，
    防止跨剧引用他剧的私有生成产物。无前缀 URL（历史数据 / 共享素材库）
    不做剧归属校验，保持兼容。
    """
    if not url.startswith(settings.MEDIA_URL):
        raise ValueError(f"只允许 {settings.MEDIA_URL} 下的本地文件: {url}")
    root = Path(settings.MEDIA_ROOT).resolve()
    src = (root / url[len(settings.MEDIA_URL):]).resolve()
    if src != root and root not in src.parents:
        raise ValueError(f"本地路径越界: {url}")
    m = re.search(r"play(\d+)", url)
    if m and play_pk is not None and int(m.group(1)) != int(play_pk):
        raise ValueError(f"媒体文件不属于当前短剧: {url}")
    return src


def _download(url, path, play_pk=None):
    """从 URL 或 /media/ 本地路径下载到本地文件。

    - 远程：只允许公网地址（或白名单内部主机），逐跳校验重定向，防 SSRF
    - 本地：只允许 MEDIA_ROOT 内的文件，拒绝 ../ 越界与任意绝对路径；
      play_pk 给定且 URL 带剧前缀时校验剧归属（跨剧引用拒绝）
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if url.startswith(("http://", "https://")):
        import httpx
        from dvadmin.utils.url_safety import check_peer, check_url
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            check_url(current)
            with httpx.stream("GET", current, follow_redirects=False, timeout=60) as resp:
                # 连接后、读响应体前校验实际对端 IP：DNS rebinding 让连接落到内网时在此中断
                stream = resp.extensions.get("network_stream")
                peer = stream.get_extra_info("server_addr") if stream else None
                check_peer((resp.url.host or "").lower(), peer)
                if resp.is_redirect:
                    current = str(resp.next_request.url) if resp.next_request else ""
                    if not current:
                        raise ValueError("重定向缺少 Location")
                    continue
                resp.raise_for_status()
                with open(path, "wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
                return path
        raise ValueError(f"重定向次数超过 {_MAX_REDIRECTS}")
    import shutil
    shutil.copyfile(_media_path(url, play_pk), path)
    return path


def _probe_encoders():
    """探测可用视频编码器（缓存）。"""
    global _ENCODER_CACHE
    if _ENCODER_CACHE is not None:
        return _ENCODER_CACHE
    encoders = set()
    try:
        proc = subprocess.run(
            [settings.FFMPEG_BIN, "-hide_banner", "-encoders"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            # ffmpeg -encoders 行格式: " V....D h264_nvenc  NVIDIA NVENC ..."
            # parts[0] 是标志列（首字符为 V/A/S），编码器名在 parts[1]
            if len(parts) >= 2 and parts[0] and parts[0][0] in ("V", "A", "S"):
                encoders.add(parts[1])
    except Exception:  # noqa: BLE001
        pass
    _ENCODER_CACHE = encoders
    return encoders


def _pick_video_encoder(style):
    if style.get("hw_encode"):
        encoders = _probe_encoders()
        for enc in ("h264_nvenc", "h264_qsv"):
            if enc in encoders:
                return enc
    return "libx264"


def _probe_media(path):
    """ffprobe 探测媒体元数据（宽高/fps/时长/码率/大小/编码/音轨）；失败抛 ValueError。"""
    proc = subprocess.run(
        [settings.FFPROBE_BIN, "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise ValueError(f"无法探测媒体: {path} ({(proc.stderr or '').strip()[:200]})")
    try:
        return media_utils.parse_probe(json.loads(proc.stdout))
    except ValueError as exc:
        raise ValueError(f"ffprobe 输出无法解析: {path}") from exc


def _probe_duration(path):
    """通过 ffprobe 探测时长（秒）；失败抛异常（不得静默返回 0）。"""
    duration = _probe_media(path)["duration"]
    if duration <= 0:
        raise ValueError(f"无法探测时长: {path}")
    return duration


def _voice_duration(path, asset):
    """配音实际时长：ffprobe 探测 -> 元数据 -> 0（失败记录警告）。"""
    try:
        return _probe_duration(path)
    except Exception as exc:  # noqa: BLE001
        fallback = float(((asset.metadata or {}) if asset else {}).get("duration_seconds") or 0)
        logger.warning("配音时长探测失败，改用元数据 %.2fs: %s", fallback, exc)
        return fallback


# ── Ken Burns 多样性 + 图生视频 ───────────────────────────────────────────

def _canvas(style=None, preview=False, aspect_ratio=None):
    """输出画布（D1）：运行时读取 W/H/FPS（测试可 patch）；预览按 preview_scale 缩小。

    aspect_ratio（任务 1.4）：Play/配方有效比例覆盖默认 W/H（9:16→1080x1920、
    16:9→1920x1080 等，见 gen_params.ASPECT_RATIOS）；未提供/未知时保持 W/H 默认
    （测试 patch W/H 兼容——显式比例优先于模块默认常量）。
    """
    w, h = W, H
    if aspect_ratio:
        from .pipeline.gen_params import aspect_ratio_dims
        dims = aspect_ratio_dims(aspect_ratio)
        if dims:
            w, h = dims
    if not preview:
        # full=True 仅当保持默认 W×H（_COVER 常量滤镜可直接复用；测试 patch W/H 时
        # 运行期值即默认值）；比例覆盖后的画布标 full=False（_cover 需按尺寸动态生成）
        return {"w": w, "h": h, "fps": FPS, "scale": 1.0,
                "full": (w, h) == (W, H)}
    w2, h2, s = media_utils.preview_size(w, h, (style or {}).get("preview_scale"))
    return {"w": w2, "h": h2, "fps": FPS, "scale": s, "full": False}


def _cover(canvas=None):
    """画布封面滤镜：canvas 为 None/默认 full（W×H）用模块常量 _COVER（测试可 patch）；
    其他画布（含比例覆盖后的 16:9 等）按 canvas 尺寸动态生成。"""
    if canvas is None or canvas.get("full"):
        return _COVER
    w, h = canvas["w"], canvas["h"]
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps={FPS},format=yuv420p"


def _kenburns_zoompan(style, index, frames, canvas=None):
    w, h = (canvas["w"], canvas["h"]) if canvas else (W, H)
    variety = style.get("kenburns_variety") or ["zoom_in"]
    mode = variety[index % len(variety)] if variety else "zoom_in"
    z, x, y = "min(zoom+0.0015,1.5)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if mode == "zoom_out":
        z = "if(lte(zoom,1),1.5,max(1.001,zoom-0.0015))"
    elif mode == "pan_left":
        z, x = "1.25", f"(iw-iw/zoom)*(on/{max(1, frames)})"
    elif mode == "pan_right":
        z, x = "1.25", f"(iw-iw/zoom)*(1-on/{max(1, frames)})"
    elif mode == "pan_up":
        z, y = "1.25", f"(ih-ih/zoom)*(on/{max(1, frames)})"
    elif mode == "pan_down":
        z, y = "1.25", f"(ih-ih/zoom)*(1-on/{max(1, frames)})"
    return f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={FPS}"


_COVER = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},format=yuv420p"


def _fit_duration_filter(duration, canvas=None):
    """视频类片段归一到计划时长：短则冻结末帧补齐，长则裁剪（D2）。"""
    return (f"{_cover(canvas)},tpad=stop_mode=clone:stop_duration={duration:.3f},"
            f"trim=duration={duration:.3f},setpts=PTS-STARTPTS")


def _video_input_segment(path, role, planned=None, canvas=None):
    """视频文件片段规格：探测真实时长与音轨；planned 为 None 时用探测时长。"""
    info = _probe_media(path)
    if info["duration"] <= 0:
        raise ValueError(f"视频片段时长无效: {path}")
    duration = float(planned if planned is not None else info["duration"])
    return {
        "kind": "video", "role": role, "path": path, "duration": duration,
        "has_audio": bool(info.get("has_audio")),
        "input_args": ["-i", str(path)],
        "filter": _fit_duration_filter(duration, canvas),
    }


def _video_segment(tmp, shot, image_asset, config, duration, canvas=None):
    """尝试图生视频；未配置/未实现/失败返回 None（降级 Ken Burns）。

    导演台参考注入（2026-11-23 组 5）：镜头有 ShotDirecting 绑定时，
    机位视角/运镜指令以 prompt 参数注入（Hailuo/Kling 均读 parameters["prompt"]），
    参考帧（reference_frames.first/last）经 gateway payload 透传到适配器 parameters
    （首帧即 image_url 本身；last 供支持尾帧的适配器消费）。计费与守卫不变。
    """
    if not (config and config.video_model):
        return None
    extra = {}
    directing = getattr(shot, "directing", None)
    if directing is not None:
        desc = directing.camera_description()
        if desc:
            extra["prompt"] = desc
        last_frame = (directing.reference_frames or {}).get("last")
        if last_frame:
            extra["last_frame_image"] = last_frame
    try:
        result = _gateway(shot.scene.episode.play).call(
            "VIDEO", config, play=shot.scene.episode.play, shot=shot,
            image_url=image_asset.artifact_url, duration=duration, **extra,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("图生视频失败，降级 Ken Burns: %s", exc)
        return None
    if not isinstance(result, dict) or not result.get("url"):
        logger.warning("图生视频无产物，降级 Ken Burns")
        return None
    try:
        path = _download(result["url"], tmp / f"v_{shot.pk}.mp4",
                         play_pk=shot.scene.episode.play_id)
        # 不信任供应商返回时长：探测真实时长，再 tpad/trim 到计划时长
        return _video_input_segment(path, "body", planned=duration, canvas=canvas)
    except Exception as exc:  # noqa: BLE001
        logger.warning("图生视频片段不可用，降级 Ken Burns: %s", exc)
        return None


def _make_segment(tmp, shot, image_asset, config, style, index, duration, canvas=None, allow_i2v=True):
    """生成单 Shot 动态片段输入规格（一次编码，不在片段级再编码）。duration 为计划时长。

    allow_i2v=False（预览）时不调用付费图生视频模型，直接 Ken Burns。
    """
    vseg = _video_segment(tmp, shot, image_asset, config, float(duration), canvas) if allow_i2v else None
    if vseg:
        return vseg
    img_path = _download(image_asset.artifact_url, tmp / f"s{shot.pk}.jpg",
                         play_pk=image_asset.play_id)
    frames = max(1, int(round(duration * FPS)))
    zoompan = _kenburns_zoompan(style, index, frames, canvas)
    return {
        "kind": "image", "role": "body", "path": img_path, "duration": float(duration),
        "has_audio": False,
        "input_args": ["-loop", "1", "-framerate", str(FPS), "-t", f"{duration:.3f}", "-i", str(img_path)],
        "filter": f"{zoompan},{_cover(canvas)}",
    }


def _library_segment(tmp, play, asset_type, canvas=None):
    """从 AssetLibrary 取 INTRO/OUTRO 片段输入规格；无则 None。"""
    lib = AssetLibrary.objects.filter(asset_type=asset_type, is_public=True).first()
    if not lib or not lib.file_url:
        return None
    role = "intro" if asset_type == AssetType.INTRO else "outro"
    try:
        path = _download(lib.file_url, tmp / f"{asset_type.lower()}.mp4")
        return _video_input_segment(path, role, canvas=canvas)
    except Exception as exc:  # noqa: BLE001
        logger.warning("片头片尾 %s 加载失败: %s", asset_type, exc)
        return None


def _shot_voice(tmp, play, shot):
    """返回 (本地路径, AUDIO Asset)；无配音返回 (None, None)。"""
    audio = Asset.objects.filter(
        play=play, shot=shot, asset_type=AssetType.AUDIO, status=AssetStatus.READY,
    ).order_by("-version").first()
    if not audio or not audio.artifact_url:
        return None, None
    return _download(audio.artifact_url, tmp / f"voice_{shot.pk}.mp3", play_pk=play.pk), audio


_SILENCE = "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05"
MIN_TRIMMED_VOICE = 0.2


def _trim_voice(tmp, path):
    """去除配音首尾静音（D2）：双 areverse 处理尾部；失败或过短回退原文件。"""
    out = Path(tmp) / f"{Path(path).stem}_trim.wav"
    try:
        run_ffmpeg(["-y", "-i", str(path), "-af", f"{_SILENCE},areverse,{_SILENCE},areverse",
                    "-ar", str(_AR), str(out)], timeout=120)
        if _probe_duration(out) < MIN_TRIMMED_VOICE:
            raise ValueError("去静音后几乎无声")
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("配音去静音失败，使用原始配音 %s: %s", path, exc)
        return path


def _sfx_accessible(lib, play):
    """素材库音效是否可被该短剧使用：公开，或本剧 active 成员所有。

    成员表语义（2026-11-16 重构）：剧内任何 active 成员的私有素材都可在
    该剧内使用（属主私有素材对成员可用的同一规则）。
    """
    if lib.is_public:
        return True
    member_ids = set(
        PlayMember.objects.filter(
            play_id=play.pk, status=MemberStatus.ACTIVE
        ).values_list("user_id", flat=True)
    )
    return lib.owner_id in member_ids


def _resolve_sound_effects(tmp, shot):
    """解析 Shot.sound_effects 为本地文件列表；无法加载的条目跳过并警告（D3）。"""
    play = shot.scene.episode.play
    out = []
    for i, sfx in enumerate(media_utils.normalize_sound_effects(shot.sound_effects)):
        try:
            url = sfx["url"]
            if sfx["library_id"]:
                lib = AssetLibrary.objects.filter(pk=sfx["library_id"], asset_type=AssetType.SFX).first()
                if not lib or not lib.file_url:
                    raise ValueError(f"素材库音效 #{sfx['library_id']} 不存在")
                if not _sfx_accessible(lib, play):
                    raise ValueError(f"素材库音效 #{sfx['library_id']} 无权访问")
                url = lib.file_url
            suffix = Path(url.split("?", 1)[0]).suffix
            path = _download(url, Path(tmp) / f"sfx_{shot.pk}_{i}{suffix if 0 < len(suffix) <= 5 else '.mp3'}")
            out.append({**sfx, "path": path})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Shot#%s 音效加载失败，跳过: %s", shot.pk, exc)
    return out


# ── 时间轴（xfade 偏移 / 总时长 / 各段可视起点）──────────────────────────

def _xfade_timeline(durations, td):
    n = len(durations)
    offsets, starts, cum = [], [], 0.0
    for i in range(1, n):
        cum += durations[i - 1]
        offsets.append(max(0.0, cum - i * td))
    total = sum(durations) - (n - 1) * td
    cum = 0.0
    for i in range(n):
        if i == 0:
            starts.append(0.0)
        else:
            cum += durations[i - 1]
            starts.append(max(0.0, cum - (i - 1) * td))
    return offsets, total, starts


def _transition_params(style):
    """(转场重叠秒数, 转场样式)；cut 重叠为 0。"""
    td = max(0.0, float(style.get("transition_duration", DEFAULT_TD)))
    trans = style.get("transition_style", "fade")
    if trans not in _TRANSITION_FILTERS and trans != "cut":
        trans = "fade"
    if trans == "cut":
        td = 0.0
    return td, trans


def _plan_timeline(durations, style):
    td, trans = _transition_params(style)
    if len(durations) < 2:
        td = 0.0
    offsets, total, starts = _xfade_timeline(durations, td)
    return td, trans, offsets, total, starts


def _video_chain(n, trans, td, offsets):
    """视频拼接链：cut 用 concat，其余用 xfade。返回 (chain_expr, 输出label)。"""
    if n == 1:
        return "", "v0"
    if trans == "cut":
        labels = "".join(f"[v{i}]" for i in range(n))
        return f"{labels}concat=n={n}:v=1:a=0[vc]", "vc"
    parts, prev = [], "v0"
    for i in range(1, n):
        f = _TRANSITION_FILTERS.get(trans, "fade")
        out = "vc" if i == n - 1 else f"x{i}"
        parts.append(f"[{prev}][v{i}]xfade=transition={f}:duration={td:.3f}:offset={offsets[i-1]:.3f}[{out}]")
        prev = out
    return ";".join(parts), "vc"


# ── 字幕（ASS 渲染 / 烧录）───────────────────────────────────────────────

def _load_subtitle_track(episode):
    return SubtitleTrack.objects.filter(
        episode=episode, language="zh-CN", status=SubtitleStatus.READY,
    ).order_by("source_type").first()


def _align_subtitles(track, shots, starts, seg_of_shot, voice_durations=None, total=None):
    """把字幕条目对齐到真实时间轴（含片头偏移 / xfade 重叠）。无则 None。

    voice_durations: {shot_pk: 实际配音秒数}；有值时字幕时长与配音一致（D3）。
    total: 合成时间线总时长；条目数与对白镜头数不一致时按比例缩放到时间线。
    """
    if not track or not track.subtitle_data:
        return None
    entries = track.subtitle_data
    voice_durations = voice_durations or {}
    dialogue_shots = [s for s in shots if s.dialogue]
    first_start = starts[0] if starts else 0.0

    if len(entries) == len(dialogue_shots):
        aligned, prev_end, missing = [], None, []
        for entry, shot in zip(entries, dialogue_shots):
            dur_raw = float(entry.get("end", 0)) - float(entry.get("start", 0))
            voice = float(voice_durations.get(shot.pk) or 0)
            dur = max(0.5, voice if voice > 0 else dur_raw)
            seg_idx = seg_of_shot.get(shot.pk)
            if seg_idx is not None:
                start = starts[seg_idx]
            else:
                # 该对白镜头缺视频片段（如 IMAGE 未 READY）：接前一条目末尾保持原时长，
                # 其余条目仍按各自段落起点对齐——不再整体平移致缺段后逐条漂移
                missing.append(shot.pk)
                start = prev_end if prev_end is not None else first_start
            aligned.append({"start": start, "end": start + dur, "text": entry.get("text", "")})
            prev_end = start + dur
        if missing:
            logger.warning("部分对白 Shot 无视频片段，缺段条目按顺序补位对齐: shots=%s", missing)
        return aligned

    # 数量不匹配（分镜重做等）：整体比例缩放到合成时间线 [片头偏移, total]，
    # 等比吸收 xfade 压缩——不再仅按 starts[0] 平移保留原始间距致后段错位
    raw_max = 0.0
    for e in entries:
        raw_max = max(raw_max, float(e.get("end", 0)))
    end = total if total is not None and total > first_start else (first_start + raw_max)
    scale = ((end - first_start) / raw_max) if raw_max > 0 else 1.0
    logger.warning("字幕条目数 %d 与对白镜头数 %d 不一致，按比例缩放对齐",
                   len(entries), len(dialogue_shots))
    return [
        {"start": first_start + scale * float(e.get("start", 0)),
         "end": first_start + scale * float(e.get("end", 0)),
         "text": e.get("text", "")}
        for e in entries
    ]


_ass_time = media_utils.ass_time


def _render_subtitle_ass(entries, style, font_family):
    # PlayRes 固定为全尺寸画布：预览缩小输出时 libass 自动等比缩放，版式与母版一致（D1）
    return media_utils.render_ass(entries, style, font_family, W, H)


def _sub_escape(path):
    """ffmpeg subtitles 滤镜参数转义（冒号/引号/逗号）。"""
    p = str(path).replace("\\", "/")
    # 第 1 层（滤镜选项解析）：转义 \ ' :
    p = p.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    # 第 2 层（filtergraph 解析，先于选项解析剥掉一层转义）：转义 \ ' [ ] , ;
    for ch in "\\'[],;":
        p = p.replace(ch, "\\" + ch)
    return p


# ── 单一 filtergraph 构建 ─────────────────────────────────────────────────

_AR = 48000


def _body_range(segments, durations, starts, td):
    """正片区间 (start, end)：首个正片入场转场结束 → 末个正片出场转场开始。"""
    body = [i for i, s in enumerate(segments) if s.get("role", "body") == "body"]
    if not body:
        return 0.0, 0.0, body
    n = len(segments)
    first, last = body[0], body[-1]
    td_prev_last = td if last > 0 else 0.0
    td_next_last = td if last < n - 1 else 0.0
    end = starts[last] - td_prev_last + durations[last] - td_next_last
    return starts[first], end, body


def _mix(parts, labels, out):
    """多路相加（normalize=0 不按路数衰减）；单路直接改名。"""
    if len(labels) == 1:
        parts.append(f"[{labels[0]}]anull[{out}]")
    else:
        joined = "".join(f"[{l}]" for l in labels)
        parts.append(f"{joined}amix=inputs={len(labels)}:duration=longest:"
                     f"dropout_transition=0:normalize=0[{out}]")


def _build_filtergraph(segments, subtitle_path, voice_map, bgm_path, style,
                       font_dir, td, trans, offsets, total, starts, watermark=None,
                       sfx_map=None, grade=None, canvas=None):
    """构建单一 filter_complex 脚本与输入参数。

    返回 (script, input_args, has_audio, video_label)。所有输入一次编码。
    音频三总线（D4）：voice（对白，loudnorm）、bed（BGM + 正片片段环境声，被对白闪避）、
    free（片头片尾原声，不闪避）；最终 alimiter 防削波。
    watermark: {"spec": parse_watermark 结果, "path": 图片本地路径, "textfile": 文字文件,
    "fontfile": 字体文件}，仅覆盖正片区间（D7）。
    sfx_map: {片段序号: [音效（含 path/offset/volume/duration）]}，进独立 sfx 总线、不闪避。
    grade: {"spec": parse_color_grade 结果, "lut": LUT 本地路径或 None}，仅作用于正片片段。
    """
    input_args, parts = [], []
    n = len(segments)
    durations = [s["duration"] for s in segments]
    lut_arg = _sub_escape(grade["lut"]) if grade and grade.get("lut") else None
    for i, seg in enumerate(segments):
        input_args += seg["input_args"]
        graph = None
        if grade and seg.get("role", "body") == "body":
            graph = media_utils.color_grade_graph(grade["spec"], lut_arg, f"g{i}", f"v{i}", i)
        if graph:
            parts.append(f"[{i}:v]{seg['filter']}[g{i}]")
            parts.append(graph)
        else:
            parts.append(f"[{i}:v]{seg['filter']}[v{i}]")
    chain_expr, vlabel = _video_chain(n, trans, td, offsets)
    if chain_expr:
        parts.append(chain_expr)
    body_start, body_end, _ = _body_range(segments, durations, starts, td)
    next_idx = n

    # ── voice 总线 ──
    voice_labels = []
    for seg_idx, voice_path in sorted((voice_map or {}).items()):
        input_args += ["-i", str(voice_path)]
        lbl = f"vo{next_idx}"
        parts.append(f"[{next_idx}:a]aresample={_AR},adelay={int(round(starts[seg_idx] * 1000))}:all=1[{lbl}]")
        voice_labels.append(lbl)
        next_idx += 1

    # ── 片段原声：正片 -> bed，片头片尾 -> free ──
    bed_labels, free_labels = [], []
    for i, seg in enumerate(segments):
        if not seg.get("has_audio"):
            continue
        local0 = starts[i] - (td if i > 0 else 0.0)  # 片段本地 0 点（入场转场开始）
        lbl = f"ca{i}"
        parts.append(
            f"[{i}:a]atrim=0:{durations[i]:.3f},asetpts=PTS-STARTPTS,aresample={_AR},"
            f"apad=whole_dur={durations[i]:.3f},adelay={int(round(max(0.0, local0) * 1000))}:all=1[{lbl}]"
        )
        (bed_labels if seg.get("role", "body") == "body" else free_labels).append(lbl)

    # ── BGM：仅覆盖正片区间 ──
    if bgm_path and body_end > body_start:
        input_args += ["-stream_loop", "-1", "-i", str(bgm_path)]
        span = body_end - body_start
        parts.append(
            f"[{next_idx}:a]aresample={_AR},volume=0.8,atrim=0:{span:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=out:st={max(0.0, span - 1.0):.3f}:d={min(1.0, span):.3f},"
            f"adelay={int(round(body_start * 1000))}:all=1[bgm]"
        )
        bed_labels.append("bgm")
        next_idx += 1

    # ── 镜头音效：放在可见起点 + offset，裁剪到镜头可见区间末尾，不闪避（D3）──
    for seg_idx, effects in sorted((sfx_map or {}).items()):
        td_prev = td if seg_idx > 0 else 0.0
        td_next = td if seg_idx < n - 1 else 0.0
        visible_end = starts[seg_idx] - td_prev + durations[seg_idx] - td_next
        for sfx in effects:
            begin = starts[seg_idx] + sfx["offset"]
            span = visible_end - begin
            if sfx.get("duration"):
                span = min(span, sfx["duration"])
            if span < 0.05:
                logger.warning("音效偏移超出镜头可见区间，跳过: %s", sfx.get("path"))
                continue
            input_args += ["-i", str(sfx["path"])]
            lbl = f"fx{next_idx}"
            parts.append(f"[{next_idx}:a]aresample={_AR},volume={sfx['volume']:.3f},atrim=0:{span:.3f},"
                         f"asetpts=PTS-STARTPTS,adelay={int(round(begin * 1000))}:all=1[{lbl}]")
            free_labels.append(lbl)
            next_idx += 1

    has_audio = bool(voice_labels or bed_labels or free_labels)
    if has_audio:
        mains = []
        if voice_labels:
            _mix(parts, voice_labels, "vmix")
            # apad 到全长：sidechaincompress 在任一输入结束时停止输出，
            # 不补齐会导致最后一句对白之后 BGM/环境声全部静音
            parts.append(f"[vmix]loudnorm=I=-16:TP=-1.5:LRA=11,aresample={_AR},"
                         f"apad=whole_dur={total:.3f}[voice]")
        if bed_labels:
            _mix(parts, bed_labels, "bed")
        if voice_labels and bed_labels:
            # sidechaincompress：第 1 路为被压缩信号（bed），第 2 路为触发信号（voice）
            parts.append("[voice]asplit=2[voice_mix][voice_sc]")
            parts.append("[bed][voice_sc]sidechaincompress=threshold=0.03:ratio=8:"
                         "attack=200:release=1000[bed_duck]")
            mains += ["voice_mix", "bed_duck"]
        elif voice_labels:
            mains.append("voice")
        elif bed_labels:
            mains.append("bed")
        mains += free_labels
        _mix(parts, mains, "amain")
        parts.append(f"[amain]alimiter=limit=0.95,apad=whole_dur={total:.3f},"
                     f"atrim=0:{total:.3f}[aout]")

    if subtitle_path:
        # 字幕样式由 ASS 文件的 [V4+ Styles] 权威指定（_render_subtitle_ass）
        sub_filter = f"subtitles={_sub_escape(subtitle_path)}"
        if font_dir:
            sub_filter += f":fontsdir={_sub_escape(font_dir)}"
        parts.append(f"[{vlabel}]{sub_filter}[vsub]")
        vlabel = "vsub"

    if watermark and body_end > body_start:
        vlabel = _apply_watermark(parts, input_args, watermark, vlabel, next_idx,
                                  body_start, body_end, style, bool(subtitle_path), canvas)

    return ";".join(parts), input_args, has_audio, vlabel


def _apply_watermark(parts, input_args, wm, vlabel, input_idx, start, end, style, has_sub, canvas=None):
    """在字幕之后叠加水印，仅在正片区间可见；底部位置避开字幕区。尺寸按画布比例缩放。"""
    spec = dict(wm["spec"])
    width_px = canvas["w"] if canvas else W
    s = (canvas or {}).get("scale", 1.0)
    spec["margin"] = int(round(spec["margin"] * s))
    spec["font_size"] = max(6, int(round(spec["font_size"] * s)))
    enable = f"enable='between(t,{start:.3f},{end:.3f})'"
    ss = (style or {}).get("subtitle_style") or {}
    bottom_extra = int(round((int(ss.get("margin_v", 80)) + int(ss.get("font_size", 48))) * s)) if has_sub else 0
    if spec["type"] == "image":
        input_args += ["-loop", "1", "-i", str(wm["path"])]
        width = media_utils._even(width_px * spec["scale"])
        x, y = media_utils.watermark_xy(spec["position"], spec["margin"], "main_w", "main_h",
                                        "overlay_w", "overlay_h", bottom_extra)
        parts.append(f"[{input_idx}:v]scale={width}:-2,format=rgba,"
                     f"colorchannelmixer=aa={spec['opacity']:.3f}[wm]")
        parts.append(f"[{vlabel}][wm]overlay=x={x}:y={y}:shortest=1:{enable}[vwm]")
    else:
        x, y = media_utils.watermark_xy(spec["position"], spec["margin"], "w", "h",
                                        "text_w", "text_h", bottom_extra)
        font = f":fontfile={_sub_escape(wm['fontfile'])}" if wm.get("fontfile") else ""
        color = "0x" + spec["color"].lstrip("#")
        parts.append(
            f"[{vlabel}]drawtext=textfile={_sub_escape(wm['textfile'])}{font}:"
            f"fontsize={spec['font_size']}:fontcolor={color}:alpha={spec['opacity']:.3f}:"
            f"x={x}:y={y}:{enable}[vwm]"
        )
    return "vwm"


def _write_filter_script(tmp, script):
    path = tmp / "filter.txt"
    path.write_text(script, encoding="utf-8")
    return path


_FILTER_SCRIPT_OPT = None


def _filter_script_args(path):
    """filtergraph 从文件读取的参数：ffmpeg ≥7 用 `-/filter_complex`，
    旧版用 `-filter_complex_script`（ffmpeg 8+ 已移除该选项）。"""
    global _FILTER_SCRIPT_OPT
    if _FILTER_SCRIPT_OPT is None:
        major = _ffmpeg_major_version()
        # 无法识别版本（如 git 构建 "N-xxxxx"）时按新版处理
        _FILTER_SCRIPT_OPT = "-filter_complex_script" if major and major < 7 else "-/filter_complex"
    return [_FILTER_SCRIPT_OPT, str(path)]


def _ffmpeg_major_version():
    try:
        proc = subprocess.run([settings.FFMPEG_BIN, "-version"], capture_output=True,
                              text=True, errors="replace", timeout=30)
    except Exception:  # noqa: BLE001
        return None
    m = re.match(r"ffmpeg version n?(\d+)\.", proc.stdout or "")
    return int(m.group(1)) if m else None


def _extract_poster(out, poster_path, ts=0.0):
    run_ffmpeg(["-y", "-ss", f"{ts:.3f}", "-i", str(out), "-frames:v", "1", str(poster_path)])


def _encode_with_fallback(head, out, vcodec, crf, preset, total, cancel_check, extra=None):
    """以选定编码器编码；硬件编码运行失败时回退 libx264 重试一次。返回实际编码器。

    取消（RenderCancelled）与超时（TimeoutExpired）不属于 CalledProcessError，不触发回退。
    extra: 追加的编码参数（如固定关键帧间隔，D6）。
    """
    tail = list(extra or []) + ["-pix_fmt", "yuv420p", "-r", str(FPS), "-movflags", "+faststart", str(out)]

    def _run_once(codec):
        run_ffmpeg(head + media_utils.encoder_args(codec, crf, preset) + tail, timeout=1800,
                   progress_cb=lambda s: logger.info("合成进度 %.1f/%.1fs", s, total),
                   cancel_check=cancel_check)

    try:
        _run_once(vcodec)
        return vcodec
    except subprocess.CalledProcessError as exc:
        if vcodec == "libx264":
            raise
        logger.warning("硬件编码 %s 失败，回退 libx264 重试: %s", vcodec, str(exc.output or "")[-500:])
    _run_once("libx264")
    return "libx264"


def _resolve_watermark(tmp, style):
    """解析并准备水印素材；未配置或素材不可用返回 None（不影响合成）。"""
    spec = media_utils.parse_watermark((style or {}).get("watermark"))
    if spec is None:
        return None
    if spec["type"] == "image":
        try:
            path = _download(spec["url"], tmp / "watermark.png")
        except Exception as exc:  # noqa: BLE001
            logger.warning("水印图片加载失败，跳过水印: %s", exc)
            return None
        return {"spec": spec, "path": path}
    textfile = tmp / "watermark.txt"
    textfile.write_text(spec["text"], encoding="utf-8")
    return {"spec": spec, "textfile": textfile, "fontfile": _resolve_font_path()}


def _resolve_color_grade(tmp, style):
    """解析调色配置并准备 LUT；LUT 加载失败只跳过 LUT（D4）。未配置返回 None。"""
    spec = media_utils.parse_color_grade((style or {}).get("color_grade"))
    if spec is None:
        return None
    lut = None
    if spec["lut"]:
        try:
            lut = _download(spec["lut"], tmp / "grade.cube")
        except Exception as exc:  # noqa: BLE001
            logger.warning("LUT 加载失败，跳过 LUT（其余调色照常）: %s", exc)
    return {"spec": spec, "lut": lut}


def _decode_check(path):
    proc = subprocess.run(
        [settings.FFMPEG_BIN, "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
    )
    if proc.returncode != 0:
        raise ValueError(f"成品解码校验失败: {(proc.stderr or '')[:500]}")


def _verify_output(out, expected_duration, tolerance=1.5):
    _decode_check(out)
    actual = _probe_duration(out)
    if abs(actual - expected_duration) > tolerance:
        raise ValueError(f"成品时长校验失败: 期望 {expected_duration:.1f}s, 实际 {actual:.1f}s")
    return actual


PREVIEW_CRF = 30
PREVIEW_PRESET = "ultrafast"


@shared_task(name="shortplay.tasks.compose_video", **_TASK_RETRY_PAID)
def compose_video(record_id):
    return _run(record_id, lambda record: _compose(record, preview=False))


@shared_task(name="shortplay.tasks.render_preview", **_TASK_RETRY_PAID)
def render_preview(record_id):
    """低分辨率快速预览（production/preview-render）：产出 PREVIEW，不影响流水线状态。"""
    return _run(record_id, lambda record: _compose(record, preview=True))


def _compose(record, preview=False):
    """合成整集。preview=True：缩小画布、ultrafast、仅 libx264、不调用图生视频、
    产出 PREVIEW 且不写 Episode 字段（D11）。

    创作参数（任务 1.4）：Play.gen_params.aspect_ratio 覆盖默认画布；
    Play.gen_params.style 命中配方 grade 时覆盖合成风格。"""
    episode = record.episode
    play = episode.play
    config = _config(play)
    style = _effective_style(config, play)
    resolved = resolve_gen_params(play, config)
    canvas = _canvas(style, preview, aspect_ratio=resolved["effective"].get("aspect_ratio"))
    shots = list(Shot.objects.filter(scene__episode=episode).order_by("scene__scene_number", "shot_number"))
    if not shots:
        raise ValueError("该集无分镜，无法合成视频")
    trim_voice = style.get("voice_trim_silence", True) is not False

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # 1. 片段骨架：片头 + 有图的 Shot + 片尾；先收集配音以确定计划时长（D1）
        intro = _library_segment(tmp, play, AssetType.INTRO, canvas)
        outro = _library_segment(tmp, play, AssetType.OUTRO, canvas)
        body = []  # (shot, image_asset, voice_path, voice_seconds)
        upstream = []  # (Asset, RelationType)：成片依赖的素材，合成后落依赖边供级联失效
        missing = []  # 无可用画面的镜头
        for shot in shots:
            image_asset = Asset.objects.filter(
                play=play, shot=shot, asset_type=AssetType.IMAGE, status=AssetStatus.READY,
            ).order_by("-version").first()
            if not image_asset or not image_asset.artifact_url:
                missing.append(shot.shot_number)
                continue
            voice_path, voice_asset = _shot_voice(tmp, play, shot)
            if voice_path and trim_voice:
                voice_path = _trim_voice(tmp, voice_path)
            voice_seconds = _voice_duration(voice_path, voice_asset) if voice_path else 0.0
            body.append((shot, image_asset, voice_path, voice_seconds))
            upstream.append((image_asset, RelationType.IMAGE_TO_VIDEO))
            if voice_asset is not None:
                upstream.append((voice_asset, RelationType.AUDIO_TO_VIDEO))
        if not body:
            raise ValueError("无可合成图像素材")
        if missing and not preview:
            # 缺镜头的成片不能当整集完成：宁可失败让人补图/重做，也不产出一条
            # 静默少镜的成片并把 Episode 置 COMPLETED 后发布。
            raise ValueError(
                f"第 {', '.join(map(str, missing))} 镜头缺少可用画面，无法合成整集"
            )

        slots = ([intro] if intro else []) + [None] * len(body) + ([outro] if outro else [])
        first_body = 1 if intro else 0
        bases, voices, fixed = [], [], []
        for i, slot in enumerate(slots):
            if slot is not None:
                bases.append(slot["duration"]); voices.append(0.0); fixed.append(True)
            else:
                shot, _, _, voice_seconds = body[i - first_body]
                bases.append(float(shot.duration_estimate or 3)); voices.append(voice_seconds); fixed.append(False)
        td, trans = _transition_params(style)
        tail = float(style.get("shot_tail_padding", DEFAULT_TAIL))
        planned = media_utils.plan_segment_durations(bases, voices, fixed, td if len(slots) > 1 else 0.0, tail)

        # 2. 生成片段（时长取计划值）与配音映射
        segments, seg_of_shot, voice_map, voice_durations, sfx_map = [], {}, {}, {}, {}
        for i, slot in enumerate(slots):
            if slot is not None:
                segments.append(slot)
                continue
            shot, image_asset, voice_path, voice_seconds = body[i - first_body]
            segments.append(_make_segment(tmp, shot, image_asset, config, style, i - first_body,
                                          planned[i], canvas, allow_i2v=not preview))
            seg_of_shot[shot.pk] = i
            if voice_path:
                voice_map[i] = voice_path
                voice_durations[shot.pk] = voice_seconds
            effects = _resolve_sound_effects(tmp, shot) if shot.sound_effects else []
            if effects:
                sfx_map[i] = effects

        # 3. BGM
        bgm_path = None
        bgm_asset = Asset.objects.filter(
            play=play, episode=episode, asset_type=AssetType.BGM, status=AssetStatus.READY,
        ).order_by("-version").first()
        if bgm_asset and bgm_asset.artifact_url:
            bgm_path = _download(bgm_asset.artifact_url, tmp / "bgm.mp3")

        # 4. 时间轴
        durations = [s["duration"] for s in segments]
        td, trans, offsets, total, starts = _plan_timeline(durations, style)

        # 5. 字幕烧录（对齐真实时间轴）
        font_dir, font_family = _resolve_font()
        subtitle_path = None
        track = _load_subtitle_track(episode)
        if track:
            entries = _align_subtitles(track, shots, starts, seg_of_shot, voice_durations,
                                        total=total)
            if entries:
                subtitle_path = tmp / "sub.ass"
                subtitle_path.write_text(_render_subtitle_ass(entries, style, font_family), encoding="utf-8")

        # 6. 单一 filtergraph（offsets 由 _plan_timeline 统一计算，避免二次重算漂移）
        watermark = _resolve_watermark(tmp, style)
        grade = _resolve_color_grade(tmp, style)
        script, input_args, has_audio, vlabel = _build_filtergraph(
            segments, subtitle_path, voice_map, bgm_path, style,
            font_dir, td, trans, offsets, total, starts, watermark=watermark,
            sfx_map=sfx_map, grade=grade, canvas=canvas,
        )

        # 7. 一次编码（编码器专属质量参数；硬编失败回退 libx264，D5）；固定关键帧间隔（D6）
        out = tmp / "episode.mp4"
        head = ["-y"] + input_args + _filter_script_args(_write_filter_script(tmp, script))
        head += ["-map", f"[{vlabel}]"]
        if has_audio:
            head += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", str(_AR)]
        keyframes = media_utils.keyframe_args(style.get("gop_seconds", 2))

        def _cancel_check():
            return TaskRecord.objects.filter(pk=record.pk, status=TaskStatus.CANCELLED).exists()

        if preview:
            vcodec = _encode_with_fallback(head, out, "libx264", PREVIEW_CRF, PREVIEW_PRESET,
                                           total, _cancel_check, extra=keyframes)
        else:
            vcodec = _encode_with_fallback(
                head, out, _pick_video_encoder(style),
                style.get("crf", DEFAULT_CRF), style.get("encode_preset", DEFAULT_PRESET),
                total, _cancel_check, extra=keyframes,
            )

        # 8. 校验 + 媒体元数据（D8）
        duration = _verify_output(out, total)
        try:
            media_info = _probe_media(out)
        except Exception as exc:  # noqa: BLE001
            logger.warning("成片媒体元数据探测失败: %s", exc)
            media_info = {}
        from dvadmin.utils.storage import save_artifact
        n = episode.episode_number

        if preview:
            url = save_artifact(out.read_bytes(), f"play{play.pk}_ep{n}_preview_r{record.pk}.mp4", "previews")
            asset = Asset.objects.create(
                play=play, episode=episode, asset_type=AssetType.PREVIEW,
                status=AssetStatus.READY, artifact_url=url,
                metadata={"duration_seconds": duration, "clip_count": len(body),
                          "canvas": [canvas["w"], canvas["h"]], "media": media_info,
                          **({"gen_params": resolved["effective"]} if resolved["effective"] else {}),
                          **({"unsupported_params": resolved["unsupported_params"]}
                             if resolved["unsupported_params"] else {})},
            )
            return {"asset_id": asset.pk, "duration_seconds": duration, "url": url}

        # 9. 封面（取首个正片片段内部，D6）+ 时间轴（D6）+ 存储
        poster = tmp / "poster.jpg"
        body_idx = [i for i, s in enumerate(segments) if s.get("role", "body") == "body"]
        _extract_poster(out, poster, media_utils.poster_timestamp(starts, durations, body_idx))
        shot_of_seg = {seg_of_shot[sh.pk]: sh for sh, *_ in body}
        metas = [{"role": seg.get("role", "body"),
                  "shot_id": shot_of_seg[i].pk if i in shot_of_seg else None,
                  "emotion": shot_of_seg[i].emotion_tag if i in shot_of_seg else "",
                  "dialogue": shot_of_seg[i].dialogue if i in shot_of_seg else ""}
                 for i, seg in enumerate(segments)]
        timeline = media_utils.build_timeline(metas, starts, durations, td, total)
        url = save_artifact(out.read_bytes(), f"play{play.pk}_ep{n}.mp4", "videos")
        poster_url = save_artifact(poster.read_bytes(), f"play{play.pk}_ep{n}_poster.jpg", "posters")

        asset = Asset.objects.create(
            play=play, episode=episode, asset_type=AssetType.VIDEO,
            status=AssetStatus.READY, artifact_url=url,
            metadata={"duration_seconds": duration, "clip_count": len(body),
                      "poster_url": poster_url, "transition_style": trans,
                      "video_encoder": vcodec, "media": media_info, "timeline": timeline,
                      # 本次合成实际用到的素材 id：合成期间若发生 redo，素材会被换新，
                      # _after_video_ready 据此判定成片已过时并补派一次合成（否则旧成片
                      # 会被当成该集最终成片直接发布）。
                      "source_assets": sorted({a.pk for a, _ in upstream}),
                      **({"gen_params": resolved["effective"]} if resolved["effective"] else {}),
                      **({"unsupported_params": resolved["unsupported_params"]}
                         if resolved["unsupported_params"] else {})},
            parameters={k: v for k, v in resolved["effective"].items()},
        )
        # 素材→成片依赖边：重做镜头使图片/配音 SUPERSEDED 时，级联让旧成片失效，
        # 不会继续作为 current_video_asset 被发布（redo 有意不直接失效 VIDEO，依赖此级联）
        AssetDependency.objects.bulk_create(
            [AssetDependency(upstream=up, downstream=asset, relation_type=rel) for up, rel in upstream],
            ignore_conflicts=True,
        )
        episode.video_url = url
        episode.poster_url = poster_url
        episode.duration_seconds = int(duration)
        episode.save(update_fields=["video_url", "poster_url", "duration_seconds"])
        return {"asset_id": asset.pk, "duration_seconds": duration, "poster_url": poster_url}


# ─── 剪辑工程导出（timeline-editing，2026-11-24 组 6）───────────────────────

@shared_task(name="shortplay.tasks.export_composition", **_TASK_RETRY_PAID)
def export_composition(record_id):
    """多轨剪辑工程导出：FFmpeg 按工程渲染（design D5：轨参数进既有渲染原语，
    不重写渲染器）；产物落 Asset(VIDEO)，走既有下载端点。"""
    return _run(record_id, _export_composition)


def _export_composition(record):
    from .pipeline import timeline as TL
    from .models import Clip, Composition, Track, TrackKind

    comp = Composition.objects.select_related("play", "episode").get(
        pk=(record.input_summary or {}).get("composition_id"))
    play, episode = comp.play, comp.episode
    # 导出前校验（6.5）：worker 侧再跑一次（派发后可能被改坏）
    problems = TL.validate_composition(comp)
    if problems:
        raise ValueError(f"剪辑工程校验未通过：{problems[:3]}")

    style = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        specs = TL.export_input_specs(comp, tmp)
        segments = specs["segments"]
        total = specs["total"]

        # 字幕：复用 ASS 渲染原语
        subtitle_path = None
        if specs["subtitle_entries"]:
            _, font_family = _resolve_font()
            subtitle_path = tmp / "sub.ass"
            subtitle_path.write_text(
                _render_subtitle_ass(specs["subtitle_entries"], style, font_family),
                encoding="utf-8")

        # 时间线：视频轨片段按 order 顺序拼接（cut 转场——剪辑工程已含精确入出点，
        # 不再叠 xfade）；音频走 voice/bgm 两总线（复用 _build_filtergraph 的形状）
        starts = []
        cursor = 0.0
        for seg in segments:
            starts.append(cursor)
            cursor += seg["duration"]
        total = max(total, cursor)
        script_parts, input_args, vlabels = [], [], []
        for i, seg in enumerate(segments):
            input_args += seg["input_args"]
            script_parts.append(f"[{i}:v]{seg['filter']}[v{i}]")
            vlabels.append(f"v{i}]")
        if len(segments) > 1:
            script_parts.append(f"{''.join(vlabels)}concat=n={len(segments)}:v=1:a=0[vc]")
            vlabel = "vc"
        else:
            vlabel = "v0"
        audio_labels = []
        next_idx = len(segments)
        # 对白轨：adelay 到各片段时间线起点（voice_map 由 export_input_specs 给出
        # {视频输入序号: path}——此处按片段起点重排为音频输入）
        dialogue = Clip.objects.filter(
            track__composition=comp, track__kind=TrackKind.DIALOGUE,
            asset__isnull=False).order_by("order")
        for clip in dialogue:
            path = tmp / f"a_{clip.pk}.mp3"
            if not path.exists():
                continue
            input_args += ["-i", str(path)]
            lbl = f"vo{next_idx}"
            script_parts.append(
                f"[{next_idx}:a]aresample={_AR},adelay={int(round(clip.start * 1000))}:all=1[{lbl}]")
            audio_labels.append(lbl)
            next_idx += 1
        # 音乐轨：BGM 循环铺满正片
        if specs["bgm"]:
            input_args += ["-stream_loop", "-1", "-i", str(specs["bgm"])]
            script_parts.append(
                f"[{next_idx}:a]aresample={_AR},volume=0.8,atrim=0:{total:.3f},"
                f"asetpts=PTS-STARTPTS,afade=t=out:st={max(0.0, total - 1.0):.3f}:"
                f"d={min(1.0, total):.3f}[bgm]")
            audio_labels.append("bgm")
            next_idx += 1
        has_audio = bool(audio_labels)
        if has_audio:
            mix = "".join(f"[{l}]" for l in audio_labels)
            script_parts.append(f"{mix}amix=inputs={len(audio_labels)}:duration=longest,"
                                 f"alimiter=limit=0.95[aout]")
        # 字幕烧录
        if subtitle_path is not None:
            script_parts.append(f"[{vlabel}]subtitles={_sub_escape(subtitle_path)}[vf]")
            vlabel = "vf"

        out = tmp / "composition.mp4"
        head = ["-y"] + input_args + _filter_script_args(_write_filter_script(tmp, ";".join(script_parts)))
        head += ["-map", f"[{vlabel}]"]
        if has_audio:
            head += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", str(_AR)]

        def _cancel_check():
            return TaskRecord.objects.filter(pk=record.pk, status=TaskStatus.CANCELLED).exists()

        _encode_with_fallback(head, out, _pick_video_encoder(style),
                              style.get("crf", DEFAULT_CRF),
                              style.get("encode_preset", DEFAULT_PRESET),
                              total, _cancel_check,
                              extra=media_utils.keyframe_args(style.get("gop_seconds", 2)))
        duration = _verify_output(out, total)
        from dvadmin.utils.storage import save_artifact
        url = save_artifact(out.read_bytes(),
                            f"play{play.pk}_ep{episode.episode_number}_edit_r{record.pk}.mp4",
                            "videos")
        asset = Asset.objects.create(
            play=play, episode=episode, asset_type=AssetType.VIDEO,
            status=AssetStatus.READY, artifact_url=url,
            metadata={"duration_seconds": duration, "edited_from_composition": comp.pk,
                      "media": _probe_media(out)},
        )
        return {"asset_id": asset.pk, "duration_seconds": duration, "url": url}


# ─── 渠道版本转码（distribution/format-adaptation，D10）─────────────────────

def _measure_loudnorm(path, target):
    """loudnorm 第一遍测量，返回 measured_* 字典。"""
    proc = subprocess.run(
        [settings.FFMPEG_BIN, "-hide_banner", "-i", str(path), "-vn",
         "-af", f"loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
    )
    if proc.returncode != 0:
        raise ValueError(f"响度测量失败: {(proc.stderr or '')[-300:]}")
    return media_utils.parse_loudnorm_json(proc.stderr)


def _integrated_loudness(path):
    proc = subprocess.run(
        [settings.FFMPEG_BIN, "-hide_banner", "-nostats", "-i", str(path), "-vn",
         "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
    )
    return media_utils.parse_ebur128_integrated(proc.stderr)


def _verify_rendition(out, plan, info):
    """校验渠道版本满足全部限制；不满足抛 ValueError。"""
    problems = []
    if (info["width"], info["height"]) != (plan["width"], plan["height"]):
        problems.append(f"分辨率 {info['width']}x{info['height']}")
    if plan.get("fps") and abs(info["fps"] - plan["fps"]) > 0.5:
        problems.append(f"帧率 {info['fps']}")
    if plan.get("max_size_bytes") and info["size_bytes"] > plan["max_size_bytes"]:
        problems.append(f"文件大小 {info['size_bytes']}B")
    if abs(info["duration"] - plan["duration"]) > 1.0:
        problems.append(f"时长 {info['duration']}s")
    if plan.get("loudness") is not None and info.get("has_audio"):
        lufs = _integrated_loudness(out)
        info["loudness_lufs"] = lufs
        if abs(lufs - plan["loudness"]) > 1.0:
            problems.append(f"响度 {lufs} LUFS")
    if problems:
        raise ValueError("渠道版本校验失败：" + "、".join(problems))


@shared_task(name="shortplay.tasks.render_channel_version", **_TASK_RETRY_PAID)
def render_channel_version(record_id):
    """由母版一次转码生成渠道版本，结果写入 Publication.rendition。"""
    from apps.distribution import rendition as rd

    def work(record):
        pub_id = (record.input_summary or {}).get("publication_id")
        pub = rd.claim(pub_id, record.pk)
        if pub is None:
            return {"skipped": True, "reason": "superseded"}  # 已有更新的派发
        try:
            url, info = _render_channel_version(pub)
        except Exception as exc:  # noqa: BLE001 — 失败记录在渠道版本上
            logger.warning("渠道版本生成失败 Publication#%s: %s", pub_id, exc)
            rd.finish(pub_id, error=str(exc)[:500])
            return {"publication_id": pub_id, "status": rd.FAILED, "error": str(exc)[:500]}
        rd.finish(pub_id, url=url, media=info)
        return {"publication_id": pub_id, "status": rd.READY, "url": url}
    return _run(record_id, work)


def _render_channel_version(pub):
    req = pub.channel.format_requirements or {}
    video = pub.video_asset
    if video is None or not video.artifact_url:
        raise ValueError("发布记录缺少视频产出物")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = _download(video.artifact_url, tmp / "master.mp4", play_pk=video.play_id)
        media_info = _probe_media(src)  # 以实际文件为准，不依赖母版元数据
        plan = media_utils.rendition_plan(media_info, req)
        has_audio = media_info.get("has_audio")
        script = media_utils.rendition_filter(plan)
        if has_audio:
            afilter = f"aresample={_AR}"
            if plan.get("loudness") is not None:
                measured = _measure_loudnorm(src, plan["loudness"])
                afilter = media_utils.loudnorm_second_pass(plan["loudness"], measured) + f",aresample={_AR}"
            script += f";[0:a]{afilter}[aout]"
        out = tmp / "rendition.mp4"
        args = ["-y", "-i", str(src)] + _filter_script_args(_write_filter_script(tmp, script))
        args += ["-map", "[vout]"]
        if has_audio:
            args += ["-map", "[aout]", "-c:a", "aac", "-b:a", f"{media_utils.AUDIO_KBPS}k", "-ac", "2"]
        args += media_utils.encoder_args("libx264", DEFAULT_CRF, DEFAULT_PRESET)
        if plan.get("video_kbps_cap"):
            cap = plan["video_kbps_cap"]
            args += ["-maxrate", f"{cap}k", "-bufsize", f"{cap * 2}k"]
        args += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
        run_ffmpeg(args, timeout=1800)
        info = _probe_media(out)
        _verify_rendition(out, plan, info)
        from dvadmin.utils.storage import save_artifact
        url = save_artifact(out.read_bytes(),
                            f"play{pub.episode.play.pk}_ep{pub.episode.episode_number}_"
                            f"{pub.channel.platform or pub.channel.pk}.mp4",
                            "renditions")
        return url, info


# ─── 字幕 ──────────────────────────────────────────────────────────────────

@shared_task(name="shortplay.tasks.generate_subtitle", **_TASK_RETRY)
def generate_subtitle(record_id):
    """自动生成中文字幕轨（zh-CN, AUTO, READY），就绪后继续派发视频合成。"""
    def work(record):
        episode = record.episode
        from apps.distribution.subtitles import generate_subtitle_track
        track, created = generate_subtitle_track(episode, language="zh-CN")
        # 字幕轨就绪（合成烧录所需）后继续素材齐备时的合成编排；
        # 若素材尚未齐备或已在进行中合成，本调用为空操作。
        try:
            from .pipeline.dispatcher import maybe_dispatch_video_compose
            maybe_dispatch_video_compose(episode)
        except Exception:
            # 编排（合成派发）失败时回滚刚创建的字幕轨：避免留下 READY 字幕
            # 却无合成被派发、且素材不再变化而无人再触发的 stall。
            if created:
                track.delete()
            raise
        return {"subtitle_track_id": track.pk, "created": created}
    return _run(record_id, work)


@shared_task(name="shortplay.tasks.translate_subtitle", **_TASK_RETRY)
def translate_subtitle(record_id):
    """将源字幕轨翻译为目标语言（source_type=TRANSLATION）。"""
    def work(record):
        from apps.distribution.subtitles import translate_subtitle_track
        track_id = (record.input_summary or {}).get("subtitle_track_id")
        target = (record.input_summary or {}).get("target_language", "en-US")
        source = SubtitleTrack.objects.get(pk=track_id) if track_id else None
        if source is None:
            source = SubtitleTrack.objects.filter(
                episode=record.episode, status=SubtitleStatus.READY,
            ).order_by("language").first()
        play = record.play
        new_track = translate_subtitle_track(
            source, target, _gateway(play), _config(play), play,
        )
        return {"subtitle_track_id": new_track.pk, "language": target}
    return _run(record_id, work)


# ─── Step 7 发布触发 ───────────────────────────────────────────────────────

@shared_task(name="shortplay.tasks.trigger_publish", **_TASK_RETRY)
def trigger_publish(record_id):
    def work(record):
        episode = record.episode
        play = episode.play
        from apps.distribution.models import DistributionChannel
        channel = DistributionChannel.objects.filter(is_active=True).first()
        if channel is None:
            return {"skipped": True, "reason": "no_channel"}
        pub, created = Publication.objects.get_or_create(
            episode=episode, channel=channel,
            defaults={"title": episode.title, "video_asset": episode.current_video_asset},
        )
        # 重做/重新合成后 current_video_asset 已更新，同步到既有发布记录，
        # 否则 ensure_rendition/is_stale 仍比较旧视频，继续发布已废弃的成片。
        if not created and pub.video_asset_id != episode.current_video_asset_id:
            pub.video_asset = episode.current_video_asset
            pub.title = episode.title
            pub.save(update_fields=["video_asset", "title"])
        from apps.distribution.rendition import RenditionBusy, ensure_rendition
        try:
            ensure_rendition(pub)
        except RenditionBusy:
            pass
        # 仅当全部分集均已完成且都已在该渠道建立发布记录时，Play 才经状态机进入 PUBLISHED
        from .pipeline import state_machine
        episodes = play.episodes.all()
        all_done = episodes.exists() and not episodes.exclude(
            status=EpisodeStatus.COMPLETED
        ).exists()
        all_published = all_done and not episodes.exclude(
            publications__channel=channel
        ).exists()
        if all_published and play.status != PlayStatus.PUBLISHED:
            state_machine.summary_play_status(play)
            if state_machine.can_transition(play, PlayStatus.PUBLISHED):
                state_machine.transition(play, PlayStatus.PUBLISHED)
        return {"publication_id": pub.pk, "play_status": play.status}
    return _run(record_id, work)


# ─── 运营衍生品（production/promo-derivatives，D7-D10）───────────────────────

DERIVATIVE_KINDS = ("trailer", "gif", "posters", "hls")


def derivative_kinds(style, requested=None):
    """请求的衍生品种类 ∩ 支持种类；未请求时取 style_params.derivatives，再缺省为全部。"""
    raw = requested if requested else (style or {}).get("derivatives") or DERIVATIVE_KINDS
    kinds = [k for k in DERIVATIVE_KINDS if k in set(raw if isinstance(raw, (list, tuple)) else [])]
    return kinds or list(DERIVATIVE_KINDS)


@shared_task(name="shortplay.tasks.generate_derivatives", **_TASK_RETRY_PAID)
def generate_derivatives(record_id):
    return _run(record_id, _generate_derivatives)


def _generate_derivatives(record):
    video = Asset.objects.filter(pk=(record.input_summary or {}).get("video_asset_id"),
                                 asset_type=AssetType.VIDEO).first()
    if video is None or video.status != AssetStatus.READY or not video.artifact_url:
        return {"skipped": True, "reason": "video_not_ready"}
    style = _style(_config(video.play))
    kinds = derivative_kinds(style, (record.input_summary or {}).get("kinds"))
    timeline = (video.metadata or {}).get("timeline") or []
    has_audio = bool(((video.metadata or {}).get("media") or {}).get("has_audio"))
    results, errors = {}, {}

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        master = _download(video.artifact_url, tmp / "master.mp4", play_pk=video.play_id)
        total = float((video.metadata or {}).get("duration_seconds") or 0) or _probe_duration(master)
        n = video.episode.episode_number if video.episode else 0
        tag = f"play{video.play.pk}_ep{n}_v{video.pk}"
        builders = {
            "trailer": lambda: _build_trailer(tmp, master, timeline, style, has_audio, tag),
            "gif": lambda: _build_gif(tmp, master, timeline, style, results.get("trailer"), tag),
            "posters": lambda: _build_posters(tmp, master, timeline, style, total, tag),
            "hls": lambda: _build_hls(tmp, master, style, tag),
        }
        for kind in kinds:  # 顺序固定：gif 可复用已生成的预告片
            try:
                results[kind] = builders[kind]()
            except Exception as exc:  # noqa: BLE001
                logger.warning("衍生品 %s 生成失败（Asset#%s）: %s", kind, video.pk, exc)
                errors[kind] = str(exc)[:500]
        if not results:
            raise ValueError("衍生品全部生成失败: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
        committed = _commit_derivatives(video.pk, results)

    summary = {k: (v.get("url") if isinstance(v, dict) and "url" in v else v)
               for k, v in results.items() if k != "trailer"}
    if "trailer" in results:
        summary["trailer"] = results["trailer"]["url"]
    discarded = committed is None
    return {"video_asset_id": video.pk, "results": {} if discarded else summary, "errors": errors,
            "discarded": discarded,
            "trailer_asset_id": None if discarded else committed.get("trailer_asset_id")}


def _commit_derivatives(video_id, results):
    """行锁合并写入母版 metadata.derivatives（QuerySet.update 不触发 post_save）；
    母版已非 READY 时放弃写入并返回 None。"""
    from django.db import transaction

    from .models import AssetDependency, RelationType
    with transaction.atomic():
        video = Asset.objects.select_for_update().get(pk=video_id)
        if video.status != AssetStatus.READY:
            logger.warning("母版 Asset#%s 已失效，放弃衍生品结果", video_id)
            return None
        meta = dict(video.metadata or {})
        derived = dict(meta.get("derivatives") or {})
        out = {}
        trailer = results.get("trailer")
        if trailer:
            asset = Asset.objects.create(
                play=video.play, episode=video.episode, asset_type=AssetType.TRAILER,
                status=AssetStatus.READY, artifact_url=trailer["url"],
                metadata={k: trailer[k] for k in ("duration_seconds", "clips", "media")}
                | {"video_asset_id": video.pk},
            )
            AssetDependency.objects.create(upstream=video, downstream=asset,
                                           relation_type=RelationType.VIDEO_TO_DERIVATIVE)
            derived["trailer"] = {"asset_id": asset.pk, "url": trailer["url"]}
            out["trailer_asset_id"] = asset.pk
        if "gif" in results:
            derived["gif_url"] = results["gif"]["url"]
        if "posters" in results:
            derived["posters"] = results["posters"]
        if "hls" in results:
            derived["hls_url"] = results["hls"]["url"]
        meta["derivatives"] = derived
        Asset.objects.filter(pk=video.pk).update(metadata=meta)
    return out


def _build_trailer(tmp, master, timeline, style, has_audio, tag):
    """按评分挑选高光片段剪辑预告片（D7）：每段输入侧 seek，concat 硬切，末尾淡出。"""
    if not timeline:
        raise ValueError("母版缺少合成时间轴，请重新合成后再生成预告片")
    cfg = style.get("trailer") or {}
    clips = media_utils.select_trailer_clips(
        timeline, float(cfg.get("min_seconds", 15)), float(cfg.get("max_seconds", 30)),
        float(cfg.get("max_clip_seconds", 6)),
    )
    if not clips:
        raise ValueError("时间轴中没有正片镜头")
    inputs, parts, labels = [], [], []
    total = sum(c["duration"] for c in clips)
    fade = min(0.5, total / 4)
    for i, c in enumerate(clips):
        inputs += ["-ss", f"{c['start']:.3f}", "-t", f"{c['duration']:.3f}", "-i", str(master)]
        parts.append(f"[{i}:v]setpts=PTS-STARTPTS,fps={FPS},format=yuv420p[tv{i}]")
        labels.append(f"[tv{i}]")
        if has_audio:
            d = c["duration"]
            parts.append(f"[{i}:a]asetpts=PTS-STARTPTS,aresample={_AR},afade=t=in:d=0.03,"
                         f"afade=t=out:st={max(0.0, d - 0.03):.3f}:d=0.03[ta{i}]")
            labels.append(f"[ta{i}]")
    a = 1 if has_audio else 0
    parts.append(f"{''.join(labels)}concat=n={len(clips)}:v=1:a={a}[tcv]" + ("[tca]" if a else ""))
    parts.append(f"[tcv]fade=t=out:st={total - fade:.3f}:d={fade:.3f}[tvout]")
    maps = ["-map", "[tvout]"]
    if a:
        parts.append(f"[tca]afade=t=out:st={total - fade:.3f}:d={fade:.3f}[taout]")
        maps += ["-map", "[taout]", "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", str(_AR)]
    out = tmp / "trailer.mp4"
    script = tmp / "trailer_filter.txt"
    script.write_text(";".join(parts), encoding="utf-8")
    run_ffmpeg(["-y"] + inputs + _filter_script_args(script) + maps
               + media_utils.encoder_args("libx264", style.get("crf", DEFAULT_CRF), "fast")
               + ["-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)], timeout=900)
    _decode_check(out)
    info = _probe_media(out)
    from dvadmin.utils.storage import save_artifact
    url = save_artifact(out.read_bytes(), f"{tag}_trailer.mp4", "trailers")
    return {"url": url, "duration_seconds": info["duration"], "media": info,
            "clips": [{"start": c["start"], "duration": c["duration"], "shot_id": c["shot_id"]} for c in clips]}


def _build_gif(tmp, master, timeline, style, trailer, tag):
    """GIF 预览（D8）：预告片开头；无预告片时取首个正片镜头。"""
    seconds = media_utils._clamp_float(style.get("gif_seconds"), 3.0, 1.0, 10.0)
    if trailer:
        src, start = tmp / "trailer.mp4", 0.0
    else:
        body = [e for e in timeline if e.get("role", "body") == "body"]
        src, start = master, (body[0]["start"] if body else 0.0)
    out = tmp / "preview.gif"
    run_ffmpeg(["-y", "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(src),
                "-filter_complex", media_utils.gif_filter(), "-loop", "0", str(out)], timeout=300)
    from dvadmin.utils.storage import save_artifact
    return {"url": save_artifact(out.read_bytes(), f"{tag}.gif", "previews")}


def _build_posters(tmp, master, timeline, style, total, tag):
    """封面候选（D8）：不同高光镜头可见区间内以 thumbnail 选代表帧。"""
    k = media_utils._clamp_int(style.get("poster_candidates"), 4, 1, 12)
    windows = media_utils.pick_poster_candidates(timeline, k, total)
    if not windows:
        raise ValueError("无可取帧的正片区间")
    from dvadmin.utils.storage import save_artifact
    out = []
    for i, w in enumerate(windows):
        span = max(0.04, w["end"] - w["start"])
        frames = max(1, min(90, int(span * FPS)))
        path = tmp / f"poster_{i}.jpg"
        run_ffmpeg(["-y", "-ss", f"{w['start']:.3f}", "-t", f"{span:.3f}", "-i", str(master),
                    "-vf", f"thumbnail=n={frames}", "-frames:v", "1", "-q:v", "2", str(path)], timeout=120)
        url = save_artifact(path.read_bytes(), f"{tag}_poster_{i}.jpg", "posters")
        out.append({"url": url, "time": w["start"], "shot_id": w["shot_id"]})
    return out


def _build_hls(tmp, master, style, tag):
    """HLS 切片（D9）：-c copy 不重编码；m3u8 与分片上传到同一子目录。"""
    hls_dir = tmp / "hls"
    hls_dir.mkdir()
    playlist = hls_dir / "index.m3u8"
    run_ffmpeg(["-y", "-i", str(master)] + media_utils.hls_args(
        style.get("hls_segment_seconds", 4), hls_dir / "seg_%03d.ts", playlist), timeout=600)
    segments = sorted(hls_dir.glob("seg_*.ts"))
    if not segments or not playlist.exists():
        raise ValueError("HLS 切片未产出分片")
    from dvadmin.utils.storage import save_artifact
    subdir = f"hls/{tag}"
    for seg in segments:
        save_artifact(seg.read_bytes(), seg.name, subdir)
    url = save_artifact(playlist.read_bytes(), playlist.name, subdir)
    return {"url": url, "segments": len(segments)}


# 创作助手轮次任务（注册到 Celery：worker 通过 shortplay.tasks 自动发现）
from .assistant.runner import run_assistant_turn  # noqa: E402,F401

# ─── 评估报告（Phase 1.7）：每周聚合评估指标（JSON 报告，无需PDF依赖）──────────

@shared_task(name="shortplay.tasks.generate_evaluation_report", **_TASK_RETRY)
def generate_evaluation_report(days=7, play_id=None):
    """生成评估报告：聚合 EvaluationMetric + 保存到 media/evaluation_reports/。

    返回 {"path": ..., "metrics": {...}}；PDF 渲染待前端/外部工具消费 JSON。
    """
    import json as _json
    from datetime import datetime
    from pathlib import Path
    from django.conf import settings as _s
    from apps.creation.assistant.evaluation import aggregate_metrics

    data = aggregate_metrics(play_id, days)
    out_dir = Path(_s.MEDIA_ROOT) / "evaluation_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = out_dir / f"report_{datetime.now():%Y%m%d_%H%M%S}.json"
    fname.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(fname), "metrics": data["metrics"], "period_days": days}


# ── 小说→剧本改编（2026-11-23 组 2 / novel-adaptation）───────────────────────

@shared_task(name="shortplay.tasks.adapt_novel", **_TASK_RETRY)
def adapt_novel(record_id):
    """章节范围改编：分块摘要→提炼场景→拆剧集→场次/对白/动作（块级断点续跑）。

    产出走提案闸（audit_mode 复用）：剧本结构以 EPISODE_SCRIPT 提案形式落
    AssistantProposal（payload.adapt 结构化场次），作者应用后才写 Episode/
    Scene/Shot——已有剧集保护在提案应用侧（create_proposal 目标剧集 PENDING
    冲突即提示确认，静默覆盖由 ProposalError 拒绝）。
    """
    def work(record):
        from .pipeline import novel_adaptation as NA
        play = record.play
        spec = (record.input_summary or {}).get("adapt", {})
        chapters = NA.collect_chapters(play, spec.get("from_chapter"), spec.get("to_chapter"))
        blocks = NA.chunk_chapters(chapters)
        if not blocks:
            raise ValueError("章节范围内没有可改编的章节")
        gw = _gateway(play)
        config = _config(play)
        # 断点续跑：从 output_summary["blocks"] 恢复已完成块（首跑为空 dict）
        done = (record.output_summary or {}).get("blocks", {})
        prev_summary = (record.output_summary or {}).get("tail_summary", "")
        context = NA.story_context(play, chapters)
        episodes_out = []
        for block in blocks:
            key = f"{block['start']}-{block['end']}"
            if key in done:
                episodes_out.extend(done[key].get("episodes", []))
                prev_summary = done[key].get("summary", prev_summary)
                continue
            prompt, system = NA.block_payload(block["chapters"], prev_summary, context)
            result = gw.call("LLM", config, play=play, task_record=record,
                             prompt=prompt, system_prompt=system,
                             response_format={"type": "json_object"})
            data = _parse_json(result.get("text", ""))
            if not data.get("episodes"):
                raise ValueError(f"块 {key} 改编返回空剧集")
            block_out = {"episodes": data["episodes"], "summary": data.get("summary", "")}
            # 块级落库（幂等断点）：完成块立刻写 output_summary，失败块可续跑
            done[key] = block_out
            record.output_summary = {"blocks": done, "tail_summary": block_out["summary"]}
            record.save(update_fields=["output_summary", "updated_at"])
            episodes_out.extend(data["episodes"])
            prev_summary = block_out["summary"]
            NA.emit_progress(play, record, "novel_adapt_progress", {
                "task_id": record.pk, "block": key,
                "done_blocks": len(done), "total_blocks": len(blocks),
            })
        # 产出统一提案（走既有审核闸；不静默写 Episode/Scene/Shot）
        from .assistant.models import AssistantSession, ProposalKind
        from .assistant.proposals import create_proposal
        session = AssistantSession.objects.filter(play=play).order_by("-updated_at").first()
        if session is None:
            session = AssistantSession.objects.create(play=play, user=play.created_by,
                                                      agent_config=config)
        base_version = 1
        target_ep = spec.get("target_episode")
        proposal = create_proposal(
            session, None, ProposalKind.EPISODE_SCRIPT,
            payload={"adapt": {"episodes": episodes_out, "from_chapter": spec.get("from_chapter"),
                               "to_chapter": spec.get("to_chapter"),
                               "episode_start": spec.get("episode_start", 1)}},
            summary=f"小说改编（{spec.get('from_chapter')}-{spec.get('to_chapter')} 章）",
            episode=None, base_version=base_version,
        )
        NA.emit_progress(play, record, "novel_adapt_done", {
            "task_id": record.pk, "proposal_id": proposal.pk,
            "episode_count": len(episodes_out),
        })
        # 标记章节改编引用（novel-workspace 软删提示用）
        NovelChapter.objects.filter(pk__in=[c.pk for c in chapters]).update(
            adaptation_refs=1)
        return {"proposal_id": proposal.pk, "episode_count": len(episodes_out),
                "block_count": len(blocks)}
    return _run(record_id, work)


# ── 本地 worker 任务注册表（工作台 Stage C1 任务 6.3/6.4）────────────────
# run_local_worker 按任务名取同步函数（Celery shared_task 对象可直接调用，
# 本地不走 Celery 重试机制，_run 的生命周期/取消/失败语义原样复用）。
# ASSISTANT_TURN 在 assistant.runner（末尾 import 保持原加载顺序）。
_TASK_FUNCS = {
    "shortplay.tasks.generate_outline": generate_outline,
    "shortplay.tasks.generate_episode_script": generate_episode_script,
    "shortplay.tasks.generate_character_ref": generate_character_ref,
    "shortplay.tasks.generate_storyboard": generate_storyboard,
    "shortplay.tasks.generate_image": generate_image,
    "shortplay.tasks.generate_tts": generate_tts,
    "shortplay.tasks.select_bgm": select_bgm,
    "shortplay.tasks.compose_video": compose_video,
    "shortplay.tasks.export_composition": export_composition,
    "shortplay.tasks.render_preview": render_preview,
    "shortplay.tasks.render_channel_version": render_channel_version,
    "shortplay.tasks.generate_subtitle": generate_subtitle,
    "shortplay.tasks.adapt_novel": adapt_novel,
    "shortplay.tasks.trigger_publish": trigger_publish,
    "shortplay.tasks.generate_derivatives": generate_derivatives,
    "shortplay.assistant.run_assistant_turn": None,  # 下方 import 后回填
}
_TASK_FUNCS["shortplay.assistant.run_assistant_turn"] = (
    __import__("apps.creation.assistant.runner", fromlist=["run_assistant_turn"]).run_assistant_turn
)
