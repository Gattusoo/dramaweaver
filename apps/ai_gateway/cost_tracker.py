"""成本计算与记录。

每次 AI 调用成功后根据 provider_type 和 usage 计算费用，写入 CostRecord，
并累加到关联的 TaskRecord.cost 与 Asset.cost。
"""
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


def calculate(provider_type, usage, cost_config):
    """根据 provider_type 计算一次调用的总费用（Decimal）。"""
    usage = usage or {}
    cost_config = cost_config or {}

    def _dec(value, default="0"):
        if value in (None, ""):
            value = default
        return Decimal(str(value))

    if provider_type == "LLM":
        input_tokens = _dec(usage.get("input_tokens", 0))
        output_tokens = _dec(usage.get("output_tokens", 0))
        input_per_1k = _dec(cost_config.get("input_per_1k", 0))
        output_per_1k = _dec(cost_config.get("output_per_1k", 0))
        return (input_tokens / Decimal(1000) * input_per_1k) + (
            output_tokens / Decimal(1000) * output_per_1k
        )
    if provider_type == "IMAGE":
        count = _dec(usage.get("image_count", 1))
        per_image = _dec(cost_config.get("cost_per_image", 0))
        return count * per_image
    if provider_type == "TTS":
        seconds = _dec(usage.get("audio_seconds", 0))
        per_second = _dec(cost_config.get("cost_per_second", 0))
        return seconds * per_second
    if provider_type == "VIDEO":
        seconds = _dec(usage.get("video_seconds", 0))
        per_second = _dec(cost_config.get("cost_per_second", 0))
        return seconds * per_second
    return Decimal("0")


def record_cost(
    *,
    play,
    task_record=None,
    provider,
    model_name,
    provider_type,
    usage,
    cost_config,
    currency="CNY",
    asset=None,
):
    """创建 CostRecord 并更新 TaskRecord.cost / Asset.cost。返回 CostRecord。"""
    from .models import CostRecord

    total = calculate(provider_type, usage, cost_config)
    record = CostRecord.objects.create(
        play=play,
        task_record=task_record,
        provider=provider,
        model=model_name,
        provider_type=provider_type,
        usage=usage or {},
        unit_cost=_unit_cost(provider_type, cost_config),
        total_cost=total,
        currency=currency,
    )
    if task_record is not None:
        task_record.cost = (task_record.cost or 0) + total
        task_record.save(update_fields=["cost"])
    if asset is not None:
        asset.cost = (asset.cost or 0) + total
        asset.save(update_fields=["cost"])
    logger.info(
        "CostRecord #%s created: %s %s = %s %s",
        record.pk, provider.name, model_name, total, currency,
    )
    return record


def _unit_cost(provider_type, cost_config):
    cost_config = cost_config or {}
    if provider_type == "LLM":
        return Decimal(str(cost_config.get("input_per_1k", 0)))
    if provider_type == "IMAGE":
        return Decimal(str(cost_config.get("cost_per_image", 0)))
    return Decimal(str(cost_config.get("cost_per_second", 0)))
