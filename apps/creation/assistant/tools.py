"""助手工具集（assistant/session 工具隔离，D4）。

- play 一律取自会话，模型传入的任何 play 标识都会被 schema 校验丢弃
- 入参按 JSON schema 校验（项目未依赖 jsonschema，此处实现所需子集）
- 写类工具只生成提案（proposals.create_proposal），不直接修改业务数据
"""
import logging

from django.db import transaction
from django.db.models import Count as models_Count

from ..models import (
    Asset, AssetStatus, AssetType, CharacterRole, Episode, Scene, Shot, ShotType, TaskRecord,
)
# 流水线感知（Phase 3.5.3）用到；模块级导入避免工具内重复 import
from ..models import TaskType
from . import proposals as P
from .models import ProposalKind

logger = logging.getLogger(__name__)

MAX_TEXT = 50000


class ToolError(Exception):
    """返回给模型的工具错误（tool_result is_error=true）。"""


class SchemaValidationError(ToolError):
    """入参 JSON schema 校验失败（区别于 handler 内的业务 ToolError）。

    resilience 层据此判定为 VALIDATION 类错误：先尝试参数自动修复，不盲目重试。
    """


# ── schema 校验（子集：object/string/integer/array、required、enum、maxLength、
#    minimum、additionalProperties=false 时丢弃未声明字段）────────────────────

def validate(schema, value, path="input"):
    t = schema.get("type")
    if isinstance(t, list):  # 类型列表，如 ["integer", "null"]
        if value is None and "null" in t:
            return None
        errors = []
        for option in t:
            if option == "null":
                continue
            try:
                return validate({**schema, "type": option}, value, path)
            except SchemaValidationError as exc:
                errors.append(str(exc))
        raise SchemaValidationError(errors[0] if errors else f"{path} 类型不正确")
    if t == "boolean":
        if not isinstance(value, bool):
            raise SchemaValidationError(f"{path} 应为布尔值")
        return value
    if t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SchemaValidationError(f"{path} 应为数字")
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            raise SchemaValidationError(f"{path} 超出范围")
        return value
    if t == "object":
        if not isinstance(value, dict):
            raise SchemaValidationError(f"{path} 应为对象")
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value or value[key] is None:
                raise SchemaValidationError(f"缺少必填参数 {path}.{key}")
        out = {}
        for key, v in value.items():
            if key in props:
                out[key] = validate(props[key], v, f"{path}.{key}")
            elif isinstance(schema.get("additionalProperties"), dict):
                out[key] = validate(schema["additionalProperties"], v, f"{path}.{key}")
            elif schema.get("additionalProperties") is True:
                out[key] = v  # 自由键透传（如写设定的类型化字段，键集合由 handler 校验）
        if "minProperties" in schema and len(out) < schema["minProperties"]:
            raise SchemaValidationError(f"{path} 至少需要 {schema['minProperties']} 个字段")
        return out
    if t == "string":
        if not isinstance(value, str):
            raise SchemaValidationError(f"{path} 应为字符串")
        if len(value) > schema.get("maxLength", MAX_TEXT):
            raise SchemaValidationError(f"{path} 过长")
        if "enum" in schema and value not in schema["enum"]:
            raise SchemaValidationError(f"{path} 取值应为 {', '.join(schema['enum'])}")
        return value
    if t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaValidationError(f"{path} 应为整数")
        # 未声明上下界时按 64 位有符号整数兜底，超大 id 直接拒绝而不是打到数据库
        lo = schema.get("minimum", -(2 ** 63))
        hi = schema.get("maximum", 2 ** 63 - 1)
        if value < lo:
            raise SchemaValidationError(f"{path} 不能小于 {lo}")
        if value > hi:
            raise SchemaValidationError(f"{path} 不能大于 {hi}")
        return value
    if t == "array":
        if not isinstance(value, list):
            raise SchemaValidationError(f"{path} 应为数组")
        return [validate(schema.get("items", {}), v, f"{path}[{i}]") for i, v in enumerate(value)]
    return value


# ── 注册表 ──────────────────────────────────────────────────────────────────

_TOOLS = {}


def tool(name, description, properties=None, required=None, fallback=None, **extra):
    """注册一个工具。

    fallback: 可选备用工具名。主工具发生 VALIDATION / PERMANENT 错误且无法修复时，
    resilience 层会尝试该备用工具（防环，见 resilience.run_tool_resilient）。
    """
    schema = {"type": "object", "properties": properties or {}, "required": required or [], **extra}

    def deco(fn):
        _TOOLS[name] = {"name": name, "description": description, "input_schema": schema,
                        "handler": fn, "fallback": fallback}
        return fn
    return deco


def get_tool(name):
    """按名取工具 spec（含 handler / fallback）；不存在返回 None。"""
    return _TOOLS.get(name)


def tool_specs():
    # handler / fallback 是内部字段，不发给模型
    from django.conf import settings as _dj_settings
    specs = [{k: v for k, v in t.items() if k not in ("handler", "fallback")}
             for t in _TOOLS.values()]
    # registry D7：开关关闭时 chain_tools 不进工具面（行为与现状一致）
    if not getattr(_dj_settings, "ENABLE_TOOL_REGISTRY", False):
        specs = [s for s in specs if s["name"] != "chain_tools"]
    # 市场侧 D4：web_fetch 仅在注册表开启且出站白名单非空时注册（fail-closed）
    if not _web_fetch_enabled():
        specs = [s for s in specs if s["name"] != "web_fetch"]
    # 灵感库（Phase 3.5.1）：开关关闭时三工具零暴露
    if not _inspiration_enabled():
        specs = [s for s in specs if s["name"] not in
                 ("inspiration_save", "inspiration_search", "inspiration_recommend")]
    # layered-memory D3：开关关闭时结构化记忆工具不进工具面
    if not getattr(_dj_settings, "ENABLE_LAYERED_MEMORY", False):
        specs = [s for s in specs if s["name"] not in ("memory_record_save", "memory_record_search")]
    return specs


class ToolContext:
    def __init__(self, session, message=None):
        self.session = session
        self.play = session.play
        self.message = message
        self.proposals = []
        self.questions = None  # ask_user 调用后非空：本轮结束等待用户


def run_tool(ctx, name, raw_input):
    """执行工具 -> (result_dict, is_error)。工具异常不外抛，以错误结果返回给模型。"""
    spec = _TOOLS.get(name)
    if spec is None:
        result, is_error = _run_registry_tool(ctx, name, raw_input)
        if result is not None or not is_error:
            return result, is_error
        return {"error": f"未知工具 {name}"}, True
    try:
        args = validate(spec["input_schema"], raw_input if raw_input is not None else {})
        # savepoint：单个工具的数据库错误只回滚自己，不毁掉整轮（及外层事务）
        with transaction.atomic():
            return spec["handler"](ctx, **args), False
    except ToolError as exc:
        return {"error": str(exc)}, True
    except Exception:  # noqa: BLE001 — 任何工具异常都应作为 is_error 结果回给模型
        logger.exception("助手工具 %s 执行失败", name)
        return {"error": f"工具 {name} 执行失败，请换个方式或稍后重试"}, True


