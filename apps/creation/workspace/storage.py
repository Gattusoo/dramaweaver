"""项目工作区存储布局（工作台 Stage A2 任务 2.6，design D3）。

目录结构（LOCAL_DATA_DIR 或 shared 的 MEDIA_ROOT/projects 双轨兼容）：

    <root>/projects/<play_uuid>/
      project.json              # Stage B 落地（清单服务）
      assets/sha256/<ab>/<digest>.<ext>   # 原始资产，内容寻址
      exports/                   # 导出 ZIP（Stage B2）
      cache/                     # 可整体删除
      logs/

本模块（A2）只提供：
- project_dir(play)：项目目录解析（自动创建）；
- save_asset_bytes(play, data, ext)：内容寻址保存（同项目同内容不互相覆盖、
  重名文件安全），返回 storage_key（受控相对键）与 artifact_url（/media/...
  相对地址——shared 的 Django 静态服务与既有前端读取路径不变）；
- resolve_storage_key(storage_key)：storage_key → 本地绝对路径（路径逃逸拒绝）。

shared 兼容：无 LOCAL_DATA_DIR（shared 模式）时根取 MEDIA_ROOT/projects，
artifact_url 仍走 /media/ 前缀（nginx/Django 直接服务的既有行为）；local 模式
根取 LOCAL_DATA_DIR，artifact_url 同样返回 /media/ 前缀相对地址（Stage C 的
本地静态服务接线后同样可读）。旧 artifact_url 不回填 storage_key——历史资产
保留原 URL（legacy），只有新写入的资产带 storage_key。
"""
import hashlib
import re
from pathlib import Path

from django.conf import settings

_EXT_WHITELIST = {"png", "jpg", "jpeg", "mp3", "mp4", "wav", "srt", "vtt", "json", "txt"}
_KEY_RE = re.compile(r"^projects/[0-9a-f-]{36}/assets/sha256/[0-9a-f]{2}/[0-9a-f]{64}\.[a-z0-9]+$")


def _root() -> Path:
    """存储根：local 模式取 LOCAL_DATA_DIR，shared 取 MEDIA_ROOT（projects 子树）。"""
    local_dir = getattr(settings, "LOCAL_DATA_DIR", "") or ""
    base = Path(local_dir).expanduser() if (local_dir and getattr(settings, "IS_LOCAL_MODE", False)) \
        else Path(settings.MEDIA_ROOT)
    return base


def project_dir(play) -> Path:
    """项目目录：projects/<play_uuid>/（幂等创建）。"""
    d = _root() / "projects" / str(play.uuid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def asset_ext_for(asset_type: str) -> str:
    """资产类型 → 内容寻址扩展名。"""
    return {
        "IMAGE": "png", "CHARACTER_REF": "png",
        "AUDIO": "mp3", "BGM": "mp3", "SFX": "mp3",
        "VIDEO": "mp4", "PREVIEW": "mp4",
        "SUBTITLE": "srt",
    }.get(str(asset_type), "bin")


def save_asset_bytes(play, data: bytes, asset_type: str):
    """内容寻址保存：projects/<uuid>/assets/sha256/<ab>/<digest>.<ext>。

    返回 (storage_key, artifact_url, content_sha256)。
    同项目同内容 → 同路径（幂等覆盖即同字节，不互相覆盖不同内容）；
    重名文件安全（文件名即内容摘要，无用户可控字符）。
    artifact_url 保持 /media/ 前缀相对地址：shared 由 Django/nginx 服务；
    local 由 Stage C 的本地静态接线服务（无 LOCAL_DATA_DIR 时文件落
    MEDIA_ROOT 下，/media/ 直读；local 模式的 LOCAL_DATA_DIR 在 Stage C
    接静态路由，A2 阶段先落库 URL 与键）。
    """
    digest = hashlib.sha256(data if isinstance(data, bytes) else data.read()).hexdigest()
    ext = asset_ext_for(asset_type)
    rel = f"projects/{play.uuid}/assets/sha256/{digest[:2]}/{digest}.{ext}"
    dest = _root() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data if isinstance(data, bytes) else data.read())
    return rel, f"{settings.MEDIA_URL}{rel}", digest


def resolve_storage_key(storage_key: str) -> Path:
    """storage_key → 本地绝对路径。非法键（穿越/绝对路径/非受控前缀）抛 ValueError。"""
    if not storage_key or not _KEY_RE.match(storage_key):
        raise ValueError(f"非法存储键: {storage_key!r}")
    p = (_root() / storage_key).resolve()
    root = _root().resolve()
    if root != p and root not in p.parents:
        raise ValueError(f"存储键越界: {storage_key!r}")
    return p
