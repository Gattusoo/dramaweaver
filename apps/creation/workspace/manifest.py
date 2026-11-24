"""项目清单 project.json（工作台 Stage B1，design D3）。

DB 是在线事实，清单是延迟快照。清单 schema_version=1，字段白名单构建，
资源以稳定 UUID 引用，绝不包含凭证 / token / 用量成本 / 绝对路径。

- build_manifest(play)：确定性构建器（同状态 → 同 JSON 字节）；
- write_manifest(play)：同 Play 串行（进程内锁）+ 临时文件 + fsync +
  os.replace 原子替换——中断/崩溃保留旧清单，永不产出半文件；
- mark_manifest_dirty / refresh_manifest：任务终态只标脏，延迟到
  下次保存/导出时统一刷新（避免生成高峰期频繁落盘）。
"""
import hashlib
import itertools
import json
import logging
import os
import threading

from ..models import Asset, AssetDependency, AssetStatus, Character, Episode

from .storage import project_dir

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# 原子写临时文件唯一后缀计数（并发安全：见 _atomic_write 注释）
_tmp_seq = itertools.count()

# 目录约定（任务 4.4，design D3）
CACHE_DIRNAME = "cache"
EXPORTS_DIRNAME = "exports"
LOGS_DIRNAME = "logs"

# 同 Play 串行锁表（进程内；local 单 worker 足够，shared 多进程下
# os.replace 仍是原子的，锁只避免同进程内交错写临时文件）
_locks_guard = threading.Lock()
_locks = {}

# 脏标记（任务 4.2 延迟刷新）：任务终态只标脏，不立即落盘
_dirty_guard = threading.Lock()
_dirty = set()


def _play_lock(play) -> threading.Lock:
    key = str(play.uuid)
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def mark_manifest_dirty(play) -> None:
    """任务终态/资产 READY 后标脏；实际刷新延迟到 refresh_manifest。

    接受 Play 实例、play_id 或 play uuid 字符串（任务终态埋点传 id，
    避免额外查库）。
    """
    key = str(getattr(play, "uuid", None) or play)
    with _dirty_guard:
        _dirty.add(key)


def refresh_manifest(play, *, force=False) -> bool:
    """仅在标脏（或 force）时重建并原子写入清单；返回是否实际写入。"""
    key = str(play.uuid)
    with _dirty_guard:
        dirty = key in _dirty
        if dirty:
            _dirty.discard(key)
    if not (dirty or force):
        return False
    write_manifest(play)
    return True


# ── 确定性构建器（任务 4.1：字段白名单，确定性排序）────────────────────────

_SHOT_FIELDS = (
    "shot_type", "visual_prompt", "dialogue", "action_note",
    "emotion_tag", "duration_estimate", "status",
)
_SCENE_FIELDS = ("location", "time_of_day", "description", "mood", "status")
_EPISODE_FIELDS = ("title", "synopsis", "status", "duration_seconds")


def _shot_payload(shot) -> dict:
    d = {"shot_number": shot.shot_number}
    d.update({f: getattr(shot, f) for f in _SHOT_FIELDS})
    d["dialogue_character"] = (
        str(shot.dialogue_character_id) if shot.dialogue_character_id else None)
    d["sound_effects"] = shot.sound_effects or []
    return d


def _scene_payload(scene) -> dict:
    d = {"scene_number": scene.scene_number}
    d.update({f: getattr(scene, f) for f in _SCENE_FIELDS})
    d["shots"] = [_shot_payload(s) for s in scene.shots.order_by("shot_number")]
    return d


def _episode_payload(episode) -> dict:
    d = {"episode_number": episode.episode_number}
    d.update({f: getattr(episode, f) for f in _EPISODE_FIELDS})
    d["scenes"] = [_scene_payload(s) for s in episode.scenes.order_by("scene_number")]
    return d


# 敏感键黑名单（任务 4.1）：generation_spec 等结构化字段进入清单前
# 递归剔除（值也可能泄漏——整对删除）
_SENSITIVE_KEYS = ("api_key", "apikey", "token", "secret", "password", "credential")


