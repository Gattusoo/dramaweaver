"""SmartCache 模型响应缓存（主计划 3.3.4–3.3.6 成本优化 / design D2-D3）。

只覆盖纯文本对话（LLM/CHAT 可重放）；IMAGE/TTS/VIDEO 受理即计费不缓存。
- 精确缓存：cache_key = SHA-256(规范化 model+messages+system)
- 语义缓存：同模型行内字符 bigram Jaccard 相似度 > SEMANTIC_CACHE_THRESHOLD 命中
  （零依赖近似方案，不引入向量化模型——守住 uv.lock 瘦身成果）
- TTL 惰性判定（读时）；写时顺带清理过期行（SMART_CACHE_MAX_ROWS 上限）
- 失败安全：任何缓存异常降级为 miss/直连（对齐反馈注入的容错模式）
"""
import hashlib
import json
import logging

logger = logging.getLogger(__name__)


def cache_key(model, messages, system="") -> str:
    """规范化序列化 + SHA-256。model 接受 AIModel 或 pk（均以模型身份入键）。"""
    model_id = getattr(model, "pk", model)
    normalized = json.dumps(
        {"m": model_id, "s": system or "", "msg": messages or []},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def similarity(a: str, b: str) -> float:
    """字符 bigram Jaccard（零依赖近似相似度，design D3）。空串相似度 0。"""
    if not a or not b:
        return 0.0
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _messages_text(messages) -> str:
    """消息列表 → 拼接文本（语义比对用）。兼容 content blocks 形状。"""
    out = []
    for m in messages or []:
        role = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, list):
            parts = []
            for blk in content:
                if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                    parts.append(blk["text"])
            content = " ".join(parts)
        if isinstance(content, str):
            out.append(f"{role}:{content}")
    return "\n".join(out)


def get_cached(model, messages, system=""):
    """查缓存 → (resp or None, hit_kind)。hit_kind ∈ exact | semantic | None。

    开关关闭返回 (None, None) 且不查库（零暴露语义）。异常降级 miss。
    """
    try:
        from django.conf import settings
        from django.utils import timezone as dj_tz

        from .models import SmartCache

        if not getattr(settings, "ENABLE_SMART_CACHE", False):
            return None, None
        key = cache_key(model, messages, system)
        ttl = int(getattr(settings, "SMART_CACHE_TTL_S", 3600))
        now = dj_tz.now()
        exact = SmartCache.objects.filter(cache_key=key).first()
        if exact is not None:
            if (now - exact.created_at).total_seconds() <= ttl:
                SmartCache.objects.filter(pk=exact.pk).update(
                    hit_count=exact.hit_count + 1, last_hit_at=now)
                return exact.cache_value, "exact"
            SmartCache.objects.filter(pk=exact.pk).delete()  # 过期惰性清理
            return None, None
        # 语义近似：同模型、未过期、行数上限内全扫
        if not getattr(settings, "ENABLE_SEMANTIC_CACHE", False):
            return None, None
        threshold = float(getattr(settings, "SEMANTIC_CACHE_THRESHOLD", 0.95))
        model_id = getattr(model, "pk", model)
        text = _messages_text(messages)
        if not text:
            return None, None
        rows = SmartCache.objects.filter(
            model_id=model_id, created_at__gte=now - dj_tz.timedelta(seconds=ttl))
        scan_cap = int(getattr(settings, "SEMANTIC_CACHE_SCAN_ROWS", 100))
        for row in rows[:scan_cap]:
            if similarity(text, _messages_text(
                    (row.cache_value or {}).get("_cached_messages"))) >= threshold:
                SmartCache.objects.filter(pk=row.pk).update(
                    hit_count=row.hit_count + 1, last_hit_at=now)
                return row.cache_value, "semantic"
        return None, None
    except Exception:  # noqa: BLE001 — 缓存异常不阻断调用
        logger.exception("SmartCache 查询失败")
        return None, None


def put_cached(model, messages, system, resp):
    """写缓存（幂等 update_or_create）。异常静默（写失败不影响调用结果）。"""
    try:
        from django.conf import settings
        from django.utils import timezone as dj_tz

        from .models import SmartCache

        if not getattr(settings, "ENABLE_SMART_CACHE", False):
            return
        key = cache_key(model, messages, system)
        model_id = getattr(model, "pk", model)
        model_name = getattr(model, "model_name", "")
        # 响应附带原始消息（语义比对用，下划线键与响应字段隔离）
        value = dict(resp or {})
        value["_cached_messages"] = messages or []
        SmartCache.objects.update_or_create(
            cache_key=key,
            defaults={"model_id": model_id if isinstance(model_id, int) else 0,
                      "model_name": model_name, "cache_value": value})
        _cleanup()
    except Exception:  # noqa: BLE001
        logger.exception("SmartCache 写入失败")


def _cleanup():
    """写时顺带清理：先删过期行，再压行数到 SMART_CACHE_MAX_ROWS。"""
    from django.conf import settings
    from django.utils import timezone as dj_tz

    from .models import SmartCache

    ttl = int(getattr(settings, "SMART_CACHE_TTL_S", 3600))
    max_rows = int(getattr(settings, "SMART_CACHE_MAX_ROWS", 500))
    SmartCache.objects.filter(
        created_at__lt=dj_tz.now() - dj_tz.timedelta(seconds=ttl)).delete()
    excess = SmartCache.objects.count() - max_rows
    if excess > 0:
        stale = (SmartCache.objects.order_by("created_at")
                 .values_list("pk", flat=True)[:excess])
        SmartCache.objects.filter(pk__in=list(stale)).delete()
