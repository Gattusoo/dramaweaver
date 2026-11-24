"""项目 ZIP 导出（工作台 Stage B2 任务 5.1，design D3/D6）。

导出以数据库当前状态即时生成（不依赖可能延迟的磁盘清单）：
- project.json（即时构建，schema_version=1，字段白名单）
- 声明的本地资产（storage_key 存在且文件在项目目录内才打包）
- MANIFEST.sha256（包内文件摘要清单，导入校验依据）

显式排除（design D3）：凭证 / cache / log / Agent 会话侧状态
（会话树、提案历史、技能库、评估指标、AgentCommunication）——
导出响应中明示 excluded，不静默发生。远程 URL 资产仅保存受控元数据，
不打包任何 URL 内容（离线下载须用户显式选择，任务 5.4）。
"""
import hashlib
import json
import zipfile

from django.core.files.base import ContentFile
from django.http import HttpResponse

from ..models import Asset, AssetStatus

from .manifest import build_manifest, _scrub
from .storage import project_dir, resolve_storage_key

# 导出包结构约定
PROJECT_JSON = "project.json"
CHECKSUM_NAME = "MANIFEST.sha256"
MAX_EXPORT_ASSETS = 2000  # 防御性上限（异常脏数据不拖垮导出）


def _export_assets(play):
    """声明导出的 READY 资产：本地文件存在且解析安全。"""
    assets = []
    for asset in (Asset.objects.filter(play=play, status=AssetStatus.READY)
                  .order_by("uuid")[:MAX_EXPORT_ASSETS]):
        entry = {"asset_uuid": str(asset.uuid), "storage_key": asset.storage_key,
                 "content_sha256": asset.content_sha256,
                 "artifact_url": asset.artifact_url, "remote": not bool(asset.storage_key)}
        if asset.storage_key:
            try:
                path = resolve_storage_key(asset.storage_key)
                if path.is_file():
                    entry["size"] = path.stat().st_size
                    assets.append(entry)
                # 文件缺失：不打包、保留元数据（remote=False 但标记 missing）
                else:
                    entry["missing"] = True
                    assets.append(entry)
            except ValueError:
                entry["invalid_key"] = True
                assets.append(entry)
        else:
            assets.append(entry)
    return assets


def export_manifest_payload(play) -> dict:
    """导出元信息（清单 + 资产声明 + 显式排除项）。"""
    m = build_manifest(play)
    return {
        "manifest": m,
        "assets": _export_assets(play),
        "excluded": [
            "agent_sessions", "proposal_history", "play_scoped_skills",
            "evaluation_metrics", "agent_communications", "cache", "logs",
            "credentials",
        ],
    }


def build_export_zip(play) -> tuple[bytes, dict]:
    """构建 ZIP（内存）：project.json + 声明资产 + MANIFEST.sha256。

    返回 (zip_bytes, summary)；summary 含 excluded 明示。
    """
    payload = export_manifest_payload(play)
    digests = {}
    buf = _BytesWriter()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest_data = json.dumps(
            payload["manifest"], ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")
        zf.writestr(PROJECT_JSON, manifest_data)
        digests[PROJECT_JSON] = hashlib.sha256(manifest_data).hexdigest()

        packed = 0
        skipped_remote = 0
        skipped_missing = 0
        for entry in payload["assets"]:
            key = entry.get("storage_key") or ""
            if not key:
                skipped_remote += 1
                continue
            if entry.get("missing") or entry.get("invalid_key"):
                skipped_missing += 1
                continue
            data = resolve_storage_key(key).read_bytes()
            zf.writestr(key, data)
            digests[key] = hashlib.sha256(data).hexdigest()
            packed += 1

        checksum_data = "\n".join(
            f"{v}  {k}" for k, v in sorted(digests.items())).encode("utf-8")
        zf.writestr(CHECKSUM_NAME, checksum_data)

    summary = {
        "project_uuid": str(play.uuid),
        "title": play.title,
        "packed_assets": packed,
        "skipped_remote": skipped_remote,
        "skipped_missing": skipped_missing,
        "excluded": payload["excluded"],
    }
    return buf.getvalue(), summary


class _BytesWriter:
    """极简内存流（zipfile 只需 write/flush/tell）。"""

    def __init__(self):
        self._parts = []

    def write(self, data):
        self._parts.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def tell(self):
        return sum(len(p) for p in self._parts)

    def getvalue(self) -> bytes:
        return b"".join(self._parts)


def export_response(play) -> HttpResponse:
    """导出 HTTP 响应（任务 5.1 流式语义：内存构建 + 单次写出）。"""
    zip_bytes, summary = build_export_zip(play)
    resp = HttpResponse(ContentFile(zip_bytes), content_type="application/zip")
    resp["Content-Disposition"] = (
        f'attachment; filename="project-{play.uuid}.zip"')
    # 排除明示（design D3：不静默发生）——摘要放响应头，前端可直接消费
    resp["X-Export-Excluded"] = ",".join(summary["excluded"])
    resp["X-Export-Packed-Assets"] = str(summary["packed_assets"])
    resp["X-Export-Skipped-Remote"] = str(summary["skipped_remote"])
    return resp
