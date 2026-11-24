"""项目 ZIP 导入预检（工作台 Stage B2 任务 5.2，design D3）。

纯 Python 流式校验，DB/项目目录零写入；所有恶意归档（绝对路径/
穿越/重复路径/炸弹/摘要不匹配/不支持版本）在写入前失败并返回原因。

限额（可调）：
- MAX_ENTRIES 5000 个文件；MAX_TOTAL 2GB 解压总量；MAX_SINGLE 512MB
- MAX_RATIO 100 压缩比（ZIP 炸弹）
"""
import hashlib
import io
import json
import re
import zipfile

from .export_service import CHECKSUM_NAME, PROJECT_JSON
from .manifest import SCHEMA_VERSION

MAX_ENTRIES = 5000
MAX_TOTAL = 2 * 1024 * 1024 * 1024
MAX_SINGLE = 512 * 1024 * 1024
MAX_RATIO = 100

_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._\-]+(/[A-Za-z0-9._\-]+)*$")


class ImportError_(Exception):
    """导入失败（预检或恢复阶段）；args[0] 为原因（HTTP 400 体）。"""
    status_code = 400


def _safe_rel_path(name: str) -> bool:
    """合法相对路径：无 ..、无绝对/盘符、无反斜杠、字符白名单。"""
    if not name or "\\" in name or ":" in name:
        return False
    if name.startswith("/") or name != name.strip():
        return False
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    return bool(_SAFE_PATH_RE.match(name))


def _parse_checksums(data: bytes) -> dict:
    """MANIFEST.sha256（"<hex>  <path>" 每行）→ {path: hex}。"""
    out = {}
    for line in data.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise ImportError_(f"校验清单行格式非法: {line[:60]!r}")
        digest, path = parts
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ImportError_(f"校验摘要格式非法: {digest[:20]!r}")
        if not _safe_rel_path(path):
            raise ImportError_(f"校验清单路径非法: {path!r}")
        out[path] = digest
    return out


def precheck_import(fileobj) -> dict:
    """导入预检（只读）：返回结构化报告；任何失败抛 ImportError_。

    fileobj：已打开的二进制文件对象（Django UploadedFile 或 BytesIO）。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(fileobj.read()))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ImportError_(f"不是合法的 ZIP 归档: {exc}")

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ImportError_(f"包内文件数超限（{len(infos)} > {MAX_ENTRIES}）")
        names = [i.filename for i in infos]
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})[:5]
            raise ImportError_(f"包内存在重复路径: {dupes}")
        for info in infos:
            if not _safe_rel_path(info.filename):
                raise ImportError_(f"包内路径非法（穿越/绝对/特殊字符）: {info.filename!r}")
            if info.file_size > MAX_SINGLE:
                raise ImportError_(f"单文件解压大小超限: {info.filename}")
            if info.compress_size > 0 and info.file_size / info.compress_size > MAX_RATIO \
                    and info.file_size > 1024 * 1024:
                raise ImportError_(f"压缩比异常（疑似炸弹）: {info.filename}")
        total = sum(i.file_size for i in infos)
        if total > MAX_TOTAL:
            raise ImportError_(f"解压总大小超限（{total} > {MAX_TOTAL}）")

        if PROJECT_JSON not in names:
            raise ImportError_("缺少 project.json")
        try:
            manifest = json.loads(zf.read(PROJECT_JSON).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImportError_(f"project.json 解析失败: {exc}")
        sv = manifest.get("schema_version")
        if sv != SCHEMA_VERSION:
            raise ImportError_(
                f"不支持的清单版本 schema_version={sv}（当前支持 {SCHEMA_VERSION}）")
        project = manifest.get("project") or {}
        uuid_ = project.get("uuid")
        if not re.fullmatch(r"[0-9a-f-]{36}", str(uuid_ or "")):
            raise ImportError_("project.json 缺少合法的项目 uuid")
        if not project.get("title"):
            raise ImportError_("project.json 缺少项目标题")
        for field in ("episodes", "characters", "assets", "dependencies"):
            if field not in manifest:
                raise ImportError_(f"project.json 缺少必需字段: {field}")

        if CHECKSUM_NAME not in names:
            raise ImportError_("缺少 MANIFEST.sha256 校验清单")
        checksums = _parse_checksums(zf.read(CHECKSUM_NAME))

        # 逐文件流式摘要校验（读取时再次限量，不信 file_size 头）；
        # 校验清单本身不进摘要自校验；压缩比按实际解压字节流式判断，
        # 先于摘要比对（炸弹拦截优先）
        verified = 0
        for info in infos:
            name = info.filename
            if name == CHECKSUM_NAME:
                continue
            expected = checksums.get(name)
            h = hashlib.sha256()
            size = 0
            with zf.open(name) as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_SINGLE:
                        raise ImportError_(f"单文件解压大小超限（流式）: {name}")
                    h.update(chunk)
            ratio = size / info.compress_size if info.compress_size else 0
            if ratio > MAX_RATIO and size > 1024 * 1024:
                raise ImportError_(
                    f"压缩比异常（疑似炸弹）: {name} ratio={ratio:.0f}")
            if expected is None:
                raise ImportError_(f"校验清单缺少条目: {name}")
            if h.hexdigest() != expected:
                raise ImportError_(f"摘要不匹配: {name}")
            verified += 1

        assets = manifest.get("assets") or []
        declared = {a.get("storage_key") for a in assets if a.get("storage_key")}
        packed = {n for n in names if n not in (PROJECT_JSON, CHECKSUM_NAME)}
        missing = declared - packed
        if missing:
            raise ImportError_(
                f"清单声明的资产缺失: {sorted(missing)[:3]}")

        return {
            "project_uuid": uuid_,
            "title": project.get("title") or "",
            "schema_version": sv,
            "episodes": len(manifest.get("episodes") or []),
            "characters": len(manifest.get("characters") or []),
            "assets": len(assets),
            "dependencies": len(manifest.get("dependencies") or []),
            "files_verified": verified,
            "bytes_total": total,
        }
