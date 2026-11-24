"""模型发现：填入 base_url + api_key 后拉取供应商支持的模型列表。

两类来源：
- live：LLM（OpenAI 兼容 / Claude）通过 GET {base_url}/models 实时拉取。
- static：IMAGE / TTS / VIDEO 多数供应商无标准列表端点，用 KNOWN_MODELS 静态清单。

始终允许手动填 model_name 兜底 —— 本模块只是“可选下拉”，不强制。
"""
import logging

from .adapters import get_api_key
from .http import safe_request

logger = logging.getLogger(__name__)

# 单个 provider 实时模型数量上限（防止异常供应商返回超大列表）
_LIVE_MODELS_CAP = 200


# adapter_type -> [{model_name, display_name, model_type, capabilities?, cost_config?}]
# 仅 IMAGE / TTS / VIDEO 需要；LLM 走 live 拉取。
KNOWN_MODELS = {
    # ── IMAGE ──
    "dall_e": [
        {"model_name": "dall-e-3", "display_name": "DALL·E 3", "model_type": "IMAGE",
         "cost_config": {"cost_per_image": 0.04}},
        {"model_name": "gpt-image-1", "display_name": "GPT Image 1", "model_type": "IMAGE",
         "cost_config": {"cost_per_image": 0.02}},
    ],
    "seedream": [
        {"model_name": "doubao-seedream-3-0-t2i", "display_name": "即梦 Seedream 3.0（文生图）",
         "model_type": "IMAGE"},
    ],
    "jimeng": [
        {"model_name": "doubao-seedream-3-0-t2i", "display_name": "即梦 Seedream 3.0（文生图）",
         "model_type": "IMAGE"},
    ],
    "custom_openai_image": [],
    "flux": [
        {"model_name": "flux-pro-1.1", "display_name": "Flux Pro 1.1", "model_type": "IMAGE"},
        {"model_name": "flux-dev", "display_name": "Flux Dev", "model_type": "IMAGE"},
        {"model_name": "flux-schnell", "display_name": "Flux Schnell", "model_type": "IMAGE"},
    ],
    # ── TTS ──
    "openai_tts": [
        {"model_name": "tts-1", "display_name": "TTS-1", "model_type": "TTS"},
        {"model_name": "tts-1-hd", "display_name": "TTS-1 HD", "model_type": "TTS"},
        {"model_name": "gpt-4o-mini-tts", "display_name": "GPT-4o mini TTS", "model_type": "TTS"},
    ],
    "volcengine_doubao": [
        {"model_name": "tts-1", "display_name": "火山豆包 TTS", "model_type": "TTS"},
    ],
    "minimax_tts": [
        {"model_name": "speech-01-hd", "display_name": "海螺 speech-01-hd", "model_type": "TTS"},
        {"model_name": "speech-01-turbo", "display_name": "海螺 speech-01-turbo", "model_type": "TTS"},
        {"model_name": "speech-02-hd", "display_name": "海螺 speech-02-hd", "model_type": "TTS"},
    ],
    "cosyvoice": [],
    "azure_tts": [],
    "edge_tts": [],
    "custom_openai_tts": [],
    # ── VIDEO ──
    "kling": [
        {"model_name": "kling-v1", "display_name": "可灵 V1", "model_type": "VIDEO"},
        {"model_name": "kling-v1-master", "display_name": "可灵 V1 Master", "model_type": "VIDEO"},
        {"model_name": "kling-v2", "display_name": "可灵 V2", "model_type": "VIDEO"},
    ],
    "hailuo": [
        {"model_name": "T2V-01", "display_name": "海螺 T2V-01", "model_type": "VIDEO"},
        {"model_name": "I2V-01-live", "display_name": "海螺 I2V-01-live", "model_type": "VIDEO"},
        {"model_name": "I2V-01", "display_name": "海螺 I2V-01", "model_type": "VIDEO"},
    ],
}


def _normalize_adapter_type(provider):
    return (getattr(provider, "adapter_type", "") or "").strip().lower()


