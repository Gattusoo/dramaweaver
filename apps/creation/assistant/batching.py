"""批量提案生成（主计划 3.3.7 / design D4）。

把多条镜头描述合并为单次生成请求的规划层：`plan_batch(descriptions)` 输出
批次切分（单批 10 条上限），由调用方组装单次 prompt。只覆盖对白/描述类
**提案生成**；不触碰 IMAGE/TTS 派发管线（受理即计费不可回滚）。
受 `BATCH_GENERATION_ENABLED` 控制（默认 false），关闭时批量参数按普通单条处理。
"""
import logging

logger = logging.getLogger(__name__)

BATCH_SIZE = 10  # 单批镜头数上限（切分多批控制单次 prompt 长度）


def batch_enabled() -> bool:
    from django.conf import settings
    return bool(getattr(settings, "ENABLE_BATCH_GENERATION", False))


def plan_batch(descriptions) -> list[list[str]]:
    """多条镜头描述 → 批次列表（每批 ≤ BATCH_SIZE 条，保序切分）。

    空输入返回 []；开关由调用方判定（本函数纯规划，不查配置）。
    """
    items = [str(d).strip() for d in (descriptions or []) if str(d).strip()]
    if not items:
        return []
    return [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]


def batch_prompt(batch: list[str], goal: str = "") -> str:
    """单批 → 合并生成 prompt（编号清单）。"""
    lines = "\n".join(f"{i}. {d}" for i, d in enumerate(batch, 1))
    head = f"创作目标：{goal}\n\n" if goal else ""
    return (f"{head}以下是需要生成镜头描述的条目，请逐条输出对应的镜头描述"
            f"（保持编号对应，逐条以「镜头N」开头）：\n{lines}")


def batch_metadata(batch_count: int, shot_count: int) -> dict:
    """批量提案的元数据（进 proposal.payload.batch，报告端点消费）。"""
    return {"batch": True, "batch_count": batch_count, "batch_shot_count": shot_count}
