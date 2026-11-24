"""发布工作流：状态流转与平台对接（可插拔平台适配）。"""
import logging

from apps.creation.models import Asset, AssetStatus, AssetType, Episode
from .models import DistributionChannel, Publication, PublicationStatus

logger = logging.getLogger(__name__)

_VALID_TRANSITIONS = {
    PublicationStatus.PENDING: {PublicationStatus.UPLOADING},
    PublicationStatus.UPLOADING: {PublicationStatus.REVIEWING},
    PublicationStatus.REVIEWING: {PublicationStatus.PUBLISHED, PublicationStatus.REJECTED},
    PublicationStatus.PUBLISHED: set(),
    PublicationStatus.REJECTED: {PublicationStatus.UPLOADING},
}


def create_publication(episode, channel):
    """创建 Publication，video_asset 指向该集 READY 的 VIDEO Asset。"""
    video = Asset.objects.filter(
        play=episode.play, episode=episode, asset_type=AssetType.VIDEO,
        status=AssetStatus.READY,
    ).order_by("-version").first()
    pub, created = Publication.objects.get_or_create(
        episode=episode, channel=channel,
        defaults={
            "video_asset": video,
            "title": episode.title or f"{episode.play.title} 第{episode.episode_number}集",
        },
    )
    return pub, created


def advance_status(publication, new_status, **fields):
    """校验并推进 Publication 状态。进入 UPLOADING 前渠道版本必须就绪。"""
    allowed = _VALID_TRANSITIONS.get(publication.status, set())
    if new_status not in allowed:
        raise ValueError(
            f"非法发布状态流转: {publication.status} -> {new_status}"
        )
    if new_status == PublicationStatus.UPLOADING:
        from .rendition import require_uploadable
        require_uploadable(publication)  # 未就绪抛 RenditionNotReady(ValueError)
    publication.status = new_status
    for key, value in fields.items():
        setattr(publication, key, value)
    publication.save()
    return publication


def upload_to_platform(publication):
    """模拟上传视频到平台（实际对接平台 API）。"""
    if publication.status != PublicationStatus.PENDING:
        raise ValueError("仅 PENDING 状态的发布可上传")
    # 上传使用渠道版本地址（无需转码时为母版），未就绪时拒绝
    return advance_status(publication, PublicationStatus.UPLOADING)


def mark_reviewing(publication, platform_video_id):
    """平台接收视频，进入审核。"""
    publication = advance_status(
        publication, PublicationStatus.REVIEWING, platform_video_id=platform_video_id
    )
    return publication


def mark_published(publication, platform_url):
    """平台审核通过。"""
    from django.utils import timezone
    publication = advance_status(
        publication, PublicationStatus.PUBLISHED,
        platform_url=platform_url, published_at=timezone.now(),
    )
    return publication


def mark_rejected(publication, reason):
    """平台审核拒绝。"""
    publication = advance_status(
        publication, PublicationStatus.REJECTED, reject_reason=reason
    )
    return publication


def validate_format(publication):
    """校验视频格式符合渠道 format_requirements。"""
    req = publication.channel.format_requirements or {}
    video = publication.video_asset
    if not video:
        raise ValueError("发布记录缺少视频产出物")
    if publication.episode.duration_seconds:
        max_duration = req.get("max_duration")
        if max_duration and publication.episode.duration_seconds > max_duration:
            raise ValueError("视频时长超过渠道限制")
    return True
