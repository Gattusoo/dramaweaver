"""AI Gateway 验证测试（openspec change: backend-ai-gateway）。"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APITestCase

import httpx

from apps.ai_gateway.circuit_breaker import CircuitBreaker, CLOSED, HALF_OPEN, OPEN
from apps.ai_gateway.cost_tracker import calculate
from apps.ai_gateway.gateway import AIGateway, AIGatewayError
from apps.ai_gateway.rate_limiter import RateLimiter
from apps.ai_gateway.redis_client import MemoryStore, reset_store
from apps.ai_gateway.router import build_candidate_chain
from apps.ai_gateway.models import (
    AIModel, AIProvider, CostRecord,
)
from apps.creation.models import (
    AgentConfig, Play, TaskRecord,
)
from apps.core.system.models import (
    CustomUser, UserRole,
)


class FakeLLMAdapter:
    def __init__(self, succeed=True, result=None, error=None):
        self.succeed = succeed
        self.result = result
        self.error = error

    def generate(self, prompt, system_prompt="", parameters=None):
        if not self.succeed:
            raise self.error or RuntimeError("boom")
        return self.result or {
            "text": "ok",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "model": parameters.get("model", ""),
        }


class CircuitBreakerTests(TestCase):
    """任务 8.3 熔断器三态。"""

    def setUp(self):
        reset_store()
        self.store = MemoryStore()
        self.cb = CircuitBreaker(store=self.store, cooldown_seconds=30)

    def test_closed_then_open_then_recover(self):
        pid = 1
        self.assertEqual(self.cb.state(pid), CLOSED)
        self.assertTrue(self.cb.allow_request(pid))
        # 连续 5 次失败 -> OPEN
        for _ in range(5):
            self.cb.record_failure(pid)
        self.assertEqual(self.cb.state(pid), OPEN)
        self.assertFalse(self.cb.allow_request(pid))
        # 冷却结束后 -> HALF_OPEN 放行探测
        self.store.set(f"cb:{pid}:cooldown_until", "0")
        self.assertTrue(self.cb.allow_request(pid))
        self.assertEqual(self.cb.state(pid), HALF_OPEN)
        # 探测成功 -> CLOSED
        self.cb.record_success(pid)
        self.assertEqual(self.cb.state(pid), CLOSED)

    def test_probe_failure_doubles_cooldown(self):
        pid = 2
        for _ in range(5):
            self.cb.record_failure(pid)
        self.store.set(f"cb:{pid}:cooldown_until", "0")
        self.cb.allow_request(pid)  # -> HALF_OPEN
        self.cb.record_failure(pid)  # 探测失败 -> OPEN 且冷却翻倍
        self.assertEqual(self.cb.state(pid), OPEN)


class RateLimiterTests(TestCase):
    """任务 8.4 限流。"""

    def setUp(self):
        reset_store()
        self.store = MemoryStore()
        self.rl = RateLimiter(store=self.store)

    def test_rpm_limit(self):
        pid = 1
        self.assertIsNotNone(self.rl.try_acquire(pid, max_rpm=1, max_concurrent=0))
        self.assertIsNone(self.rl.try_acquire(pid, max_rpm=1, max_concurrent=0))

    def test_concurrency_limit(self):
        pid = 2
        token = self.rl.try_acquire(pid, max_rpm=0, max_concurrent=1)
        self.assertIsNotNone(token)
        self.assertIsNone(self.rl.try_acquire(pid, max_rpm=0, max_concurrent=1))
        self.rl.release(pid, token)
        self.assertIsNotNone(self.rl.try_acquire(pid, max_rpm=0, max_concurrent=1))

    def test_lease_expired_member_recycled(self):
        """崩溃泄漏回收：过期租约在后续 acquire 时被清理，槽位可复用。"""
        pid = 3
        token = self.rl.try_acquire(pid, max_rpm=0, max_concurrent=1)
        self.assertIsNotNone(token)
        # 模拟租约过期：把成员 score 改写到过去
        key = self.rl._conc_key(pid)
        self.store._zsets[key] = {m: 0.0 for m in self.store._zsets[key]}
        # 过期成员被清掉 → 槽位空出 → 成功获得新租约
        new_token = self.rl.try_acquire(pid, max_rpm=0, max_concurrent=1)
        self.assertIsNotNone(new_token)
        self.assertNotEqual(token, new_token)
        # 新租约生效：并发 1 已被占用，再 acquire 拒绝
        self.assertIsNone(self.rl.try_acquire(pid, max_rpm=0, max_concurrent=1))

    def test_release_stale_token_noop(self):
        """租约换代后旧 token 的 release 是 no-op，不误减新持有者。"""
        pid = 4
        token = self.rl.try_acquire(pid, max_rpm=0, max_concurrent=1)
        self.assertIsNotNone(token)
        # token 已过期回收、新请求持有新租约
        key = self.rl._conc_key(pid)
        self.store._zsets[key] = {m: 0.0 for m in self.store._zsets[key]}
        self.rl.try_acquire(pid, max_rpm=0, max_concurrent=1)  # 清旧+发新
        # 旧进程的 release 到达：不应影响新持有者
        self.rl.release(pid, token)
        self.assertEqual(self.rl.current_concurrent(pid), 1)
        # 新租约正常释放
        key2 = self.rl._conc_key(pid)
        new_token = next(iter(self.store._zsets[key2]))
        self.rl.release(pid, new_token)
        self.assertEqual(self.rl.current_concurrent(pid), 0)

    def test_release_idempotent(self):
        """同一租约重复 release 幂等。"""
        pid = 5
        token = self.rl.try_acquire(pid, max_rpm=0, max_concurrent=1)
        self.rl.release(pid, token)
        self.rl.release(pid, token)
        self.assertEqual(self.rl.current_concurrent(pid), 0)
        self.assertIsNotNone(self.rl.try_acquire(pid, max_rpm=0, max_concurrent=1))

    def test_release_empty_token_noop(self):
        """不限并发成功（空 token）时 release 是 no-op。"""
        pid = 6
        token = self.rl.try_acquire(pid, max_rpm=0, max_concurrent=0)
        self.assertEqual(token, "")
        self.rl.release(pid, token)  # 不应抛错

    def test_rpm_reject_releases_lease(self):
        """RPM 被拒时归还并发租约。"""
        pid = 7
        self.assertIsNotNone(self.rl.try_acquire(pid, max_rpm=1, max_concurrent=5))
        self.assertIsNone(self.rl.try_acquire(pid, max_rpm=1, max_concurrent=5))
        # RPM 拒绝不占并发槽
        self.assertEqual(self.rl.current_concurrent(pid), 1)


class CostCalculatorTests(TestCase):
    """任务 8.5 成本计算。"""

    def test_llm_cost(self):
        cost = calculate(
            "LLM",
            {"input_tokens": 3500, "output_tokens": 1200},
            {"input_per_1k": 0.001, "output_per_1k": 0.002},
        )
        self.assertEqual(cost, Decimal("0.0059"))

    def test_tts_cost(self):
        cost = calculate(
            "TTS", {"audio_seconds": 5.2}, {"cost_per_second": 0.01}
        )
        self.assertEqual(cost, Decimal("0.052"))


class GatewayFailoverTests(APITestCase):
    """任务 8.1/8.2 适配器调用与降级链。"""

    def setUp(self):
        reset_store()
        self.store = MemoryStore()
        self.admin = CustomUser.objects.create_user(
            username="gwadmin", password="Passw0rd!123", is_platform_admin=True
        )
        self.producer = CustomUser.objects.create_user(
            username="gwprod", password="Passw0rd!123", role=UserRole.PRODUCER
        )
        self.primary_provider = AIProvider.objects.create(
            name="DeepSeek", provider_type="LLM", adapter_type="deepseek",
            base_url="https://api.deepseek.com/v1", api_key_encrypted="k1",
            priority=1, retry_count=0,
        )
        self.backup_provider = AIProvider.objects.create(
            name="OpenAI", provider_type="LLM", adapter_type="openai",
            base_url="https://api.openai.com/v1", api_key_encrypted="k2",
            priority=2, retry_count=0,
        )
        self.primary_model = AIModel.objects.create(
            provider=self.primary_provider, model_name="deepseek-v3",
            display_name="DeepSeek V3", model_type="LLM",
            cost_config={"input_per_1k": 0.001, "output_per_1k": 0.002},
        )
        self.backup_model = AIModel.objects.create(
            provider=self.backup_provider, model_name="gpt-4o",
            display_name="GPT-4o", model_type="LLM",
            cost_config={"input_per_1k": 0.01, "output_per_1k": 0.03},
        )
        self.config = AgentConfig.objects.create(
            name="配方", llm_model=self.primary_model, llm_failover=[self.backup_model.pk],
            created_by=self.producer,
        )

    def _factory(self, succeed_map):
        def factory(provider):
            return FakeLLMAdapter(succeed=succeed_map.get(provider.pk, True))
        return factory

    def test_primary_success(self):
        gateway = AIGateway(
            store=self.store,
            adapter_factory=self._factory({self.primary_provider.pk: True}),
        )
        result = gateway.call("LLM", self.config, prompt="生成大纲")
        self.assertEqual(result["text"], "ok")

    def test_failover_to_backup(self):
        gateway = AIGateway(
            store=self.store,
            adapter_factory=self._factory({self.primary_provider.pk: False}),
        )
        result = gateway.call("LLM", self.config, prompt="生成大纲")
        self.assertEqual(result["text"], "ok")

    def test_cost_record_created_on_success(self):
        play = Play.objects.create(title="短剧", created_by=self.producer)
        gateway = AIGateway(
            store=self.store,
            adapter_factory=self._factory({self.primary_provider.pk: True}),
        )
        gateway.call("LLM", self.config, prompt="生成大纲", play=play)
        self.assertTrue(CostRecord.objects.filter(play=play).exists())
        cr = CostRecord.objects.get(play=play)
        self.assertEqual(cr.provider, self.primary_provider)
        self.assertEqual(cr.model, "deepseek-v3")
        # 100/1000*0.001 + 50/1000*0.002 = 0.0001 + 0.0001 = 0.0002
        self.assertEqual(cr.total_cost, Decimal("0.0002"))


class CircuitClientErrorTests(APITestCase):
    """4xx（业务错误/限流）不计入熔断；5xx/网络错误计入并打开熔断。"""

    def setUp(self):
        reset_store()
        self.store = MemoryStore()
        self.producer = CustomUser.objects.create_user(
            username="cproducer", password="Passw0rd!123", role=UserRole.PRODUCER
        )
        self.provider = AIProvider.objects.create(
            name="P", provider_type="LLM", adapter_type="deepseek",
            base_url="https://api.deepseek.com/v1", api_key_encrypted="k",
            priority=1, retry_count=0,
        )
        self.model = AIModel.objects.create(
            provider=self.provider, model_name="m", display_name="M", model_type="LLM",
        )
        self.config = AgentConfig.objects.create(
            name="配方", llm_model=self.model, llm_failover=[],
            created_by=self.producer,
        )

    def _gateway(self, adapter, threshold=2):
        return AIGateway(
            store=self.store,
            adapter_factory=lambda provider: adapter,
            breaker=CircuitBreaker(store=self.store, error_threshold=threshold),
        )

    def _http_error(self, code):
        req = httpx.Request("POST", "https://api.deepseek.com/v1")
        resp = httpx.Response(code, request=req)
        return httpx.HTTPStatusError(f"HTTP {code}", request=req, response=resp)

    def _errors(self):
        return int(self.store.get(f"cb:{self.provider.pk}:errors") or 0)

    def test_4xx_does_not_count_or_open_circuit(self):
        """连续 4xx 达阈值也不熔断（4xx 是请求/业务错误，非服务故障）。"""
        adapter = FakeLLMAdapter(succeed=False, error=self._http_error(400))
        gateway = self._gateway(adapter, threshold=2)
        for _ in range(3):
            with self.assertRaises(AIGatewayError):
                gateway.call("LLM", self.config, prompt="x")
        self.assertEqual(self._errors(), 0)  # record_success 清零，未累计
        self.assertEqual(self.breaker_state(), CLOSED)

    def test_429_retries_without_breach(self):
        """限流 429：退避重试（retry_count+1 次），不计入熔断。"""
        calls = {"n": 0}

        class RateLimited:
            def generate(self, prompt, system_prompt="", parameters=None):
                calls["n"] += 1
                req = httpx.Request("POST", "https://api.deepseek.com/v1")
                resp = httpx.Response(429, request=req)
                raise httpx.HTTPStatusError("429", request=req, response=resp)

        # retry_count=0 由 provider 决定；这里临时用 retry_count=2 的 provider
        self.provider.retry_count = 2
        self.provider.save(update_fields=["retry_count"])
        gateway = self._gateway(RateLimited())
        with self.assertRaises(AIGatewayError):
            gateway.call("LLM", self.config, prompt="x")
        self.assertEqual(calls["n"], 3)  # retry_count=2 -> 3 次尝试
        self.assertEqual(self._errors(), 0)
        self.assertEqual(self.breaker_state(), CLOSED)

    def test_5xx_opens_circuit(self):
        """对照：5xx 仍按原逻辑累计并打开熔断。"""
        adapter = FakeLLMAdapter(succeed=False, error=self._http_error(500))
        gateway = self._gateway(adapter, threshold=2)
        for _ in range(2):
            with self.assertRaises(AIGatewayError):
                gateway.call("LLM", self.config, prompt="x")
        self.assertEqual(self.breaker_state(), OPEN)

    def breaker_state(self):
        return self.store.get(f"cb:{self.provider.pk}:state") or CLOSED


class GatewayStatusAPITests(APITestCase):
    """任务 8 状态端点。"""

    def setUp(self):
        reset_store()
        self.admin = CustomUser.objects.create_user(
            username="stadmin", password="Passw0rd!123", is_platform_admin=True
        )
        AIProvider.objects.create(
            name="SD", provider_type="IMAGE", adapter_type="sd_comfyui",
            base_url="http://localhost:8188", api_key_encrypted="",
            priority=1,
        )

    def test_status_endpoint_requires_admin(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/ai-gateway/status/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("providers", resp.data)
        self.assertIn("failover_events_24h", resp.data)
        self.assertEqual(len(resp.data["providers"]), 1)

    def test_candidate_chain(self):
        # 验证降级链解析：主模型 + failover
        provider = AIProvider.objects.create(
            name="Qwen", provider_type="LLM", adapter_type="qwen",
            base_url="https://dashscope.aliyuncs.com", api_key_encrypted="",
            priority=5,
        )
        model = AIModel.objects.create(
            provider=provider, model_name="qwen-max", display_name="Qwen Max",
            model_type="LLM",
        )
        chain = build_candidate_chain(None, "LLM")
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0][0], model)