def _run_registry_tool(ctx, name, raw_input):
    """注册表工具回退（registry D2）。返回 (result, is_error)；
    工具不存在时返回 (None, True) 由调用方按未知工具处理（不泄露存在性）。
    """
    from . import registry
    if not registry._enabled():
        return None, True
    record = registry.get_registry_tool(name, getattr(ctx, "user", None)
                                        or getattr(ctx.session, "user", None))
    if record is None:
        return None, True
    try:
        args = validate(record.input_schema if isinstance(record.input_schema, dict)
                        else {"type": "object", "properties": {}},
                        raw_input if raw_input is not None else {})
    except SchemaValidationError as exc:
        return {"error": str(exc)}, True
    try:
        return registry.run_in_sandbox(record, args), False
    except registry.SandboxError as exc:
        return {"error": f"工具 {name} 执行失败：{exc}"}, True
    except Exception:  # noqa: BLE001
        logger.exception("注册表工具 %s 执行失败", name)
        return {"error": f"工具 {name} 执行失败，请换个方式或稍后重试"}, True


_ID = {"type": "integer", "minimum": 1}
_TEXT = {"type": "string", "maxLength": MAX_TEXT}
_SHORT = {"type": "string", "maxLength": 200}
_SUMMARY = {"type": "string", "maxLength": 2000, "description": "向用户说明改了什么、为什么"}


def _episode(ctx, number):
    ep = Episode.objects.filter(play=ctx.play, episode_number=number).first()
    if ep is None:
        raise ToolError(f"第 {number} 集不存在")
    return ep


def _scene(ctx, scene_id):
    scene = Scene.objects.filter(pk=scene_id, episode__play=ctx.play).select_related("episode").first()
    if scene is None:
        raise ToolError(f"场景 {scene_id} 未找到")
    return scene


def _shot(ctx, shot_id):
    shot = Shot.objects.filter(pk=shot_id, scene__episode__play=ctx.play).select_related("scene__episode").first()
    if shot is None:
        raise ToolError(f"镜头 {shot_id} 未找到")
    return shot


def _asset_view(asset):
    if asset is None:
        return None
    return {"asset_id": asset.pk, "version": asset.version, "text": (asset.metadata or {}).get("text", "")}


# ── 只读工具 ────────────────────────────────────────────────────────────────

@tool("get_play_overview", "短剧概览：标题、类型、状态、分集、角色、当前审核检查点")
def get_play_overview(ctx):
    play = ctx.play
    return {
        "title": play.title, "genre": play.genre, "synopsis": play.synopsis, "status": play.status,
        "checkpoint": play.status if play.status.startswith("PAUSED_AT_") else None,
        "episodes": [{"episode_number": e.episode_number, "title": e.title, "status": e.status}
                     for e in play.episodes.order_by("episode_number")],
        "characters": [{"name": c.name, "role": c.role, "description": c.description}
                       for c in play.characters.all()],
    }


@tool("get_outline", "最新大纲文本（含 asset_id 与版本号）")
def get_outline(ctx):
    return {"outline": _asset_view(P.latest_text_asset(ctx.play, AssetType.OUTLINE))}


@tool("get_episode_script", "某集剧本文本与场景列表", {"episode_number": _ID}, ["episode_number"])
def get_episode_script(ctx, episode_number):
    ep = _episode(ctx, episode_number)
    return {
        "episode_number": ep.episode_number, "title": ep.title,
        "script": _asset_view(P.latest_text_asset(ctx.play, AssetType.EPISODE_SCRIPT, ep)),
        "scenes": [{"scene_id": s.pk, "scene_number": s.scene_number, "location": s.location,
                    "time_of_day": s.time_of_day, "description": s.description, "mood": s.mood,
                    "shot_count": s.shots.count()} for s in ep.scenes.order_by("scene_number")],
    }


@tool("get_scene", "场景字段与镜头列表", {"scene_id": _ID}, ["scene_id"])
def get_scene(ctx, scene_id):
    scene = _scene(ctx, scene_id)
    return {
        "scene_id": scene.pk, "episode_number": scene.episode.episode_number,
        **P.snapshot(scene, P.SCENE_FIELDS), "scene_number": scene.scene_number,
        "shots": [{"shot_id": s.pk, "shot_number": s.shot_number, "shot_type": s.shot_type,
                   **P.snapshot(s, P.SHOT_FIELDS), "duration_estimate": s.duration_estimate}
                  for s in scene.shots.order_by("shot_number")],
    }


@tool("get_task_status", "最近 10 条生产任务状态")
def get_task_status(ctx):
    records = TaskRecord.objects.filter(play=ctx.play).order_by("-created_at")[:10]
    return {"tasks": [{"id": r.pk, "type": r.task_type, "status": r.status,
                       "error": (r.error_message or "")[:200], "created_at": r.created_at.isoformat()}
                      for r in records]}


# ── 流水线感知（Phase 3.5.3 轨道 2）：agent 循环内的状态快照与下一步建议 ──────

_PLAY_STATUS_LABELS = {
    "DRAFT": "草稿", "OUTLINING": "生成大纲中", "SCRIPTING": "分集剧本中",
    "STORYBOARDING": "分镜中", "ASSET_GEN": "素材生成中", "COMPOSING": "合成中",
    "COMPLETED": "已完成", "PUBLISHED": "已发布",
    "PAUSED_AT_OUTLINE": "大纲审核暂停", "PAUSED_AT_SCRIPT": "分集剧本审核暂停",
    "PAUSED_AT_ASSETS": "素材审核暂停",
}

# 下一步建议规则（与 pipeline.state_machine.TRANSITIONS 合法转换一致；描述性，不执行）
_NEXT_STEP_RULES = {
    "DRAFT": "尚未开始：从大纲生成开始（可直接对话让助手产出大纲提案）",
    "OUTLINING": "大纲生成中：等待生成任务完成后进入大纲审核",
    "PAUSED_AT_OUTLINE": "大纲审核暂停：审核通过后进入分集剧本阶段",
    "SCRIPTING": "分集剧本生成中：等待全部剧本完成后进入审核",
    "PAUSED_AT_SCRIPT": "剧本审核暂停：审核通过后进入分镜阶段",
    "STORYBOARDING": "分镜生成中：等待分镜完成后进入素材生成",
    "ASSET_GEN": "素材生成中：等待全部素材就绪后可进入合成",
    "PAUSED_AT_ASSETS": "素材审核暂停：审核通过后进入合成阶段",
    "COMPOSING": "视频合成中：等待合成完成",
    "COMPLETED": "已完成：可发布，或通过助手提案逐镜头/逐场景重做",
    "PUBLISHED": "已发布（终态）：可生成渠道版本或衍生品",
}


@tool("pipeline_status", "查看本剧流水线状态快照：当前阶段、各分集进度与素材就绪度、"
      "待审核项、进行中任务计数。回答\"现在到哪了\"类问题先用本工具")
