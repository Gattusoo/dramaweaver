"""灵感库服务（Phase 3.5.1 / design D3）。

与 SkillLibrary（可复用方法论：怎么写）、MemoryRecord（本剧工作状态）正交：
灵感是"写什么"的跨会话可复用创意素材。检索为关键词命中 + 评分加权排序
（与 memory_layers.search_memory_records 同型，无向量计算）。
"""
import re

from django.db.models import Q

from .models import (Inspiration, InspirationCategory, InspirationSource)

MAX_CONTENT = 2000


class InspirationError(ValueError):
    """灵感操作校验失败（调用方转 400/ToolError）。"""


def save_inspiration(*, play, user, category, content, rating=3,
                     source=InspirationSource.MANUAL) -> Inspiration:
    """保存灵感（play=None 为全局）。长度/分类校验；rating 1-5。"""
    if not isinstance(content, str) or not content.strip():
        raise InspirationError("灵感内容不能为空")
    if len(content) > MAX_CONTENT:
        raise InspirationError(f"灵感内容超过 {MAX_CONTENT} 字上限")
    if category not in InspirationCategory.values:
        raise InspirationError("灵感分类非法")
    if not isinstance(rating, int) or isinstance(rating, bool) or not (1 <= rating <= 5):
        raise InspirationError("评分须为 1-5 的整数")
    return Inspiration.objects.create(
        play=play, user=user, category=category, content=content.strip(),
        source=source, rating=rating)


def _visible_q(play):
    """可见范围：全局 + 本剧（play 为 None 时仅全局）。"""
    q = Q(play__isnull=True)
    if play is not None:
        q |= Q(play_id=play.pk)
    return q


def search_inspirations(*, user, play, keywords, category=None, limit=None):
    """关键词检索（GLOBAL+本剧合并）：命中数×10 + rating 加权排序。

    keywords：字符串（空格分词）或列表；命中 = content/category icontains 任一。
    """
    if limit is None:
        limit = int(getattr_limit("INSPIRATION_SEARCH_LIMIT", 20))
    limit = max(1, min(int(limit), 50))
    if isinstance(keywords, str):
        words = [w for w in re.split(r"\s+", keywords.strip()) if w]
    else:
        words = [str(w) for w in (keywords or []) if str(w).strip()]
    qs = Inspiration.objects.filter(_visible_q(play))
    if category:
        if category not in InspirationCategory.values:
            raise InspirationError("灵感分类非法")
        qs = qs.filter(category=category)
    rows = list(qs)
    if not words:
        scored = [(r.rating, r) for r in rows]
    else:
        scored = []
        for r in rows:
            hits = sum(1 for w in words
                       if w.lower() in (r.content or "").lower()
                       or w.lower() in (r.category or "").lower())
            if hits:
                scored.append((hits * 10 + r.rating, r))
    scored.sort(key=lambda t: -t[0])
    return [r for _, r in scored[:limit]]


def _extract_keywords(text: str, max_words=8):
    """2-4 字符中文切分 + 英文单词（与种子工具 text_keywords 同款，纯函数）。"""
    words = re.findall(r"[a-zA-Z]{2,}", text or "")
    words += re.findall(r"[一-龥]{2,4}", text or "")
    seen, out = set(), []
    for w in words:
        k = w.lower()
        if k not in seen:
            seen.add(k)
            out.append(k)
        if len(out) >= max_words:
            break
    return out


def recommend_inspirations(*, play, text, mark_used=False, limit=None):
    """按文本推荐相关灵感：关键词提取 → 命中数排序（>0 才推荐）。

    mark_used=True 时对返回条目 used_count +1（agent 显式标记引用）。
    """
    if limit is None:
        limit = int(getattr_limit("INSPIRATION_RECOMMEND_LIMIT", 5))
    limit = max(1, min(int(limit), 20))
    words = _extract_keywords(text or "")
    rows = list(Inspiration.objects.filter(_visible_q(play)))
    scored = []
    for r in rows:
        hits = sum(1 for w in words if w in (r.content or "").lower())
        if hits > 0:
            scored.append((hits * 10 + r.rating, r))
    scored.sort(key=lambda t: -t[0])
    picked = [r for _, r in scored[:limit]]
    if mark_used and picked:
        from django.db.models import F
        Inspiration.objects.filter(pk__in=[r.pk for r in picked]) \
            .update(used_count=F("used_count") + 1)
    return picked


def getattr_limit(name, default):
    from django.conf import settings
    return getattr(settings, name, default)
