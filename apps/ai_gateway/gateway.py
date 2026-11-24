"""AI Gateway 主入口。

统一编排：路由 -> 熔断检查 -> 限流 -> 适配器调用（指数退避重试）
-> 失败降级 -> 成本记录。
"""
import json
import logging
import time

from .adapters import build_adapter
from .circuit_breaker import CircuitBreaker
from .cost_tracker import record_cost
from .rate_limiter import RateLimiter
from .redis_client import get_store
from .router import build_candidate_chain, build_chat_chain

logger = logging.getLogger(__name__)

# 指数退避序列（秒）
BACKOFF = [1, 2, 4]
# 限流排队等待（秒）
RATE_LIMIT_WAIT = 5
# 降级事件日志 Redis key
FAILOVER_EVENTS_KEY = "ai_gateway:failover_events"
FAILOVER_EVENTS_MAX = 200


class AIGatewayError(Exception):
    def __init__(self, message, provider_type=None, attempts=None):
        super().__init__(message)
        self.provider_type = provider_type
        self.attempts = attempts or []


def log_failover(from_provider_id, to_provider_id, reason, store=None):
    """记录一条降级事件到 Redis 环形列表。"""
    store = store or get_store()
    try:
        raw = store.get(FAILOVER_EVENTS_KEY)
        events = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        events = []
    events.append(
        {
            "time": time.time(),
            "from": from_provider_id,
            "to": to_provider_id,
            "reason": reason,
        }
    )
    events = events[-FAILOVER_EVENTS_MAX:]
    store.set(FAILOVER_EVENTS_KEY, json.dumps(events))


def read_failover_events(store=None, since=None):
    store = store or get_store()
    raw = store.get(FAILOVER_EVENTS_KEY)
    if not raw:
        return []
    try:
        events = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if since is not None:
        events = [e for e in events if e.get("time", 0) >= since]
    return events