def pipeline_status(ctx):
    """Phase 3.5.3（design D1）：纯只读聚合——不调 summary_play_status（有写副作用）。"""
    from ..models import EpisodeStatus, TaskStatus
    play = ctx.play
    # 各分集：状态 + 素材就绪度（READY / 总素材，排除 SUPERSEDED）
    episodes = []
    for ep in play.episodes.order_by("episode_number"):
        assets = ep.assets.exclude(status=AssetStatus.SUPERSEDED)
        total = assets.count()
        ready = assets.filter(status=AssetStatus.READY).count()
        episodes.append({
            "episode_number": ep.episode_number,
            "status": ep.status,
            "status_label": ep.get_status_display(),
            "asset_ready": ready,
            "asset_total": total,
        })
    # 待审核：暂停态 + 该阶段非废弃素材计数（就绪/待生成/失败都属"本次审核范围内"）
    paused = play.status.startswith("PAUSED_")
    reviewable = (play.assets.exclude(status=AssetStatus.SUPERSEDED).count()
                  if paused else 0)
    # 进行中任务：非终态，ASSISTANT_TURN 不算生产流水线（对话任务始终存在）
    active_rows = list(
        TaskRecord.objects.filter(
            play=play,
            status__in=[TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRYING],
        ).exclude(task_type=TaskType.ASSISTANT_TURN).values_list("task_type").annotate(
            n=models_Count("pk")))
    active = {k: n for k, n in active_rows}  # values_list 扁平元组：('IMAGE_GEN', 3)
    recovery = TaskRecord.objects.filter(
        play=play, status=TaskStatus.RECOVERY_REQUIRED).count()
    return {
        "play_status": play.status,
        "play_status_label": _PLAY_STATUS_LABELS.get(play.status, play.status),
        "episode_progress": episodes,
        "pending_reviews": {"paused": paused, "reviewable_assets": reviewable},
        "active_tasks": active,
        "recovery_required": recovery,
    }


@tool("pipeline_next_steps", "获取本剧流水线的下一步建议（基于当前阶段的合法流转）。"
      "建议是描述性的：实际操作仍需通过审核按钮或助手提案执行")
def pipeline_next_steps(ctx):
    """Phase 3.5.3（design D2）：状态机驱动的建议规则表，纯只读不执行。"""
    from ..models import AssetStatus, TaskStatus
    play = ctx.play
    base = _NEXT_STEP_RULES.get(play.status)
    if base is None:
        return {"next_suggestions": [],
                "note": f"当前状态 {play.status}，无建议（未知状态）"}
    suggestions = [{"source_status": play.status, "suggestion": base}]
    paused = play.status.startswith("PAUSED_")
    if paused:
        reviewable = play.assets.exclude(status=AssetStatus.SUPERSEDED).count()
        suggestions[0]["hint"] = f"待审核范围约 {reviewable} 项素材/资产，通过审核后流程继续"
    if TaskRecord.objects.filter(play=play, status=TaskStatus.RECOVERY_REQUIRED).exists():
        suggestions.append({
            "source_status": play.status,
            "suggestion": "存在等待人工确认恢复的任务（付费请求中断），请先在任务记录中处理恢复",
            "hint": "恢复令牌流程见任务记录端点",
        })
    return {"next_suggestions": suggestions}


@tool("get_cost_summary", "本剧费用汇总")
def get_cost_summary(ctx):
    from apps.distribution.analytics import play_cost_breakdown
    return play_cost_breakdown(ctx.play)


# ── 提案工具（只生成提案）────────────────────────────────────────────────────

def _proposed(ctx, proposal):
    ctx.proposals.append(proposal)
    return {"proposal_id": proposal.pk, "status": proposal.status,
            "impacted_asset_count": len(proposal.impacted_asset_ids),
            "note": "已生成修改提案，需用户确认后才会生效；请勿声称已经修改"}


@tool("propose_outline_edit", "提议改写大纲全文", {"text": _TEXT, "summary": _SUMMARY}, ["text", "summary"])
def propose_outline_edit(ctx, text, summary):
    current = P.latest_text_asset(ctx.play, AssetType.OUTLINE)
    if current is None:
        raise ToolError("本剧尚无大纲，无法改写")
    return _proposed(ctx, P.create_proposal(
        ctx.session, ctx.message, ProposalKind.OUTLINE, payload={"text": text}, summary=summary,
        base_version=current.version, target_asset=current))


_SCENE_EDIT = {"type": "object", "properties": {
    "scene_number": _ID, "description": _TEXT, "mood": _SHORT, "location": _SHORT,
    "time_of_day": {"type": "string", "enum": list(P.TIME_OF_DAY)},
}, "required": ["scene_number"]}


@tool("propose_episode_script_edit",
      "提议改写某集剧本文本和/或已有场景字段（不能新增或删除场景）。多镜头改写传 batch_shot_descriptions 走批量合并（BATCH_GENERATION_ENABLED 开启时），各镜头仍拆为独立提案",
      {"episode_number": _ID, "text": _TEXT, "scenes": {"type": "array", "items": _SCENE_EDIT},
       "batch_shot_descriptions": {"type": "array", "items": _TEXT,
                                   "description": "批量镜头描述列表（各条为该镜头的改写要求）"},
       "summary": _SUMMARY}, ["episode_number", "summary"])
def propose_episode_script_edit(ctx, episode_number, summary, text=None, scenes=None,
                                batch_shot_descriptions=None):
    ep = _episode(ctx, episode_number)
    existing = set(ep.scenes.values_list("scene_number", flat=True))
    edits = {}
    for item in scenes or []:
        number = item.pop("scene_number")
        if number not in existing:
            raise ToolError(f"第 {episode_number} 集没有场景 {number}（只能修改已有场景）")
        if item:
            edits[str(number)] = {**edits.get(str(number), {}), **item}
    batch_meta = None
    batch_text = None
    if batch_shot_descriptions:
        # 批量合并（主计划 3.3.7 / design D4）：开关关闭时按普通单条处理
        from . import batching as B
        if B.batch_enabled():
            batches = B.plan_batch(batch_shot_descriptions)
            batch_text = "\n\n".join(B.batch_prompt(b, goal=summary) for b in batches)
            batch_meta = B.batch_metadata(len(batches), len(batch_shot_descriptions))
        elif text is None and not edits:
            # 开关关闭且无其他内容：降级为拼接单条
            batch_text = "\n".join(str(d) for d in batch_shot_descriptions)
    if text is None and batch_text is not None:
        text = batch_text
    if text is None and not edits:
        raise ToolError("text 与 scenes 至少提供一项")
    current = P.latest_text_asset(ctx.play, AssetType.EPISODE_SCRIPT, ep)
    payload = {**({"text": text} if text is not None else {}), **({"scenes": edits} if edits else {})}
    if batch_meta is not None:
        payload["batch_info"] = batch_meta
    snapshots = P._scene_snapshots(ep, edits) if edits else {}
    return _proposed(ctx, P.create_proposal(
        ctx.session, ctx.message, ProposalKind.EPISODE_SCRIPT, payload=payload, summary=summary,
        base_version=current.version if current else None, target_asset=current, episode=ep,
        base_snapshot={"scenes": snapshots} if snapshots is not None else {}))


@tool("propose_scene_edit", "提议修改场景字段",
      {"scene_id": _ID, "description": _TEXT, "mood": _SHORT, "location": _SHORT,
       "time_of_day": {"type": "string", "enum": list(P.TIME_OF_DAY)}, "summary": _SUMMARY},
      ["scene_id", "summary"])
