"""项目目录约定与缓存清理（工作台 Stage B1 任务 4.4，design D3）。

目录约定（storage.py project_dir 之下）：
    projects/<play_uuid>/
      project.json
      assets/sha256/...   # 原始资产（内容寻址，绝不清）
      exports/            # 导出 ZIP（B2 落地）
      cache/              # 可整体删除
      logs/

缓存清理只作用于本项目 cache/ 子树；越剧目录 / 路径逃逸拒绝。
"""
import shutil

from .manifest import CACHE_DIRNAME
from .storage import project_dir

_CACHE_ALLOWED = {CACHE_DIRNAME}  # 仅 cache 可清理


class CleanupError(Exception):
    status_code = 400


def clear_cache(play, target=CACHE_DIRNAME) -> dict:
    """清理项目缓存目录（默认 cache/）；返回统计。

    target 仅接受 "cache"（白名单）；路径拼接后再次校验仍在项目目录内
    （双保险防逃逸）。原始资产 / 剧本版本 / 导出成片不受影响。
    """
    if target not in _CACHE_ALLOWED:
        raise CleanupError(f"仅支持清理 cache 目录，当前请求: {target!r}")
    project = project_dir(play)
    cache_dir = (project / target).resolve()
    if project.resolve() not in cache_dir.parents:
        raise CleanupError("目标路径越出项目目录，拒绝清理")
    removed = 0
    freed_bytes = 0
    if cache_dir.exists():
        for entry in cache_dir.iterdir():
            if entry.is_file():
                freed_bytes += entry.stat().st_size
                removed += 1
            elif entry.is_dir():
                for f in entry.rglob("*"):
                    if f.is_file():
                        freed_bytes += f.stat().st_size
                        removed += 1
        shutil.rmtree(cache_dir, ignore_errors=False)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return {"cleared": target, "files_removed": removed, "bytes_freed": freed_bytes}
