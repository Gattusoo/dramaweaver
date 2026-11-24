"""成品/素材存储：本地 MEDIA_ROOT fallback，可切换 S3 兼容对象存储。"""
import hashlib
import io
import logging
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)


def _local_save(data, filename, subdir=""):
    root = Path(settings.MEDIA_ROOT) / subdir
    root.mkdir(parents=True, exist_ok=True)
    dest = root / filename
    with open(dest, "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.read())
    rel = f"{subdir}/{filename}".lstrip("/")
    return f"{settings.MEDIA_URL}{rel}"


def _s3_save(data, filename, subdir=""):
    import boto3  # 可选依赖

    key = f"{subdir}/{filename}".lstrip("/")
    client = boto3.client(
        "s3",
        aws_access_key_id=settings.STORAGE_OSS_ACCESS_KEY,
        aws_secret_access_key=settings.STORAGE_OSS_SECRET_KEY,
        endpoint_url=settings.STORAGE_OSS_ENDPOINT or None,
    )
    body = data if isinstance(data, bytes) else data.read()
    client.put_object(
        Bucket=settings.STORAGE_OSS_BUCKET, Key=key, Body=body,
        ContentType="application/octet-stream",
    )
    endpoint = settings.STORAGE_OSS_ENDPOINT or "https://s3.amazonaws.com"
    return f"{endpoint}/{settings.STORAGE_OSS_BUCKET}/{key}"


def save_artifact(data, filename, subdir=""):
    """保存产物并返回可访问 URL。data 为 bytes 或 file-like。"""
    if settings.STORAGE_BACKEND == "s3":
        try:
            return _s3_save(data, filename, subdir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("S3 上传失败，回退本地存储: %s", exc)
    return _local_save(data, filename, subdir)


def save_bytes_urlsafe(data, ext=".bin", subdir="artifacts", prefix=""):
    """以内容哈希命名保存 bytes，返回 URL。"""
    digest = hashlib.sha1(data if isinstance(data, bytes) else data.read()).hexdigest()[:16]
    filename = f"{prefix}{digest}{ext}"
    return save_artifact(data, filename, subdir)