def propose_scene_edit(ctx, scene_id, summary, **fields):
    scene = _scene(ctx, scene_id)
    if not fields:
        raise ToolError("至少修改一个字段：" + ", ".join(P.SCENE_FIELDS))
    return _proposed(ctx, P.create_proposal(
        ctx.session, ctx.message, ProposalKind.SCENE, payload=fields, summary=summary,
        base_snapshot=P.snapshot(scene, fields.keys()), episode=scene.episode, scene=scene))


_SFX_ITEM = {"type": "object", "properties": {
    "library_id": _ID, "url": {"type": "string", "maxLength": 1000},
    "offset": {"type": "number", "minimum": 0, "maximum": 600},
    "volume": {"type": "number", "minimum": 0, "maximum": 2},
    "duration": {"type": "number", "minimum": 0, "maximum": 600},
}}
_SHOT_PROPS = {
    "dialogue": _TEXT, "visual_prompt": _TEXT, "action_note": _TEXT,
    "emotion_tag": {"type": "string", "maxLength": 100},
    "dialogue_character_id": {"type": ["integer", "null"], "minimum": 1, "description": "本剧角色 id，null 表示旁白"},
    "shot_type": {"type": "string", "enum": list(ShotType.values)},
    "duration_estimate": {"type": "integer", "minimum": 0, "maximum": 600},
    "sound_effects": {"type": "array", "items": _SFX_ITEM, "description": "镜头音效，引用素材库 SFX 或 URL"},
}


def _clean_shot_fields(ctx, fields):
    """镜头字段：对白角色必须属于本剧；音效规范化（非法条目丢弃）。"""
    if fields.get("dialogue_character_id") is not None and not ctx.play.characters.filter(
            pk=fields["dialogue_character_id"]).exists():
        raise ToolError(f"角色 {fields['dialogue_character_id']} 不属于本剧")
    if "sound_effects" in fields:
        from apps.core.utils import media
        fields["sound_effects"] = media.normalize_sound_effects(fields["sound_effects"])
    return fields


@tool("propose_shot_edit", "提议修改镜头的对白、对白角色、画面描述、动作、情绪、景别、时长或音效",
      {"shot_id": _ID, **_SHOT_PROPS, "summary": _SUMMARY}, ["shot_id", "summary"])
def propose_shot_edit(ctx, shot_id, summary, **fields):
    shot = _shot(ctx, shot_id)
    if not fields:
        raise ToolError("至少修改一个字段：" + ", ".join(P.SHOT_FIELDS))
    fields = _clean_shot_fields(ctx, fields)
    return _proposed(ctx, P.create_proposal(
        ctx.session, ctx.message, ProposalKind.SHOT, payload=fields, summary=summary,
        base_snapshot=P.snapshot(shot, fields.keys()), episode=shot.scene.episode,
        scene=shot.scene, shot=shot))


# ── 结构提案（D1）──────────────────────────────────────────────────────────

@tool("propose_shot_insert", "提议在场景中插入一个新镜头（after_shot_number=0 表示插到最前）；后续镜头编号顺延",
      {"scene_id": _ID, "after_shot_number": {"type": "integer", "minimum": 0}, **_SHOT_PROPS,
       "summary": _SUMMARY}, ["scene_id", "after_shot_number", "visual_prompt", "summary"])
def propose_shot_insert(ctx, scene_id, after_shot_number, summary, **fields):
    scene = _scene(ctx, scene_id)
    last = scene.shots.order_by("-shot_number").values_list("shot_number", flat=True).first() or 0
    if after_shot_number > last:
        raise ToolError(f"场景只有 {last} 个镜头，after_shot_number 不能大于 {last}")
    fields = _clean_shot_fields(ctx, fields)
    return _proposed(ctx, P.create_proposal(
        ctx.session, ctx.message, ProposalKind.SHOT_CREATE, summary=summary,
        payload={"after_shot_number": after_shot_number, **fields},
        base_snapshot={"shot_ids": P.shot_ids(scene)}, episode=scene.episode, scene=scene))


@tool("propose_shot_delete", "提议删除镜头；其素材会标记失效但保留历史，后续镜头编号前移",
      {"shot_id": _ID, "summary": _SUMMARY}, ["shot_id", "summary"])
def propose_shot_delete(ctx, shot_id, summary):
    shot = _shot(ctx, shot_id)
    fields = P.snapshot(shot, P.SHOT_FIELDS)
    return _proposed(ctx, P.create_proposal(
        ctx.session, ctx.message, ProposalKind.SHOT_DELETE, summary=summary,
        payload={"shot_number": shot.shot_number, "scene_number": shot.scene.scene_number, **fields},
        base_snapshot={"shot_ids": P.shot_ids(shot.scene), "fields": fields},
        episode=shot.scene.episode, scene=shot.scene, shot=shot))


@tool("propose_scene_create", "提议在分集末尾新增一个场景",
      {"episode_number": _ID, "location": _SHORT, "description": _TEXT, "mood": _SHORT,
       "time_of_day": {"type": "string", "enum": list(P.TIME_OF_DAY)}, "summary": _SUMMARY},
      ["episode_number", "description", "summary"])
def propose_scene_create(ctx, episode_number, summary, **fields):
    ep = _episode(ctx, episode_number)
    return _proposed(ctx, P.create_proposal(
        ctx.session, ctx.message, ProposalKind.SCENE_CREATE, payload=fields, summary=summary,
        base_snapshot={"scene_ids": P.scene_ids(ep)}, episode=ep))


# ── 角色（D3）──────────────────────────────────────────────────────────────

_CHAR_PROPS = {"name": {"type": "string", "maxLength": 100},
               "role": {"type": "string", "enum": list(CharacterRole.values)},
               "description": _TEXT, "appearance_prompt": _TEXT}


def _character(ctx, character_id):
    c = ctx.play.characters.filter(pk=character_id).first()
    if c is None:
        raise ToolError(f"角色 {character_id} 未找到")
    return c


@tool("get_characters", "角色表：性格、外貌描述、是否有定妆照、声线与出场镜头数")
def get_characters(ctx):
    refs = set(Asset.objects.filter(play=ctx.play, asset_type=AssetType.CHARACTER_REF,
                                    status=AssetStatus.READY).values_list("character_id", flat=True))
    return {"characters": [
        {"character_id": c.pk, **P.snapshot(c, P.CHARACTER_FIELDS), "has_reference_image": c.pk in refs,
         "voice": c.voice_profile.name if c.voice_profile else None,
         "dialogue_shot_count": Shot.objects.filter(dialogue_character=c).count()}
        for c in ctx.play.characters.select_related("voice_profile").order_by("pk")]}


@tool("propose_character_edit", "提议修改角色的名字、定位、性格背景或外貌描述",
      {"character_id": _ID, **_CHAR_PROPS, "summary": _SUMMARY}, ["character_id", "summary"])
