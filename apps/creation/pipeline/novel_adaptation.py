"""小说→剧本改编编排（novel-adaptation，2026-11-23 组 2）。

链路：章节范围 → 分块摘要（块级连贯）→ 提炼场景 → 拆分剧集 → 场次/对白/动作
生成（LLM）→ 落 Episode/Scene/Shot 结构（走既有提案闸——audit_mode 复用，
已有剧集保护禁止静默覆盖）。

断点续跑：块级幂等（块产出落 output_summary["blocks"]），失败块重试不重跑
已完成块。进度事件走助手事件流（session 粒度）+ TaskRecord 状态轮询。
"""
import json
import logging

from django.utils import timezone

from ..assistant import events
from ..assistant.proposals import create_proposal
from ..assistant.models import ProposalKind
from ..models import (
    AssetType, Novel, NovelChapter, TaskRecord, TaskStatus, TaskType,
)

logger = logging.getLogger(__name__)

# 分块参数：单块字符数与块摘要携带窗口
CHUNK_SIZE = 6000
SUMMARY_WINDOW = 800
# 单块重试由 Celery _TASK_RETRY 承担；这里控制链内块失败快速失败

AdaptError = type("AdaptError", (Exception,), {"__module__": __name__})


def collect_chapters(play, from_chapter=None, to_chapter=None):
    """按章节区间收集未软删章节（升序）；空范围抛错。"""
    novel = getattr(play, "novel", None)
    if novel is None:
        raise AdaptError("本剧尚无小说，无法改编")
    qs = NovelChapter.objects.filter(volume__novel=novel, is_deleted=False)
    if from_chapter:
        qs = qs.filter(chapter_number__gte=from_chapter)
    if to_chapter:
        qs = qs.filter(chapter_number__lte=to_chapter)
    chapters = list(qs.order_by("chapter_number"))
    if not chapters:
        raise AdaptError("章节范围内没有可改编的章节")
    return chapters


def chunk_chapters(chapters):
    """按累计字符量切块（CHUNK_SIZE）；返回 [{chapters: [chapter], start, end}]。"""
    blocks, current, size = [], [], 0
    for ch in chapters:
        current.append(ch)
        size += max(len(ch.content or ""), 1)
        if size >= CHUNK_SIZE:
            blocks.append({"chapters": current, "start": current[0].chapter_number,
                           "end": current[-1].chapter_number})
            current, size = [], 0
    if current:
        blocks.append({"chapters": current, "start": current[0].chapter_number,
                       "end": current[-1].chapter_number})
    return blocks


def story_context(play, chapters):
    """设定库注入：范围内出场人物的设定字段（design D6 改编质量依赖组 1）。"""
    from ..models import StoryAsset, StoryAssetType
    name_set = set()
    for ch in chapters:
        text = ch.content or ""
        for asset in StoryAsset.objects.filter(play=play, type=StoryAssetType.PERSON):
            if asset.pk in name_set:
                continue
            if asset.name and asset.name in text:
                name_set.add(asset.pk)
    assets = StoryAsset.objects.filter(pk__in=name_set)
    return "\n".join(
        f"- {a.name}：{json.dumps(a.fields, ensure_ascii=False)}"
        for a in assets.order_by("name"))


def block_payload(chapters, prev_summary, context):
    """单块改编提示词：前块摘要保连贯 + 设定库注入。"""
    texts = "\n\n".join(
        f"第 {ch.chapter_number} 章 {ch.title}\n{ch.content or ''}" for ch in chapters)
    prompt = (
        f"【前情摘要】\n{prev_summary or '（本块为首块）'}\n\n"
        f"【人物设定】\n{context or '（无）'}\n\n"
        f"【小说正文】\n{texts}\n\n"
        "你是短剧编剧。请把上述小说内容改编为短剧剧本，以 JSON 返回：\n"
        '{"summary": "本块剧情摘要（供下块连贯用，200字内）", '
        '"episodes": [{"episode_number": 1, "title": "", "scenes": ['
        '{"scene_number": 1, "location": "", "time_of_day": "DAY", '
        '"description": "", "mood": "", '
        '"shots": [{"shot_number": 1, "description": "", "dialogue": "", '
        '"action": ""}]}]}]}'
    )
    system = ("你是资深短剧编剧，擅长把小说章节压缩改编为紧凑的短剧集数，"
              "对白符合人物设定，只输出 JSON。")
    return prompt, system


def _parse(text):
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def parse_blocks(payload, from_chapter, to_chapter, *, episode_start=1):
    """端点入参校验 + 块规划（不调 LLM）：返回 (chapters, blocks, episode_start)。"""
    chapters = collect_chapters(payload, from_chapter, to_chapter)
    return chapters, chunk_chapters(chapters), episode_start


def emit_progress(play, record, kind, data):
    """改编进度事件（复用助手事件流；推送失败只记日志——events.emit 语义）。"""
    session_ids = _active_session_ids(play)
    for sid in session_ids:
        events.emit(sid, kind, data)


def _active_session_ids(play):
    from ..assistant.models import AssistantSession
    return list(AssistantSession.objects.filter(play=play)
                .order_by("-updated_at").values_list("pk", flat=True)[:5])


def dispatch_adaptation(play, from_chapter=None, to_chapter=None, *, episode_start=1,
                        target_episode=None, user=None):
    """派发改编任务（走既有计费闸/冻结剧/在跑去重）。返回 TaskRecord。

    target_episode：改编目标剧集（已有剧集保护在任务内判定——非覆盖确认拒绝）。
    """
    from .dispatcher import dispatch_task
    chapters = collect_chapters(play, from_chapter, to_chapter)
    blocks = chunk_chapters(chapters)
    pending = TaskRecord.objects.filter(
        play=play, task_type=TaskType.NOVEL_ADAPT,
        status__in=(TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRYING),
    ).exists()
    if pending:
        raise AdaptError("已有进行中的改编任务")
    summary = {
        "from_chapter": chapters[0].chapter_number,
        "to_chapter": chapters[-1].chapter_number,
        "chapter_count": len(chapters),
        "block_count": len(blocks),
        "episode_start": episode_start,
    }
    if target_episode is not None:
        summary["target_episode"] = target_episode.pk
    return dispatch_task(
        TaskType.NOVEL_ADAPT, play=play,
        input_summary={"adapt": summary},
    )


def adapt_pending(play):
    return TaskRecord.objects.filter(
        play=play, task_type=TaskType.NOVEL_ADAPT,
        status__in=(TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRYING),
    ).exists()


def resume_adaptation(play, record):
    """断点续跑：FAILED 记录清状态重派发（块产出已在 output_summary，worker 恢复）。"""
    from .dispatcher import send_task
    if record.task_type != TaskType.NOVEL_ADAPT or record.play_id != play.pk:
        raise AdaptError("任务不是本剧的改编任务")
    if adapt_pending(play):
        raise AdaptError("已有进行中的改编任务")
    record.status = TaskStatus.PENDING
    record.error_message = ""
    record.retry_count += 1
    record.save(update_fields=["status", "error_message", "retry_count", "updated_at"])
    send_task("shortplay.tasks.adapt_novel", args=[record.pk])
    return record