def _live_openai_models(provider):
    """OpenAI 兼容 GET {base_url}/models -> [{model_name, display_name, model_type}]。"""
    base_url = (getattr(provider, "base_url", "") or "").rstrip("/")
    api_key = get_api_key(provider)
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = safe_request(provider, "GET", f"{base_url}/models", headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("data") or []
    out = []
    for item in data:
        mid = item.get("id")
        if mid:
            out.append({"model_name": mid, "display_name": mid, "model_type": "LLM"})
    out.sort(key=lambda m: m["model_name"])
    return out[:_LIVE_MODELS_CAP]


def _live_claude_models(provider):
    """Claude GET {base_url}/v1/models -> [{model_name, display_name, model_type}]。"""
    base_url = (getattr(provider, "base_url", "") or "").rstrip("/") or "https://api.anthropic.com"
    api_key = get_api_key(provider)
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    resp = safe_request(provider, "GET", f"{base_url}/v1/models", headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("data") or []
    out = []
    for item in data:
        mid = item.get("id")
        if mid:
            out.append({"model_name": mid,
                        "display_name": item.get("display_name") or mid,
                        "model_type": "LLM"})
    out.sort(key=lambda m: m["model_name"])
    return out[:_LIVE_MODELS_CAP]


def fetch_provider_models(provider):
    """返回 {source, models, existing}。

    source: "live"（实时拉取成功）| "static"（静态清单 / live 失败回退）。
    models: [{model_name, display_name, model_type, capabilities?, cost_config?}]。
    existing: 该 provider 下已导入的 model_name 列表，供 UI 标记。
    live 失败（网络/鉴权/不支持）一律回退 static，绝不抛 500。
    """
    adapter_type = _normalize_adapter_type(provider)
    provider_type = getattr(provider, "provider_type", "")
    source = "static"
    models = []

    if provider_type == "LLM":
        try:
            if adapter_type == "claude":
                models = _live_claude_models(provider)
            else:
                models = _live_openai_models(provider)
            source = "live"
        except Exception as exc:  # noqa: BLE001
            logger.warning("实时拉取模型失败（回退静态/空），provider=%s: %s",
                           getattr(provider, "pk", "?"), exc)
            models = []
            source = "static"

    if source == "static":
        models = [dict(m) for m in KNOWN_MODELS.get(adapter_type, [])]

    existing = _existing_model_names(provider)

    return {"source": source, "models": models, "existing": existing}


def _existing_model_names(provider):
    """已导入的 model_name 列表；provider 未保存（无 pk）时返回空。"""
    pk = getattr(provider, "pk", None)
    if pk is None or not hasattr(provider, "models"):
        return []
    try:
        return list(provider.models.values_list("model_name", flat=True))
    except Exception:  # noqa: BLE001
        return []


def import_provider_models(provider, model_names=None):
    """把选中的 model_names 批量建为 AIModel。返回 {imported, skipped}。

    model_names 为空时默认导入全部「尚未导入」的已知/实时模型。
    已存在的 model_name 跳过（幂等）。元数据（display_name/model_type/cost_config）
    优先取 KNOWN_MODELS，缺失时用 provider.provider_type 兜底。
    """
    from .models import AIModel

    info = fetch_provider_models(provider)
    known = {m["model_name"]: m for m in info["models"]}
    existing = set(info["existing"])

    seen, dup = set(), []
    if model_names is None:
        targets = [name for name in known if name not in existing]
    else:
        # 重复名去重（保序）：重复出现的计入 skipped，不虚增 imported
        targets = []
        for name in model_names:
            if name in existing:
                continue
            if name in seen:
                dup.append(name)
                continue
            seen.add(name)
            targets.append(name)

    skipped = [name for name in (model_names or []) if name in existing] + dup
    rows = []
    for name in targets:
        meta = known.get(name, {})
        rows.append(AIModel(
            provider=provider,
            model_name=name,
            display_name=meta.get("display_name") or name,
            model_type=meta.get("model_type") or getattr(provider, "provider_type", "LLM"),
            capabilities=meta.get("capabilities") or {},
            cost_config=meta.get("cost_config") or {},
            is_active=True,
        ))

    imported = []
    if rows:
        AIModel.objects.bulk_create(rows, ignore_conflicts=True)
        # bulk_create 后按 DB 实际对账：ignore_conflicts 静默丢弃的（如并发同名）不计入 imported，
        # 保证返回计数与实际插入行数一致
        created = set(AIModel.objects.filter(
            provider=provider, model_name__in=targets).values_list("model_name", flat=True))
        imported = [name for name in targets if name in created]
    return {"imported": imported, "skipped": skipped}