def propose_character_edit(ctx, character_id, summary, **fields):
    c = _character(ctx, character_id)
    if not fields:
        raise ToolError("至少修改一个字段：" + ", ".join(P.CHARACTER_FIELDS))
    if "name" in fields and ctx.play.characters.filter(name=fields["name"]).exclude(pk=c.pk).exists():
        raise ToolError(f"本剧已有角色「{fields['name']}」")
    return _proposed(ctx, P.create_proposal(
        ctx.session, ctx.message, ProposalKind.CHARACTER_EDIT, payload=fields, summary=summary,
        base_snapshot=P.snapshot(c, fields.keys()), character=c))


@tool("propose_character_create", "提议新增角色",
      {**_CHAR_PROPS, "summary": _SUMMARY}, ["name", "role", "summary"])
def propose_character_create(ctx, summary, **fields):
    if ctx.play.characters.filter(name=fields["name"]).exists():
        raise ToolError(f"本剧已有角色「{fields['name']}」")
    return _proposed(ctx, P.create_proposal(
        ctx.session, ctx.message, ProposalKind.CHARACTER_CREATE, payload=fields, summary=summary))


# ── 查询（D8）──────────────────────────────────────────────────────────────

@tool("get_shot", "镜头详情：全部字段、音效，以及图片 / 配音 / 分镜素材的最新版本与状态",
      {"shot_id": _ID}, ["shot_id"])
def get_shot(ctx, shot_id):
    shot = _shot(ctx, shot_id)
    assets = {}
    for t in (AssetType.IMAGE, AssetType.AUDIO, AssetType.STORYBOARD):
        a = Asset.objects.filter(shot=shot, asset_type=t).order_by("-version").first()
        assets[t.lower()] = {"asset_id": a.pk, "version": a.version, "status": a.status} if a else None
    return {"shot_id": shot.pk, "episode_number": shot.scene.episode.episode_number,
            "scene_id": shot.scene_id, "scene_number": shot.scene.scene_number, "shot_number": shot.shot_number,
            **P.snapshot(shot, P.SHOT_FIELDS), "assets": assets}


@tool("search_asset_library", "检索素材库（BGM、音效、片头片尾、背景图），返回名称、类型与 URL",
      {"query": {"type": "string", "maxLength": 100},
       "asset_type": {"type": "string", "enum": ["BGM", "SFX", "INTRO", "OUTRO", "BG_IMAGE", "TRANSITION"]},
       "limit": {"type": "integer", "minimum": 1, "maximum": 20}})
def search_asset_library(ctx, query="", asset_type=None, limit=10):
    from django.db.models import Q

    from ..models import AssetLibrary
    qs = AssetLibrary.objects.filter(Q(is_public=True) | Q(owner=ctx.session.user))
    if asset_type:
        qs = qs.filter(asset_type=asset_type)
    if query:
        qs = qs.filter(name__icontains=query)
    return {"items": [{"library_id": a.pk, "name": a.name, "asset_type": a.asset_type, "url": a.file_url,
                       "mood": (a.metadata or {}).get("mood"), "tags": (a.metadata or {}).get("tags")}
                      for a in qs.order_by("name")[:limit]]}


# ── 生成请求（D4）──────────────────────────────────────────────────────────

@tool("request_generation", "请求重新生成（会产生费用，需用户确认后才执行）：redo_shot 重做镜头图片与配音，"
      "redo_scene 重做场景下所有镜头，preview_episode 生成整集低分辨率预览",
      {"action": {"type": "string", "enum": list(P.GENERATION_ACTIONS)}, "target_id": _ID,
       "summary": _SUMMARY}, ["action", "target_id", "summary"])
def request_generation(ctx, action, target_id, summary):
    if action == "redo_shot":
        target = _shot(ctx, target_id)
        kw = dict(episode=target.scene.episode, scene=target.scene, shot=target)
    elif action == "redo_scene":
        target = _scene(ctx, target_id)
        kw = dict(episode=target.episode, scene=target)
    else:
        target = Episode.objects.filter(pk=target_id, play=ctx.play).first()
        if target is None:
            raise ToolError(f"分集 {target_id} 未找到（preview_episode 的 target_id 为分集 id）")
        kw = dict(episode=target)
    return _proposed(ctx, P.create_proposal(
        ctx.session, ctx.message, ProposalKind.GENERATION, summary=summary,
        payload={"action": action, "target_id": target_id, "estimated_tasks": P.estimate_tasks(action, target)},
        **kw))


# ── 审查（D5）/ 提问（D6）/ 记忆（D7）──────────────────────────────────────

@tool("review_episode", "检查某集的结构与素材问题（确定性审查）；"
      "当 ENABLE_AGENT_REVIEW 开启时同时附 LLM 主观审查（剧情逻辑/对白/节奏/情感）",
      {"episode_number": _ID}, ["episode_number"])
def review_episode(ctx, episode_number):
    from .review import review_episode as run_review
    config = ctx.session.agent_config or ctx.play.agent_config
    ep = _episode(ctx, episode_number)
    det = run_review(ep, (config.style_params or {}) if config else {})
    # Phase 1.8.3：合并主观审查（失败静默降级为纯确定性结果）
    from django.conf import settings
    if getattr(settings, "ENABLE_AGENT_REVIEW", False):
        try:
            from .agent_review import merge_reviews, review_episode_llm
            # 主观审查 LLM 费用归因到本轮记录（message.task_record），入账/预算可见
            record = getattr(ctx.message, "task_record_id", None) and ctx.message.task_record
            sub = review_episode_llm(ctx.session, ep, task_record=record)
            det = merge_reviews(det, sub)
        except Exception:  # noqa: BLE001 — 主观审查失败不阻断确定性结果
            import logging
            logging.getLogger(__name__).exception("主观审查失败 ep=%s", episode_number)
    return det


_QUESTION = {"type": "object", "properties": {
    "id": {"type": "string", "maxLength": 50}, "prompt": {"type": "string", "maxLength": 500},
    "type": {"type": "string", "enum": ["text", "single", "multi"]},
    "options": {"type": "array", "items": {"type": "string", "maxLength": 100}},
    "required": {"type": "boolean"},
}, "required": ["id", "prompt", "type"]}


@tool("ask_user", "信息不足时向用户提出结构化问题；调用后本轮结束，等待用户回答",
      {"questions": {"type": "array", "items": _QUESTION}}, ["questions"])
def ask_user(ctx, questions):
    if not 1 <= len(questions) <= 12:
        raise ToolError("问题数量应为 1-12 个")
    for q in questions:
        opts = q.get("options") or []
        if q["type"] != "text" and not 2 <= len(opts) <= 8:
            raise ToolError(f"选择题「{q['id']}」需要 2-8 个选项")
    ctx.questions = questions
    return {"status": "awaiting_user"}


@tool("memory_read", "读取本剧的长期记忆及其版本号。注意：项目记忆已在系统提示中自动加载，"
      "通常无需再调用此工具，除非需要获取最新版本号或确认内容未被他人修改")
def memory_read(ctx):
    from .project_memory import memory_read as _read
    return _read(ctx.play)


@tool("memory_save", "保存本剧的长期记忆（完整 Markdown，合并后整体保存）；必须带最近一次 memory_read 的 revision。"
      "项目记忆已在系统提示中自动加载，修改前先调用 memory_read 获取最新版本号",
      {"content": {"type": "string", "maxLength": 20000, "x-keep": "full",
                   "description": "记忆全文；超长会直接报错，不会自动截断"},
       "revision": {"type": "string", "maxLength": 64}},
      ["content", "revision"])
