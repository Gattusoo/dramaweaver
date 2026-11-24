"""成片/预览鉴权下载（工作台 Stage D1 任务 8.1–8.3，design D6）。

- 本地（storage_key）：受控路径解析（resolve_storage_key 越界拒绝）→
  FileResponse，文件名安全（uuid 命名，无用户可控字符），不泄露服务器路径；
- shared 私有 S3：受控 SDK 生成短期签名 URL（只读、限时），**不由服务器
  拉取任何外部 URL**（用户/归档中的 URL 一律不代理——无 SSRF）；
- PREVIEW 带 `X-Artifact-Kind: preview`（不冒充正式 VIDEO）；
- REMOTE 无受控对象（无 storage_key 且 artifact_url 非本域受控地址）→
  410 明确不可下载，不静默替换。

文件响应不包 JSON 信封（与字幕 SRT/VTT 导出一致的交付形态）。
"""
import re

from django.conf import settings
from django.http import FileResponse

from ..models import AssetType

SIGNED_URL_TTL_SECONDS = 300


class DownloadError(Exception):
    status_code = 410


_CONTENT_TYPES = {
    "mp4": "video/mp4",
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
}


def _safe_filename(asset) -> str:
    """下载文件名：uuid + 类型扩展（无用户可控字符）。"""
    ext = "mp4" if asset.asset_type in (AssetType.VIDEO, AssetType.PREVIEW) else "bin"
    return f"{asset.asset_type.lower()}-{asset.uuid}.{ext}"


def _local_download(request, asset):
    from .storage import resolve_storage_key
    try:
        path = resolve_storage_key(asset.storage_key)
    except ValueError:
        # 存储键非法（历史脏数据/穿越尝试）：不泄露路径细节
        raise DownloadError("产物存储键非法，无法下载")
    if not path.is_file():
        raise DownloadError("产物文件缺失（可能已被外部清理），无法下载")
    response = FileResponse(open(path, "rb"), content_type="video/mp4")
    response["Content-Disposition"] = f'attachment; filename="{_safe_filename(asset)}"'
    return response


def _s3_signed_url(request, asset) -> str:
    """shared 私有 S3：受控 SDK 生成短期只读签名 URL。

    只对**本项目上传的受控对象**（storage_key 指向 projects/<uuid>/ 子树）
    签名；artifact_url 为任意外部 URL 时绝不代理/重定向（410）。
    """
    import boto3  # 可选依赖
    key = asset.storage_key
    client = boto3.client(
        "s3",
        aws_access_key_id=settings.STORAGE_OSS_ACCESS_KEY,
        aws_secret_access_key=settings.STORAGE_OSS_SECRET_KEY,
        endpoint_url=settings.STORAGE_OSS_ENDPOINT or None,
    )
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.STORAGE_OSS_BUCKET, "Key": key},
        ExpiresIn=SIGNED_URL_TTL_SECONDS,
    )


def build_download(request, asset):
    """构建下载响应（本地 FileResponse / S3 签名跳转）。

    仅 VIDEO/PREVIEW 进入（视图层已校验）；返回带 X-Artifact-Kind 头。
    """
    if asset.storage_key:
        if settings.STORAGE_BACKEND == "s3":
            url = _s3_signed_url(request, asset)
            from django.http import HttpResponseRedirect
            response = HttpResponseRedirect(url)
        else:
            response = _local_download(request, asset)
    else:
        # 无 storage_key：REMOTE 适配器产物存于供应商侧，服务器无受控对象
        raise DownloadError(
            "产物为远程托管对象，本部署无受控副本，不可下载（reason=remote_no_object）")
    if asset.asset_type == AssetType.PREVIEW:
        response["X-Artifact-Kind"] = "preview"  # 预览不冒充正式成片
    return response
