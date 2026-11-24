"""渠道格式适配：判定母版是否满足渠道要求，并管理渠道版本状态。

openspec change: backend-ffmpeg-quality-distribution（distribution/format-adaptation）。
rendition JSON 结构：
  {status, url, media, error, video_asset_id, requirements, task_record_id}
status: pending | processing | ready | not_needed | failed
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.core.utils import media as media_utils
from .models import Publication
from apps.creation.models import TaskType

logger = logging.getLogger(__name__)

PENDING, PROCESSING, READY, NOT_NEEDED, FAILED = (
    "pending", "processing", "ready", "not_needed", "failed",
)
UPLOADABLE = {READY, NOT_NEEDED}

# processing 超时：claim 后 worker 被 hard time_limit / OOM 杀死时 finish 不会执行，
# 不设超时则该发布记录永久卡在 processing（force=True 也越不过），只能改库。
# 与转码任务 soft/hard time_limit（1800/1900s）对齐，再留足余量。
PROCESSING_TIMEOUT = timedelta(seconds=3300)


class RenditionBusy(Exception):
    """渠道版本生成中，拒绝重复请求。"""


class RenditionNotReady(ValueError):
    """渠道版本未就绪，不得推进上传。"""


def _processing_timed_out(publication, rendition):
    """processing 是否已超时（用 Publication.updated_at 近似 claim 时间）。"""
    started = publication.updated_at
    if started is None:
        return False
    return timezone.now() - started > PROCESSING_TIMEOUT


def _requirements(publication):
    return dict(publication.channel.format_requirements or {})


def evaluate(publication):
    """纯判定 -> (status, reason)；不写库、不派发。"""
    video = publication.video_asset
    req = _requirements(publication)
    if video is None:
        return FAILED, "发布记录缺少视频产出物"
    media = (video.metadata or {}).get("media") or {}
    result, reason = media_utils.evaluate_requirements(media, req)
    if result == "not_needed":
        return NOT_NEEDED, ""
    if result == "failed":
        return FAILED, reason
    return PENDING, reason


def is_stale(publication):
    """母版或渠道要求变化后，已有判定结果失效。"""
    r = publication.rendition or {}
    if not r.get("status"):
        return True
    return (r.get("video_asset_id") != publication.video_asset_id
            or r.get("requirements") != _requirements(publication))


def ensure_rendition(publication, force=False):
    """执行适配判定并写入状态；需要转码时派发 RENDITION 任务。返回 rendition。

    生成中（processing）时抛 RenditionBusy。force=True 时忽略已有结果重新判定。
    """
    from apps.creation.pipeline.dispatcher import dispatch_task

    with transaction.atomic():
        pub = Publication.objects.select_for_update().select_related(
            "channel", "video_asset", "episode__play",
        ).get(pk=publication.pk)
        current = pub.rendition or {}
        timed_out = False
        if current.get("status") == PROCESSING:
            if not _processing_timed_out(pub, current):
                raise RenditionBusy("渠道版本生成中，请稍后再试")
            # 超时视为 worker 已死：按失效处理 → 重新判定并派发，避免永久卡死
            logger.warning("Publication#%s 渠道版本 processing 超时，重新派发", pub.pk)
            timed_out = True
        # 未失效时沿用结果（含 pending：不重复派发）；任务丢失时由 force（重新生成接口）补派
        if not force and not timed_out and not is_stale(pub):
            publication.rendition = current
            return current
        status, reason = evaluate(pub)
        rendition = {
            "status": status,
            "url": pub.video_asset.artifact_url if (status == NOT_NEEDED and pub.video_asset) else "",
            "media": {},
            "error": reason if status == FAILED else "",
            "reason": reason if status == PENDING else "",
            "video_asset_id": pub.video_asset_id,
            "requirements": _requirements(pub),
        }
        if status == PENDING:
            # 先写 task_record_id 再发 Celery 消息（defer_send：事务提交后才发），
            # 否则快的 worker 在 claim() 时看到 task_record_id 为空会直接放弃，渠道版本永远 pending
            record = dispatch_task(
                TaskType.RENDITION, play=pub.episode.play, episode=pub.episode,
                asset=pub.video_asset, input_summary={"publication_id": pub.pk},
                defer_send=True,
            )
            # 记录最新任务：旧任务执行时发现 id 不符即放弃（避免并发重复转码覆盖结果）
            rendition["task_record_id"] = record.pk
        pub.rendition = rendition
        pub.save(update_fields=["rendition", "updated_at"])
    publication.rendition = rendition
    return rendition


def require_uploadable(publication):
    """推进 UPLOADING 前置校验：未判定或已失效时先判定；非 ready/not_needed 抛 RenditionNotReady。"""
    rendition = publication.rendition or {}
    if is_stale(publication):
        rendition = ensure_rendition(publication)
    status = rendition.get("status")
    if status not in UPLOADABLE:
        detail = rendition.get("error") or rendition.get("reason") or ""
        raise RenditionNotReady(f"渠道版本未就绪（{status}）{('：' + detail) if detail else ''}")
    return rendition.get("url", "")


def claim(publication_id, task_record_id):
    """转码任务开始时原子占用：仅最新派发的任务可将 pending -> processing。"""
    with transaction.atomic():
        pub = Publication.objects.select_for_update().get(pk=publication_id)
        r = pub.rendition or {}
        if r.get("status") != PENDING or r.get("task_record_id") != task_record_id:
            return None
        pub.rendition = {**r, "status": PROCESSING}
        pub.save(update_fields=["rendition", "updated_at"])
        return pub


def finish(publication_id, *, url="", media=None, error=""):
    """写入转码结果（ready / failed）。"""
    with transaction.atomic():
        pub = Publication.objects.select_for_update().get(pk=publication_id)
        r = dict(pub.rendition or {})
        r.update(status=FAILED if error else READY, url=url if not error else "",
                 media=media or {}, error=error)
        pub.rendition = r
        pub.save(update_fields=["rendition", "updated_at"])
        return pub