def memory_save(ctx, content, revision):
    from .project_memory import MemoryConflict, memory_save_tool
    try:
        return memory_save_tool(ctx.play, content, revision, ctx.session.user)
    except MemoryConflict as exc:
        raise ToolError(str(exc)) from exc


@tool("memory_record_save", "保存一条结构化记忆条目（跨会话可检索）：用户确认的偏好、已确认的决策或稳定剧情事实。"
      "与 memory_save 分工：单条可检索的偏好或事实用本工具；需用户查看全文的主文档用 memory_save",
      {"kind": {"type": "string", "enum": ["PREFERENCE", "DECISION", "FACT"],
                "description": "PREFERENCE=用户确认偏好；DECISION=已确认决策；FACT=稳定事实"},
       "content": {"type": "string", "maxLength": 2000,
                   "description": "单条内容；同内容重复保存自动去重"},
       "importance": {"type": "number", "minimum": 0, "maximum": 1}},
      ["kind", "content"])
def memory_record_save(ctx, kind, content, importance=0.5):
    import hashlib
    from .memory_layers import enabled
    if not enabled():
        raise ToolError("结构化记忆未启用")
    from .models import MemoryRecord
    sha = hashlib.sha256(content.strip().encode("utf-8")).hexdigest()
    obj, created = MemoryRecord.objects.update_or_create(
        play=ctx.play, kind=kind, content_sha256=sha,
        defaults={"content": content.strip(), "importance": float(importance),
                  "source_session": ctx.session, "is_active": True})
    return {"id": obj.pk, "kind": obj.kind, "content": obj.content,
            "importance": obj.importance, "created": created,
            "detail": "已保存（重复内容仅刷新）" if not created else "已保存"}