def _scrub(value):
    """递归剔除敏感键（不信任上游结构化字段的键名）。"""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()
                if not any(s in str(k).lower() for s in _SENSITIVE_KEYS)}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _asset_payload(asset) -> dict:
    """资产条目：UUID 引用 + 声明的相对资源键（storage_key），无绝对路径、
    无成本/用量；generation_spec 保留（不含凭证），供迁移复用。"""
    return {
        "uuid": str(asset.uuid),
        "type": asset.asset_type,
        "version": asset.version,
        "status": asset.status,
        "episode_number": asset.episode.episode_number if asset.episode_id else None,
        "scene_number": asset.scene.scene_number if asset.scene_id else None,
        "shot_number": asset.shot.shot_number if asset.shot_id else None,
        "character": (f"char-{asset.character_id}" if asset.character_id else None),
        "storage_key": asset.storage_key or "",
        "artifact_url": asset.artifact_url or "",  # 远程资产受控元数据（任务 5.4）
        "content_sha256": asset.content_sha256 or "",
        "fingerprint": asset.input_fingerprint or "",
        "generation_spec": _scrub(asset.generation_spec) if asset.generation_spec else None,
        "prompt": asset.prompt or "",
        "parameters": _scrub(asset.parameters) if asset.parameters else {},
    }


def build_manifest(play) -> dict:
    """确定性构建 project.json 内容（同 DB 状态 → 同 JSON 字节）。

    白名单字段 + 排序遍历；不含凭证/token/成本/绝对路径/Agent 会话侧
    状态（design D3 明示排除）。
    """
    episodes = [ep for ep in Episode.objects.filter(play=play)
                .order_by("episode_number")
                .prefetch_related("scenes__shots")]
    characters = Character.objects.filter(play=play).order_by("name", "pk")
    assets = (Asset.objects.filter(play=play, status=AssetStatus.READY)
              .select_related("episode", "scene", "shot", "character")
              .order_by("uuid"))
    edges = list(AssetDependency.objects.filter(
        upstream__play=play).select_related("upstream", "downstream")
        .order_by("upstream__uuid", "downstream__uuid"))
    return {
        "schema_version": SCHEMA_VERSION,
        "project": {
            "uuid": str(play.uuid),
            "title": play.title,
            "synopsis": play.synopsis,
            "genre": play.genre,
            "total_episodes": play.total_episodes,
            "status": play.status,
            "audit_mode": play.audit_mode,
            "redo_granularity": play.redo_granularity,
            "consistency_mode": play.consistency_mode,
            "gen_params": play.gen_params or {},
            "created_at": play.created_at.isoformat(),
            "updated_at": play.updated_at.isoformat(),
        },
        "characters": [{
            "uuid": f"char-{c.pk}",
            "name": c.name,
            "role": c.role,
            "description": c.description,
            "appearance_prompt": c.appearance_prompt,
        } for c in characters],
        "episodes": [_episode_payload(ep) for ep in episodes],
        "assets": [_asset_payload(a) for a in assets],
        "dependencies": [{
            "upstream": str(e.upstream.uuid),
            "downstream": str(e.downstream.uuid),
            "relation_type": e.relation_type,
        } for e in edges],
    }


def manifest_bytes(play) -> bytes:
    """规范化 JSON 字节（确定性：sort_keys + 紧凑分隔符）。"""
    return json.dumps(
        build_manifest(play), ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")


def manifest_digest(play) -> str:
    """清单内容摘要（判重依据，任务 4.3）。"""
    return hashlib.sha256(manifest_bytes(play)).hexdigest()


# ── 原子写入（任务 4.2）───────────────────────────────────────────────────

def _atomic_write(path, data: bytes) -> None:
    """临时文件 + fsync + os.replace 原子替换（中断保留旧文件）。

    并发安全：临时文件名带唯一后缀（线程 id + 单调计数），同一目标并发写时
    各线程独立 tmp——共享固定 tmp 名会在"A open → B open 截断 → A replace"
    交错下把写一半的内容原子化为正式文件（并发清单刷新竞态，测试抓出）。
    """
    tmp = path.with_name(f".{path.name}.tmp.{threading.get_ident():x}.{next(_tmp_seq)}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_manifest(play) -> str:
    """同 Play 串行、原子写入 project.json；返回内容摘要。"""
    d = project_dir(play)
    for sub in (EXPORTS_DIRNAME, CACHE_DIRNAME, LOGS_DIRNAME):
        (d / sub).mkdir(exist_ok=True)
    with _play_lock(play):
        data = manifest_bytes(play)
        _atomic_write(d / "project.json", data)
    return hashlib.sha256(data).hexdigest()