class AIGateway:
    def __init__(self, breaker=None, limiter=None, adapter_factory=None, store=None):
        self.store = store or get_store()
        self.breaker = breaker or CircuitBreaker(store=self.store)
        self.limiter = limiter or RateLimiter(store=self.store)
        self.adapter_factory = adapter_factory or build_adapter

    # ── main entry ────────────────────────────────────────────────────────
    def call(self, provider_type, agent_config, *, play=None, task_record=None, asset=None, **payload):
        """Execute an AI call with routing / retry / fallback.

        payload keys by provider_type:
          LLM   : prompt, system_prompt, temperature, max_tokens, response_format
          IMAGE : prompt, reference_images, aspect_ratio, size
          TTS   : text, voice_id, speed
          VIDEO : image_url, duration
        """
        # 属主解析（决策 3/9）：配方属主即生成任务的属主；custom 节点需属主有凭证
        owner = getattr(agent_config, "created_by", None)
        candidates = build_candidate_chain(
            agent_config, provider_type, breaker=self.breaker, tried_providers=[], user=owner,
        )
        if not candidates:
            raise AIGatewayError(
                f"无可用 {provider_type} 候选（主模型未配置且无全局备用）",
                provider_type=provider_type,
            )
        return self._route(provider_type, candidates, payload,
                           play=play, task_record=task_record, asset=asset, user=owner)

    def chat(self, agent_config, *, messages, system="", tools=None, play=None, task_record=None,
             model=None, **params):
        """多轮工具调用（ai-gateway/tool-calling）。只在支持工具调用的模型间降级，
        候选为空时报错而不退化为纯文本；熔断 / 限流 / 重试 / 计费与 call 相同。

        model：指定单个 AIModel 时固定用它（跳过 supports_tools 过滤，供摘要等
        无需工具的辅助调用走更便宜的小模型）；为 None 时走 assistant 候选链。"""
        if model is not None:
            provider = getattr(model, "provider", None)
            candidates = [(model, provider)] if (
                model.is_active and provider is not None and provider.is_active) else []
        else:
            owner = getattr(agent_config, "created_by", None)
            candidates = build_chat_chain(agent_config, breaker=self.breaker, user=owner)
        if not candidates:
            raise AIGatewayError(
                "无可用的支持工具调用的 LLM 模型（需在 AIModel.capabilities 中设置 supports_tools）",
                provider_type="LLM",
            )
        payload = {**params, "messages": messages, "system": system, "tools": tools or []}
        return self._route("CHAT", candidates, payload, play=play, task_record=task_record,
                           user=getattr(agent_config, "created_by", None))

    def vision(self, agent_config, *, image_url, prompt, system="", play=None, task_record=None,
               model=None, **params):
        """视觉分析（Phase 2.3）：图片 + 文本 → 结构化文本。

        image_url 支持 http(s) 或 data URI。只走 capabilities.supports_vision 的模型；
        候选为空时报错（调用方据此降级为不使用视觉分析）。
        """
        from .router import build_vision_chain
        if model is not None:
            provider = getattr(model, "provider", None)
            candidates = [(model, provider)] if (
                model.is_active and provider is not None and provider.is_active) else []
        else:
            owner = getattr(agent_config, "created_by", None)
            candidates = build_vision_chain(agent_config, breaker=self.breaker, user=owner)
        if not candidates:
            raise AIGatewayError("无可用的视觉模型（需在 AIModel.capabilities 中设置 supports_vision）",
                                 provider_type="LLM")
        block = _vision_block(image_url)
        messages = [{"role": "user", "content": [
            {"type": "image", **block},
            {"type": "text", "text": prompt},
        ]}]
        payload = {**params, "messages": messages, "system": system, "tools": []}
        return self._route("CHAT", candidates, payload, play=play, task_record=task_record,
                           user=getattr(agent_config, "created_by", None))

    def _route(self, provider_type, candidates, payload, **ctx):
        # SmartCache（主计划 3.3.4–3.3.6）：只缓存纯文本对话（可重放）；
        # IMAGE/TTS/VIDEO 受理即计费不可缓存。命中返回原响应（不产生 CostRecord）。
        if provider_type == "CHAT":
            from .smart_cache import get_cached
            messages = payload.get("messages") or []
            system = payload.get("system") or ""
            model = candidates[0][0] if candidates else None
            if model is not None:
                cached, kind = get_cached(model, messages, system)
                if cached is not None:
                    return cached
        attempts = []
        last_error = None
        for model, provider in candidates:
            result, error = self._try_provider(provider_type, model, provider, payload, **ctx)
            if error is None:
                # 成功的 CHAT 响应写缓存（失败安全，写失败不影响结果）
                if provider_type == "CHAT":
                    from .smart_cache import put_cached
                    put_cached(model, payload.get("messages") or [],
                               payload.get("system") or "", result)
                return result
            attempts.append({"provider": provider.name, "model": model.model_name, "error": str(error)})
            last_error = error
            # 记录降级事件
            log_failover(provider.pk, None, self._reason_of(error), self.store)

        raise AIGatewayError(
            f"{provider_type} 调用失败，已尝试 {len(attempts)} 个 provider: {last_error}",
            provider_type=provider_type,
            attempts=attempts,
        )

    # ── per-provider attempt ──────────────────────────────────────────────
    def _try_provider(self, provider_type, model, provider, payload, **ctx):
        """Attempt one provider with rate-limit wait + backoff retry."""
        max_rpm = (provider.rate_limit or {}).get("max_rpm", 0)
        max_concurrent = (provider.rate_limit or {}).get("max_concurrent", 0)
        # 真正调用前再过熔断器（HALF_OPEN 时原子占用唯一探测名额）
        if not self.breaker.allow_request(provider.pk):
            return None, CircuitOpenError("熔断中")
        acquired = self.limiter.acquire(
            provider.pk, max_rpm=max_rpm, max_concurrent=max_concurrent,
            wait_seconds=RATE_LIMIT_WAIT,
        )
        if acquired is None:
            self.breaker.release_probe(provider.pk)
            return None, RateLimitExceededError("限流排队超时")

        # 0 表示不重试；仅未配置（None）时使用默认值
        retry_count = 3 if provider.retry_count is None else provider.retry_count
        try:
            adapter = self.adapter_factory(provider)
        except Exception as exc:  # noqa: BLE001
            self.limiter.release(provider.pk, acquired)
            self.breaker.release_probe(provider.pk)
            return None, exc

        last_error = None
        # 幂等性：LLM/CHAT（纯文本生成）可整段重试；IMAGE/VIDEO/TTS 服务端受理即计费，
        # 5xx/超时后重跑会重复生成+重复计费 → 不重试，仅记失败降级（429 被拒未受理，仍按退避重试）。
        idempotent = provider_type in ("LLM", "CHAT")
        for attempt in range(retry_count + 1):
            try:
                result = self._invoke(adapter, model, provider_type, payload)
                self.limiter.release(provider.pk, acquired)
                self.breaker.record_success(provider.pk)
                # 服务费埋点（决策 18）：custom provider + 属主凭证 → emit service_fee 事件
                try:
                    self._emit_service_fee(provider, **ctx)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("服务费埋点失败（不影响已成功调用）: %s", exc)
                # 成本记录失败不得触发 AI 重试（已计费），仅记录日志
                try:
                    self._record_cost(model, provider, provider_type, result, **ctx)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("记录成本失败（不影响已成功调用）: %s", exc)
                return result, None
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                # 4xx（请求/权限/业务错误、限流）：服务可达但拒绝——不计入熔断（非服务故障）
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code is not None and 400 <= int(status_code) < 500:
                    self.breaker.record_success(provider.pk)
                    logger.warning(
                        "%s %s attempt %d HTTP %s（不熔断）: %s",
                        provider.name, model.model_name, attempt + 1, status_code, exc,
                    )
                    if status_code == 429:
                        # 限流：幂等调用退避重试；非幂等生成（IMAGE/VIDEO/TTS）不重试——
                        # 适配器在 generate/image_to_video 内部轮询任务，轮询阶段的 429
                        # 发生时供应商已受理并计费，重试会提交一个全新任务再计一次费。
                        if not idempotent:
                            self.limiter.release(provider.pk, acquired)
                            return None, exc
                        if attempt < retry_count:
                            time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                        continue
                    # 其他 4xx：不重试（同样错误会再失败），直接降级到下一 provider
                    self.limiter.release(provider.pk, acquired)
                    return None, exc
                self.breaker.record_failure(provider.pk)
                logger.warning(
                    "%s %s attempt %d failed: %s",
                    provider.name, model.model_name, attempt + 1, exc,
                )
                # 熔断已打开（含探测失败）则不再对该 provider 重试，直接降级
                if not self.breaker.is_available(provider.pk):
                    break
                # 非幂等生成（已可能受理+计费）：5xx/超时不重跑，直接降级
                if not idempotent:
                    break
                if attempt < retry_count:
                    time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
        self.limiter.release(provider.pk, acquired)
        return None, last_error

    def _invoke(self, adapter, model, provider_type, payload):
        params = dict(payload)
        params["model"] = model.model_name
        if provider_type == "LLM":
            prompt = payload.get("prompt", "")
            system_prompt = payload.get("system_prompt", "")
            return adapter.generate(prompt, system_prompt, params)
        if provider_type == "IMAGE":
            prompt = payload.get("prompt", "")
            refs = payload.get("reference_images") or []
            return adapter.generate(prompt, refs, params)
        if provider_type == "TTS":
            text = payload.get("text", "")
            voice_id = payload.get("voice_id", "")
            return adapter.synthesize(text, voice_id, params)
        if provider_type == "VIDEO":
            image_url = payload.get("image_url", "")
            return adapter.image_to_video(image_url, params)
        if provider_type == "CHAT":
            for key in ("messages", "system", "tools"):
                params.pop(key, None)
            return adapter.chat(payload["messages"], payload.get("system", ""), payload.get("tools"), params)
        raise ValueError(f"未知 provider_type: {provider_type}")

    def _record_cost(self, model, provider, provider_type, result, **ctx):
        if not ctx.get("play"):
            return
        if provider_type == "CHAT":
            provider_type = "LLM"  # 按 LLM 单价计费；CostRecord.provider_type 无 CHAT
        usage = result.get("usage", {}) or {}
        # 视频适配器（Kling/Hailuo 等）把时长放在结果的顶层 duration_seconds，
        # 不放进 usage。计费读 usage.video_seconds 会恒得 0，成本/预算统计全部失真。
        if provider_type == "VIDEO" and not usage.get("video_seconds"):
            usage = {**usage, "video_seconds": result.get("duration_seconds", 0) or 0}
        record_cost(
            play=ctx["play"],
            task_record=ctx.get("task_record"),
            provider=provider,
            model_name=model.model_name,
            provider_type=provider_type,
            usage=usage,
            cost_config=model.cost_config or {},
            asset=ctx.get("asset"),
        )

    @staticmethod
    def _reason_of(error):
        if isinstance(error, RateLimitExceededError):
            return "rate_limit_exceeded"
        if isinstance(error, CircuitOpenError):
            return "circuit_open"
        if "timed out" in str(error).lower() or "timeout" in str(error).lower():
            return "timeout"
        return "error"

    # ── 服务费埋点（决策 18，仅埋事件不扣费）────────────────────────────────

    @staticmethod
    def _emit_service_fee(provider, **ctx):
        """custom 节点 + 属主凭证路由的成功调用 emit `service_fee` 事件。

        - amount = settings.SERVICE_FEE_PER_CALL（默认 0 → 直接 return，不emit）
        - 仅当 provider.visibility=custom 且 ctx["user"] 有启用凭证时 emit
        - 平台模型路由（visibility=platform）不 emit
        - 计费变更消费此事件落流水；本变更不实现扣费
        """
        from django.conf import settings as dj_settings

        from .models import ProviderVisibility
        from .credentials import resolve_credential

        amount = int(getattr(dj_settings, "SERVICE_FEE_PER_CALL", 0) or 0)
        if amount <= 0:
            return
        if provider.visibility != ProviderVisibility.CUSTOM:
            return
        user = ctx.get("user")
        if user is None:
            return
        cred = resolve_credential(user, provider)
        if cred is None:
            return
        # 事件总线：写 Redis 环形列表（与 failover events 同型）；billing 变更消费
        event = {
            "type": "service_fee",
            "user_id": user.pk,
            "provider_id": provider.pk,
            "credential_id": cred.pk,
            "amount": amount,
            "play_id": getattr(ctx.get("play"), "pk", None),
            "task_record_id": getattr(ctx.get("task_record"), "pk", None),
            "ts": time.time(),
        }
        try:
            store = ctx.get("store") or get_store()
            raw = store.get("ai_gateway:service_fee_events")
            events = json.loads(raw) if raw else []
            events.append(event)
            events = events[-FAILOVER_EVENTS_MAX:]  # 复用环形上限
            store.set("ai_gateway:service_fee_events", json.dumps(events))
        except Exception:  # noqa: BLE001
            logger.exception("service_fee 事件写 Redis 失败")


def _vision_block(image_url):
    """http(s) 图片原样传 url；data URI 或本地 /media 路径转 base64 source（Anthropic 风格）。"""
    import base64
    from pathlib import Path

    from django.conf import settings

    url = (image_url or "").strip()
    if url.startswith(("http://", "https://")):
        return {"url": url}
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        media_type = header[5:].split(";")[0] or "image/png"
        return {"source": {"type": "base64", "media_type": media_type, "data": data}}
    # 本地 MEDIA_URL 路径（/media/images/x.png）→ 读文件转 base64
    media_url = (settings.MEDIA_URL or "/media/").rstrip("/")
    rel = url[len(media_url):].lstrip("/") if url.startswith(media_url) else url.lstrip("/")
    path = Path(settings.MEDIA_ROOT) / rel
    if not path.is_file():
        raise AIGatewayError(f"图片不存在或不可读取：{url}", provider_type="LLM")
    media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return {"source": {"type": "base64", "media_type": media_type,
                       "data": base64.b64encode(path.read_bytes()).decode("ascii")}}


class RateLimitExceededError(Exception):
    pass


class CircuitOpenError(Exception):
    pass