@tool("memory_record_search", "按关键词检索本剧的结构化记忆条目（跨会话偏好/决策/事实），返回类型、内容与重要性",
      {"query": {"type": "string", "maxLength": 200, "description": "检索关键词"},
       "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
      ["query"])
def memory_record_search(ctx, query, limit=5):
    from .memory_layers import enabled, search_memory_records
    if not enabled():
        raise ToolError("结构化记忆未启用")
    records = search_memory_records(ctx.play, query, limit=int(limit))
    return {"records": [{"id": r.pk, "kind": r.kind, "content": r.content,
                         "importance": r.importance} for r in records],
            "count": len(records)}


@tool("list_skills", "列出可用的创作技能（Skills）：名称、描述、触发词；需要详细指引时用 load_skill 加载全文")
def list_skills_tool(ctx):
    from .skills import list_skills
    return {"skills": list_skills()}


@tool("load_skill", "加载指定Skill的完整指引（渐进式披露第二层）；返回 instructions + 关联工具",
      {"name": {"type": "string", "maxLength": 100, "description": "Skill名称"}}, ["name"])
def load_skill(ctx, name):
    from .skills import load_skill as _load
    try:
        return _load(name)
    except KeyError as exc:
        raise ToolError(f"Skill {name} 不存在") from exc


@tool("propose_multiple",
      "一次生成多个风格不同的剧本方案（同一备选组），供用户对比后选择其一应用；"
      "创意类改写任务可主动使用。会多次调用模型，产生多次费用",
      {"episode_number": _ID,
       "styles": {"type": "array", "maxItems": 3, "items": {"type": "object", "properties": {
           "pacing": {"type": "string", "enum": ["紧凑快切", "舒缓铺陈", "张弛交替"]},
           "tone": {"type": "string", "enum": ["冷峻悬疑", "情感细腻", "爽感直给"]},
           "structure": {"type": "string", "enum": ["单线推进", "双线交叉", "倒叙插叙"]},
           "ending": {"type": "string", "enum": ["反转收束", "悬念留钩", "情绪爆发"]}}}},
       "summary": _SUMMARY}, ["episode_number", "summary"])
def propose_multiple(ctx, episode_number, summary, styles=None):
    from .variants import propose_multiple as _propose
    _episode(ctx, episode_number)  # 先校验集数与权限
    try:
        result = _propose(ctx.session, ctx.message, episode_number, styles=styles,
                          record=ctx.message.task_record if ctx.message else None,
                          summary_prefix=summary)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    # 让本轮把新提案推给前端
    from .models import AssistantProposal
    ctx.proposals.extend(AssistantProposal.objects.filter(pk__in=[p["proposal_id"]
                                                                for p in result["proposals"]]))
    return result


@tool("analyze_generated_image",
      "分析本剧已生成的镜头图片：场景/人物/构图/问题，并对照分镜要求给出一致性分数与改进建议；"
      "视觉能力未启用或无视觉模型时返回 skipped",
      {"asset_id": _ID}, ["asset_id"])
def analyze_generated_image(ctx, asset_id):
    from .vision import VisionUnavailable, analyze_generated_image_tool
    try:
        return analyze_generated_image_tool(ctx, asset_id)
    except VisionUnavailable as exc:
        return {"skipped": True, "reason": str(exc)[:300], "needs_regeneration": False}


# ── 工具注册表链式调用（Phase 3.1 / registry D5）────────────────────────────

from django.conf import settings as _settings  # noqa: E402


def _chain_enabled():
    return bool(getattr(_settings, "ENABLE_TOOL_REGISTRY", False))


def _chain_max_steps():
    return int(getattr(_settings, "CHAIN_MAX_STEPS", 5))


@tool("chain_tools",
      "按顺序串联执行多个注册表自定义工具（至多 5 步）：每一步收到上一步结果（prev 键），"
      "任一步失败返回已完成的部分结果与失败位置。步骤只能用自定义注册表工具",
      {"chain": {"type": "array", "minItems": 1, "maxItems": 5, "items": {"type": "object",
           "properties": {"tool": {"type": "string", "maxLength": 50},
                          "args": {"type": "object", "additionalProperties": {}}},
           "required": ["tool"], "additionalProperties": False}},
       "stop_on_error": {"type": "boolean"}}, ["chain"])
def chain_tools(ctx, chain, stop_on_error=True):
    """链式调用（registry D5）。handler 返回 dict（失败信息也在 dict 里，
    is_error 语义由 dict 的 failed_at/error 键表达——模型需要看到部分结果）。"""
    from . import registry
    if not _chain_enabled():
        raise ToolError("工具注册表未启用")
    max_steps = _chain_max_steps()
    if len(chain) > max_steps:
        raise ToolError(f"链式调用最多 {max_steps} 步")
    seen = set()
    for step in chain:
        name = step.get("tool") or ""
        if name == "chain_tools":
            raise ToolError("链式步骤不能引用 chain_tools 自身")
        if name in seen:
            raise ToolError(f"链式步骤重复引用 {name}")
        seen.add(name)
    user = getattr(ctx, "user", None) or getattr(ctx.session, "user", None)
    results = []
    for i, step in enumerate(chain):
        name = step["tool"]
        record = registry.get_registry_tool(name, user)
        if record is None:
            return {"completed": results, "failed_at": i, "error": f"未知工具 {name}"}
        args = dict(step.get("args") or {})
        if "prev" in args:
            raise ToolError(f"步骤 {i} 的 args 不能预定义 prev 键")
        if results:
            args["prev"] = results[-1]
        try:
            result = registry.run_in_sandbox(record, args)
        except registry.SandboxError as exc:
            if stop_on_error:
                return {"completed": results, "failed_at": i, "error": str(exc)}
            results.append({"error": str(exc)})
            continue
        results.append(result)
    return {"completed": results, "steps": len(results)}


# ── web_fetch 官方网络工具（市场侧 design D4：父进程代理出口，fail-closed）──

def _web_fetch_enabled():
    """注册表开启 且 出站白名单非空（空白名单即工具不注册不执行）。"""
    from django.conf import settings as _dj_settings
    if not getattr(_dj_settings, "ENABLE_TOOL_REGISTRY", False):
        return False
    return bool(_web_fetch_hosts())


def _web_fetch_hosts() -> set:
    from django.conf import settings as _dj_settings
    raw = getattr(_dj_settings, "WEB_FETCH_ALLOWED_HOSTS", "") or ""
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = str(raw).split(",")
    return {h.strip().lower() for h in items if h.strip()}


def _web_fetch_timeout() -> float:
    return float(getattr(_settings, "WEB_FETCH_TIMEOUT_S", 8))


def _web_fetch_max_bytes() -> int:
    return int(getattr(_settings, "WEB_FETCH_MAX_BYTES", 65536))


@tool("web_fetch",
      "抓取白名单网站的文本内容（HTTP GET）。仅可访问部署配置 WEB_FETCH_ALLOWED_HOSTS "
      "中列出的域名；返回 {url, status, content_type, text, truncated}",
      {"url": {"type": "string", "maxLength": 2048},
       "max_chars": {"type": "integer", "minimum": 1, "maximum": 200000}},
      ["url"])
def web_fetch(ctx, url, max_chars=None):
    """官方网络工具（市场侧 D4）：出站链 = scheme → 域名白名单（精确 host）→
    check_url（拒内网/保留/metadata）→ 请求 + check_peer（DNS rebinding 防护）
    → 重定向逐跳重复全部校验（≤3 跳）→ 字节上限截断。

    失败一律 raise ToolError（确定性错误，PERMANENT 语义不重试）。
    沙箱保持零网络不变——本工具在父进程执行，不进沙箱。
    """
    import httpx
    from urllib.parse import urlsplit
    from apps.core.utils.url_safety import UnsafeURLError, check_peer, check_url

    if not _web_fetch_enabled():
        raise ToolError("web_fetch 未启用（需配置 WEB_FETCH_ALLOWED_HOSTS）")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ToolError("URL 非法")
    if parts.scheme not in ("http", "https"):
        raise ToolError("仅支持 http/https URL")
    host = (parts.hostname or "").lower()
    if not host:
        raise ToolError("URL 缺少主机名")
    if host not in _web_fetch_hosts():
        raise ToolError(f"域名 {host} 不在 web_fetch 白名单内")

    limit = _web_fetch_max_bytes()
    if isinstance(max_chars, int) and 0 < max_chars < limit:
        limit = max_chars
    current = url
    max_redirects = 3
    for _ in range(max_redirects + 1):
        check_url(current)  # 每一跳都拒内网/保留/云 metadata
        with httpx.Client(timeout=_web_fetch_timeout(), follow_redirects=False) as client:
            resp = client.get(current)
            stream = resp.extensions.get("network_stream")
            peer = None
            if stream is not None:
                try:
                    peer = stream.get_extra_info("server_addr")
                except Exception:  # noqa: BLE001 — httpx 后端差异兜底
                    peer = None
            check_peer((resp.url.host or "").lower(), peer)
            if resp.is_redirect:
                nxt = resp.next_request
                if nxt is None or not str(nxt.url):
                    raise ToolError("重定向缺少 Location")
                current = str(nxt.url)
                parts2 = urlsplit(current)
                if (parts2.hostname or "").lower() not in _web_fetch_hosts():
                    raise ToolError("重定向目标不在 web_fetch 白名单内")
                continue
            if resp.status_code >= 400:
                raise ToolError(f"目标返回 HTTP {resp.status_code}")
            text = resp.text or ""
            truncated = False
            if len(text.encode("utf-8")) > limit:
                # 按字符截断到字节数不超限（保守下界：每字符 ≤3 字节 UTF-8）
                text = text[: max(1, limit // 3)]
                truncated = True
            return {"url": str(resp.url), "status": resp.status_code,
                    "content_type": resp.headers.get("content-type", ""),
                    "text": text, "truncated": truncated}
    raise ToolError(f"重定向次数超过 {max_redirects}")


# ── 灵感库工具（Phase 3.5.1 / design D3，开关 ENABLE_INSPIRATION_LIBRARY）──

def _inspiration_enabled():
    from django.conf import settings as _dj_settings
    return bool(getattr(_dj_settings, "ENABLE_INSPIRATION_LIBRARY", False))


@tool("inspiration_save",
      "把好的创意灵感保存到灵感库（跨会话可检索的创作素材）。分类：plot_twist（剧情反转）/"
      "dialogue（对白）/scene（场景）/character（角色）/other（其他）",
      {"category": {"type": "string", "maxLength": 20},
       "content": {"type": "string", "maxLength": 2000},
       "rating": {"type": "integer", "minimum": 1, "maximum": 5}},
      ["category", "content"])
def inspiration_save(ctx, category, content, rating=3):
    """agent 保存灵感（design D3）：须在本剧会话上下文，source=agent。"""
    from . import inspiration as I
    if not _inspiration_enabled():
        raise ToolError("灵感库未启用")
    play = getattr(ctx, "play", None)
    if play is None:
        raise ToolError("灵感保存须在本剧会话中使用")
    user = getattr(ctx, "user", None) or getattr(ctx.session, "user", None)
    try:
        rec = I.save_inspiration(play=play, user=user, category=category,
                                 content=content, rating=rating,
                                 source=I.InspirationSource.AGENT)
    except I.InspirationError as exc:
        raise ToolError(str(exc))
    return {"saved": True, "id": rec.pk, "category": rec.category}


@tool("inspiration_search",
      "按关键词检索灵感库（全局+本剧合并，命中数与评分加权排序）。返回 id/分类/内容/评分",
      {"keywords": {"type": "string", "maxLength": 200},
       "category": {"type": "string", "maxLength": 20},
       "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
      ["keywords"])
def inspiration_search(ctx, keywords, category=None, limit=None):
    from . import inspiration as I
    if not _inspiration_enabled():
        raise ToolError("灵感库未启用")
    play = getattr(ctx, "play", None)
    user = getattr(ctx, "user", None) or getattr(ctx.session, "user", None)
    try:
        rows = I.search_inspirations(user=user, play=play, keywords=keywords,
                                     category=category, limit=limit)
    except I.InspirationError as exc:
        raise ToolError(str(exc))
    return {"results": [{"id": r.pk, "category": r.category,
                         "content": r.content, "rating": r.rating,
                         "used_count": r.used_count} for r in rows],
            "count": len(rows)}


@tool("inspiration_recommend",
      "根据当前创作内容推荐相关灵感（按文本关键词命中排序）；引用推荐结果时传 mark_used=true 计数",
      {"text": {"type": "string", "maxLength": 2000},
       "mark_used": {"type": "boolean"},
       "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
      [])
def inspiration_recommend(ctx, text=None, mark_used=False, limit=None):
    from . import inspiration as I
    if not _inspiration_enabled():
        raise ToolError("灵感库未启用")
    play = getattr(ctx, "play", None)
    if not (text or "").strip():
        text = _latest_user_text_of(ctx)
    try:
        rows = I.recommend_inspirations(play=play, text=text,
                                         mark_used=bool(mark_used), limit=limit)
    except I.InspirationError as exc:
        raise ToolError(str(exc))
    return {"recommendations": [{"id": r.pk, "category": r.category,
                                 "content": r.content, "rating": r.rating} for r in rows],
            "count": len(rows), "marked_used": bool(mark_used)}


def _latest_user_text_of(ctx):
    """推荐缺省文本：当前轮用户消息（无则空串——推荐返回空集）。"""
    session = getattr(ctx, "session", None)
    if session is None:
        return ""
    from .models import MessageRole
    msg = session.messages.filter(role=MessageRole.USER).order_by("-seq").first()
    return (msg.content[0]["text"] if msg and msg.content
            and isinstance(msg.content, list) and msg.content
            and isinstance(msg.content[0], dict) else "")


# ── 小说工作区工具（2026-11-23 组 1 / novel-workspace spec）──────────────────
# 读设定 / 写设定（提案）/ 检索前文：助手按需读取设定库与章节，写走提案闸。

_STORY_TYPES = ["PERSON", "FACTION", "SKILL", "FORESHADOWING", "LOCATION"]


@tool("story_asset_read",
      "按名称或类型检索设定库（人物/势力/技能/伏笔/地点五类故事资产）。返回 id/类型/名称/类型化字段/绑定角色/关联章节号",
      {"name": {"type": "string", "maxLength": 100},
       "type": {"type": "string", "enum": _STORY_TYPES},
       "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
      [])
def story_asset_read(ctx, name=None, type=None, limit=None):  # noqa: A002
    from apps.creation.models import StoryAsset
    play = getattr(ctx, "play", None)
    if play is None:
        raise ToolError("读设定须在本剧会话中使用")
    qs = StoryAsset.objects.filter(play=play)
    if type:
        qs = qs.filter(type=type)
    if name:
        qs = qs.filter(name__icontains=name)
    qs = qs.order_by("type", "name")[: (limit or 10)]
    return {"results": [
        {"id": a.pk, "type": a.type, "name": a.name, "fields": a.fields,
         "character_id": a.character_id,
         "chapter_numbers": [c.chapter_number for c in a.chapter_refs.all()]}
        for a in qs], "count": len(qs)}


@tool("story_asset_search_chapters",
      "检索前文：按关键词在小说章节正文中搜索。返回章节号/标题/命中文本片段（含上下文）",
      {"keywords": {"type": "string", "maxLength": 100},
       "from_chapter": {"type": "integer", "minimum": 1},
       "to_chapter": {"type": "integer", "minimum": 1},
       "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
      ["keywords"])
def story_asset_search_chapters(ctx, keywords, from_chapter=None, to_chapter=None, limit=None):
    from apps.creation.models import NovelChapter
    play = getattr(ctx, "play", None)
    if play is None:
        raise ToolError("检索前文须在本剧会话中使用")
    qs = NovelChapter.objects.filter(volume__novel__play=play, is_deleted=False)
    if from_chapter:
        qs = qs.filter(chapter_number__gte=from_chapter)
    if to_chapter:
        qs = qs.filter(chapter_number__lte=to_chapter)
    hits, window = [], 80
    for ch in qs.order_by("chapter_number"):
        idx = ch.content.find(keywords)
        while idx != -1 and len(hits) < (limit or 10):
            start, end = max(0, idx - window), min(len(ch.content), idx + len(keywords) + window)
            hits.append({"chapter_number": ch.chapter_number, "title": ch.title,
                         "fragment": ch.content[start:end]})
            idx = ch.content.find(keywords, idx + len(keywords))
        if len(hits) >= (limit or 10):
            break
    return {"results": hits, "count": len(hits)}


@tool("story_asset_propose",
      "新建或更新设定库中的故事资产（走提案：须用户确认后才落库）。action=new 新建（需 type/name/fields）；"
      "action=update 更新既有设定（需 story_asset_id，可改 name/fields）。fields 的键须符合该类型 schema"
      "（PERSON: identity/personality/appearance/goal/arc/notes；FACTION: purpose/members/strength/relations/notes；"
      "SKILL: effect/cost/limitation/owner/notes；FORESHADOWING: setup_chapter/payoff_plan/status/notes；"
      "LOCATION: description/atmosphere/geography/notes）",
      {"action": {"type": "string", "enum": ["new", "update"]},
       "story_asset_id": _ID,
       "type": {"type": "string", "enum": _STORY_TYPES},
       "name": _SHORT, "fields": {"type": "object", "additionalProperties": True}, "summary": _SHORT},
      ["action", "summary"])
def story_asset_propose(ctx, action, summary, story_asset_id=None, type=None,  # noqa: A002
                        name=None, fields=None):
    """写设定走提案闸（novel-workspace spec）：apply_proposal 复用全部校验落 StoryAsset。"""
    from apps.creation.models import STORY_ASSET_SCHEMAS, StoryAsset
    from . import proposals as P
    from .models import ProposalKind
    play = getattr(ctx, "play", None)
    if play is None:
        raise ToolError("写设定须在本剧会话中使用")
    fields = fields or {}
    if action == "new":
        if not type or not name:
            raise ToolError("action=new 需要 type 与 name")
        schema = STORY_ASSET_SCHEMAS.get(type)
        if schema is None:
            raise ToolError(f"未知设定类型 {type}")
        unknown = set(fields) - schema["allowed"]
        if unknown:
            raise ToolError(f"类型 {type} 不支持字段：{', '.join(sorted(unknown))}")
        missing = schema["required"] - set(fields)
        if missing:
            raise ToolError(f"类型 {type} 缺少必填字段：{', '.join(sorted(missing))}")
        payload = {"action": "new", "type": type, "name": name, "fields": fields}
    else:
        asset = StoryAsset.objects.filter(pk=story_asset_id, play=play).first()
        if asset is None:
            raise ToolError("目标设定不存在或不属于本剧")
        payload = {"action": "update", "type": asset.type, "story_asset_id": asset.pk}
        if name:
            payload["name"] = name
        if fields:
            schema = STORY_ASSET_SCHEMAS.get(asset.type, {})
            unknown = set(fields) - set(schema.get("allowed", set()))
            if unknown:
                raise ToolError(f"类型 {asset.type} 不支持字段：{', '.join(sorted(unknown))}")
            payload["fields"] = fields
    return _proposed(ctx, P.create_proposal(
        ctx.session, ctx.message, ProposalKind.STORY_ASSET, payload=payload, summary=summary))


__all__ = ["ToolContext", "ToolError", "SchemaValidationError", "run_tool", "tool_specs",
           "validate", "get_tool"]
