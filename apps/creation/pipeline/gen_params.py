"""统一创作参数解析层（工作台 Stage A1，design D4）。

有效参数按优先级解析：任务显式参数 > Play.gen_params > AgentConfig.style_params > 系统默认。
产出两个东西：
- effective（dict）：进入生成逻辑的最终参数（如 aspect_ratio/style 的映射结果）；
- unsupported_params（list[str]）：请求携带但本变更未接线的保留字段（create_type/duration/count），
  逐个明示"未参与生成"，不静默吞掉。

Stage A1 只接通已支持的两项：
- aspect_ratio：传入图像适配器参数（gateway payload → adapter parameters）并进入
  合成画布 _canvas（16:9 竖剧 → 1080x608 类横画布）；
- style：映射到配方 style_params 中已支持的风格/调色配置（style → style_params.grade），
  配方未支持该风格时返回明确提示，不猜测。

指纹/规格快照（incremental-generation）在 Stage A2 落地，本模块先提供解析与回显。
"""
import logging

logger = logging.getLogger(__name__)

# 支持的画面比例 → (宽, 高)（与合成模块 W/H 分辨率解耦：9:16 保持既有 1080x1920，
# 16:9 用 1920x1080，其余常见比例按整偶数对齐）
ASPECT_RATIOS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:3": (1440, 1080),
    "3:4": (1080, 1440),
}

# 保留字段：请求可携带但当前未接入生成逻辑（不猜测影响分集数量/时长），
# 解析结果中明示返回，不静默宣称已生效
RESERVED_PARAMS = ("create_type", "duration", "count")

# Play.gen_params / 任务覆盖中本层识别的键
_KNOWN_TOP_KEYS = ("aspect_ratio", "style")


def _clean(value):
    """字符串参数去空白；非字符串原样返回。"""
    return value.strip() if isinstance(value, str) else value


def resolve_gen_params(play, config=None, task_params=None):
    """解析有效创作参数。

    Args:
        play: Play 实例（读 play.gen_params）
        config: AgentConfig 实例（读 config.style_params；None 时跳过该层）
        task_params: 任务显式覆盖（dict；最高优先级）

    Returns:
        {
          "effective": {"aspect_ratio": "9:16", ...},
          "style_mapping": {...} | None,   # style 命中的配方风格配置（原样键值）
          "unsupported_params": ["create_type", ...],  # 携带但未接线的保留字段
          "sources": {"aspect_ratio": "play|task|default", ...},  # 各键来源（回显/排查）
        }

    设计要点（design D4 / spec production/asset-generation）：
    - 缺省（三层都没有 aspect_ratio）时返回空 effective——调用方保持既有行为，
      不把默认值硬塞进 payload（避免改动 9:16 时代的历史行为）。
    - style 只映射到配方已有键：style="cinematic" → style_params.grade.cinematic 存在
      才生效；配方没配任何风格时 style 返回 unsupported（明确提示，不猜映射）。
    - create_type/duration/count 不在 KNOWN 键内：携带即进 unsupported_params。
    """
    play_params = (getattr(play, "gen_params", None) or {}) if play is not None else {}
    config_params = (getattr(config, "style_params", None) or {}) if config is not None else {}
    task_params = dict(task_params or {})

    effective = {}
    sources = {}
    unsupported = []

    # aspect_ratio：task > play > config.style_params.aspect_ratio（配方可给基线，Play 覆盖）
    ar = task_params.get("aspect_ratio")
    ar_source = "task"
    if ar is None:
        ar = play_params.get("aspect_ratio")
        ar_source = "play"
    if ar is None:
        ar = config_params.get("aspect_ratio")
        ar_source = "config"
    if ar is not None:
        ar = _clean(ar)
        if ar in ASPECT_RATIOS:
            effective["aspect_ratio"] = ar
            sources["aspect_ratio"] = ar_source
        else:
            # 值非法（如 "21:9"）：不静默丢弃——进 unsupported 提示，不猜测近似比例
            unsupported.append("aspect_ratio")

    # style：仅当配方配置了该风格（style_params.grade.<name>）时映射生效
    style = task_params.get("style")
    style_source = "task"
    if style is None:
        style = play_params.get("style")
        style_source = "play"
    style_mapping = None
    if style is not None:
        style = _clean(style)
        grade = config_params.get("grade") if isinstance(config_params.get("grade"), dict) else {}
        if isinstance(style, str) and style in grade:
            effective["style"] = style
            sources["style"] = style_source
            style_mapping = {"grade": grade[style]}
        else:
            unsupported.append("style")

    # 保留字段：携带即明示（无论值是什么）
    for key in RESERVED_PARAMS:
        if key in task_params or (isinstance(play_params, dict) and key in play_params):
            unsupported.append(key)

    return {
        "effective": effective,
        "style_mapping": style_mapping,
        "unsupported_params": unsupported,
        "sources": sources,
    }


def aspect_ratio_dims(aspect_ratio):
    """画面比例 → (w, h)；未知比例返回 None（调用方保持既有默认画布）。"""
    return ASPECT_RATIOS.get(_clean(aspect_ratio)) if aspect_ratio else None
