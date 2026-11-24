"""/code-review max A 集群与 C.1/C.3 修复的回归测试。

覆盖：
- A.1 base_url SSRF 防护（security.validate_base_url + 序列化校验 + provider_test 复验）
- A.2 api_key GET 脱敏（write-only + has_key）
- A.3 非幂等生成（IMAGE/VIDEO/TTS）5xx/超时不重跑
- A.4 OpenAICompatLLMAdapter.generate 防御性解析
- C.1 import_provider_models 计数与 DB 实际一致
- C.3 熔断 CLOSED->OPEN 跃迁并发不重复
"""
from unittest.mock import MagicMock, patch

import httpx
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from apps.ai_gateway.circuit_breaker import CircuitBreaker, CLOSED, OPEN
from apps.ai_gateway.gateway import AIGateway, AIGatewayError
from apps.ai_gateway.models import AIModel, AIProvider
from apps.ai_gateway.redis_client import MemoryStore, reset_store
from apps.ai_gateway.security import classify_test_error, validate_base_url
from apps.creation.models import AgentConfig
from apps.core.system.models import CustomUser, UserRole


# ─── A.1 SSRF 防护 ────────────────────────────────────────────────────────

class ValidateBaseUrlTests(TestCase):
    """validate_base_url：仅 http/https 公网；链路本地/metadata 硬封；私网/回环开关放行。"""

    def test_public_ip_allowed(self):
        self.assertEqual(validate_base_url("https://8.8.8.8/v1"), (True, ""))

    def test_unresolvable_hostname_allowed(self):
        # 无法解析（占位域名/离线）放行，由适配器调用时报错——不阻断 admin 入库
        self.assertEqual(validate_base_url("https://x"), (True, ""))

    def test_metadata_ip_rejected(self):
        ok, reason = validate_base_url("http://169.254.169.254/latest/meta-data/")
        self.assertFalse(ok)
        self.assertIn("metadata", reason)

    def test_metadata_hostname_rejected(self):
        ok, reason = validate_base_url("https://metadata.google.internal/v1")
        self.assertFalse(ok)

    def test_loopback_rejected_by_default(self):
        ok, reason = validate_base_url("http://127.0.0.1:8188")
        self.assertFalse(ok)
        self.assertIn("内网", reason)

    def test_private_rejected_by_default(self):
        ok, reason = validate_base_url("http://192.168.1.10:11434")
        self.assertFalse(ok)

    @override_settings(AI_GATEWAY_ALLOW_LOCAL_BASE_URL=True)
    def test_local_flag_releases_loopback_and_private(self):
        # 本地模型（Ollama/ComfyUI）场景：开关放行私网/回环
        self.assertEqual(validate_base_url("http://127.0.0.1:8188")[0], True)
        self.assertEqual(validate_base_url("http://10.0.0.5:11434")[0], True)

    @override_settings(AI_GATEWAY_ALLOW_LOCAL_BASE_URL=True)
    def test_link_local_rejected_even_with_local_flag(self):
        # 链路本地（云 metadata 网段）任何时候都拒绝
        self.assertFalse(validate_base_url("http://169.254.169.254/")[0])

    def test_scheme_whitelist(self):
        self.assertFalse(validate_base_url("ftp://api.example.com")[0])
        self.assertFalse(validate_base_url("file:///etc/passwd")[0])

    def test_empty_ok(self):
        self.assertEqual(validate_base_url(""), (True, ""))


class ProviderCrudSsrfTests(APITestCase):
    """AIProvider CRUD：base_url 校验入库前生效；api_key GET 脱敏。"""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username="secadm", password="Passw0rd!123", is_platform_admin=True
        )
        self.client.force_authenticate(self.admin)

    def test_create_metadata_ip_rejected(self):
        resp = self.client.post(
            "/api/ai-providers/",
            {"name": "evil", "provider_type": "LLM", "adapter_type": "openai",
             "base_url": "http://169.254.169.254/latest/", "api_key_encrypted": "k"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_create_loopback_rejected_then_flag_allows(self):
        body = {"name": "comfy", "provider_type": "IMAGE", "adapter_type": "comfyui",
                "base_url": "http://127.0.0.1:8188", "api_key_encrypted": "none"}
        resp = self.client.post("/api/ai-providers/", body, format="json")
        self.assertEqual(resp.status_code, 400)
        with override_settings(AI_GATEWAY_ALLOW_LOCAL_BASE_URL=True):
            resp2 = self.client.post("/api/ai-providers/", body, format="json")
        self.assertEqual(resp2.status_code, 201, resp2.data)

    def test_api_key_not_in_get_response(self):
        self.client.post(
            "/api/ai-providers/",
            {"name": "p1", "provider_type": "LLM", "adapter_type": "openai",
             "base_url": "https://8.8.8.8/v1", "api_key_encrypted": "sk-secret"},
            format="json",
        )
        detail = self.client.get("/api/ai-providers/").data
        rows = detail["results"] if isinstance(detail, dict) and "results" in detail else detail
        for row in rows:
            self.assertNotIn("api_key_encrypted", row)   # 明文 key 不回显
            self.assertIn("has_key", row)                 # 只透出是否存在
            self.assertTrue(row["has_key"])

    def test_has_key_false_without_key(self):
        # 模型层 api_key_encrypted 必填（无 blank=True），空 key 走 ORM 建存量数据
        AIProvider.objects.create(
            name="nokey", provider_type="IMAGE", adapter_type="comfyui",
            base_url="https://8.8.8.8", api_key_encrypted="",
        )
        detail = self.client.get("/api/ai-providers/").data
        rows = detail["results"] if isinstance(detail, dict) and "results" in detail else detail
        row = next(r for r in rows if r["name"] == "nokey")
        self.assertFalse(row["has_key"])


class ProviderTestSsrfTests(APITestCase):
    """/providers/{id}/test/：存量 base_url 复验 + 错误回显脱敏。"""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username="testadm", password="Passw0rd!123", is_platform_admin=True
        )
        self.client.force_authenticate(self.admin)

    def test_existing_private_base_url_rejected_at_test_time(self):
        # ORM 直建（绕过序列化校验）的存量 provider，测试端点复验拒绝
        provider = AIProvider.objects.create(
            name="old", provider_type="LLM", adapter_type="openai",
            base_url="http://169.254.169.254/latest/", api_key_encrypted="k",
        )
        resp = self.client.post(f"/api/ai-gateway/providers/{provider.pk}/test/", {}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("校验失败", resp.data["detail"])

    def test_error_echo_is_classified_not_raw(self):
        provider = AIProvider.objects.create(
            name="t", provider_type="LLM", adapter_type="openai",
            base_url="https://8.8.8.8/v1", api_key_encrypted="k",
        )
        AIModel.objects.create(provider=provider, model_name="m", display_name="m",
                               model_type="LLM", is_active=True)
        secret_marker = "InternalHostname-10.0.0.7"
        with patch("apps.ai_gateway.views.build_adapter") as mock_build:
            mock_build.return_value.generate.side_effect = httpx.ConnectTimeout(secret_marker)
            resp = self.client.post(
                f"/api/ai-gateway/providers/{provider.pk}/test/", {}, format="json",
            )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["success"])
        # 回显为分类文案，不含原始异常字符串（防内网信息泄露）
        self.assertEqual(resp.data["error"], "连接超时（目标服务无响应）")
        self.assertNotIn(secret_marker, resp.data["error"])

    def test_auth_error_classified(self):
        provider = AIProvider.objects.create(
            name="t2", provider_type="LLM", adapter_type="openai",
            base_url="https://8.8.8.8/v1", api_key_encrypted="k",
        )
        AIModel.objects.create(provider=provider, model_name="m", display_name="m",
                               model_type="LLM", is_active=True)
        with patch("apps.ai_gateway.views.build_adapter") as mock_build:
            resp401 = MagicMock(status_code=401)
            mock_build.return_value.generate.side_effect = httpx.HTTPStatusError(
                "Unauthorized", request=MagicMock(), response=resp401)
            resp = self.client.post(
                f"/api/ai-gateway/providers/{provider.pk}/test/", {}, format="json",
            )
        self.assertEqual(resp.data["error"], "鉴权失败（API Key 无效或无权限）")


class ClassifyTestErrorTests(TestCase):
    def test_unknown_exception_generic(self):
        msg = classify_test_error(ValueError("sensitive-detail"))
        self.assertIn("连通性测试失败", msg)
        self.assertNotIn("sensitive-detail", msg)


# ─── A.3 非幂等生成不重跑 ─────────────────────────────────────────────────

class _CountingAdapter:
    """计数适配器：记录 generate 被调次数，按 provider_type 模拟超时。"""

    def __init__(self, error):
        self.error = error
        self.calls = []

    def generate(self, *args, **kwargs):
        self.calls.append(args)
        raise self.error

    def synthesize(self, *args, **kwargs):
        self.calls.append(args)
        raise self.error

    def image_to_video(self, *args, **kwargs):
        self.calls.append(args)
        raise self.error


class NonIdempotentRetryTests(APITestCase):
    """IMAGE/TTS/VIDEO 超时（服务端已可能受理+计费）不重跑；LLM 保持重试。"""

    def setUp(self):
        reset_store()
        self.store = MemoryStore()
        self.producer = CustomUser.objects.create_user(
            username="retryprod", password="Passw0rd!123", role=UserRole.PRODUCER
        )
        self.adapter = _CountingAdapter(httpx.ReadTimeout("timed out"))

    def _provider(self, ptype, adapter_type="x"):
        provider = AIProvider.objects.create(
            name=f"{ptype}", provider_type=ptype, adapter_type=adapter_type,
            base_url="https://8.8.8.8/v1", api_key_encrypted="", priority=1,
            retry_count=2,
        )
        AIModel.objects.create(provider=provider, model_name="m", display_name="m",
                               model_type=ptype)
        return provider

    def _config(self, model):
        return AgentConfig.objects.create(name="c", created_by=self.producer, **{
            {"LLM": "llm_model", "IMAGE": "image_model",
             "TTS": "tts_model", "VIDEO": "video_model"}[model.provider.provider_type]: model,
        })

    def test_image_timeout_single_attempt(self):
        provider = self._provider("IMAGE")
        config = self._config(provider.models.first())
        gateway = AIGateway(store=self.store, adapter_factory=lambda p: self.adapter)
        with self.assertRaises(AIGatewayError):
            gateway.call("IMAGE", config, prompt="画一张图")
        self.assertEqual(len(self.adapter.calls), 1)  # 不重跑，避免重复生成+计费

    def test_tts_timeout_single_attempt(self):
        provider = self._provider("TTS")
        config = self._config(provider.models.first())
        gateway = AIGateway(store=self.store, adapter_factory=lambda p: self.adapter)
        with self.assertRaises(AIGatewayError):
            gateway.call("TTS", config, text="台词", voice_id="v")
        self.assertEqual(len(self.adapter.calls), 1)

    def test_llm_timeout_still_retries(self):
        provider = self._provider("LLM")
        config = self._config(provider.models.first())
        gateway = AIGateway(store=self.store, adapter_factory=lambda p: self.adapter)
        with self.assertRaises(AIGatewayError):
            gateway.call("LLM", config, prompt="写大纲")
        self.assertEqual(len(self.adapter.calls), 3)  # retry_count=2 → 共 3 次尝试


# ─── A.4 generate 防御性解析 ─────────────────────────────────────────────

def _patch_response(payload):
    """patch safe_post（出站统一入口），让 generate() 拿到预设 payload。

    适配器不再直接调 httpx.Client，统一走 apps.ai_gateway.http.safe_post；
    此处 patch 模块内引用即可拦截（不再依赖 httpx.Client mock）。
    """
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return patch("apps.ai_gateway.adapters.safe_post", return_value=resp)


class GenerateParsingTests(TestCase):
    """generate() 与 chat()/from_openai_response 防御一致：不因非标准 200 抛 KeyError。"""

    def setUp(self):
        from apps.ai_gateway.adapters import OpenAICompatLLMAdapter
        self.adapter = OpenAICompatLLMAdapter(MagicMock(base_url="https://x/v1",
                                                         api_key_encrypted="k"))

    def test_error_envelope_raises_business_error(self):
        with _patch_response({"error": {"message": "quota exceeded"}}):
            with self.assertRaises(RuntimeError) as ctx:
                self.adapter.generate("p", "", {"model": "m"})
            self.assertIn("quota exceeded", str(ctx.exception))

    def test_empty_choices_degrades_to_empty_text(self):
        with _patch_response({"choices": []}):
            result = self.adapter.generate("p", "", {"model": "m"})
        self.assertEqual(result["text"], "")

    def test_null_content_degrades_to_empty_text(self):
        with _patch_response({"choices": [{"message": {"content": None}}],
                              "usage": {"prompt_tokens": 1}}):
            result = self.adapter.generate("p", "", {"model": "m"})
        self.assertEqual(result["text"], "")

    def test_normal_response_unaffected(self):
        payload = {"choices": [{"message": {"content": "你好"},
                                "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        with _patch_response(payload):
            result = self.adapter.generate("p", "", {"model": "m"})
        self.assertEqual(result["text"], "你好")


# ─── C.1 import 计数与 DB 一致 ────────────────────────────────────────────

class ImportCountingTests(TestCase):
    """重复名去重；imported 与 bulk_create 实际插入行数一致。"""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username="imp", password="Passw0rd!123", is_platform_admin=True
        )
        self.provider = AIProvider.objects.create(
            name="dalle", provider_type="IMAGE", adapter_type="dall_e",
            base_url="https://8.8.8.8/v1", api_key_encrypted="k", priority=1,
        )

    def test_duplicate_names_deduped_in_count(self):
        from apps.ai_gateway.model_catalog import import_provider_models
        result = import_provider_models(
            self.provider, ["dall-e-3", "dall-e-3", "gpt-image-1"])
        self.assertEqual(result["imported"], ["dall-e-3", "gpt-image-1"])  # 去重
        self.assertEqual(result["skipped"], ["dall-e-3"])                  # 重复名计 skipped
        # 返回计数与 DB 实际行数一致
        self.assertEqual(
            AIModel.objects.filter(provider=self.provider).count(),
            len(result["imported"]),
        )

    def test_reimport_existing_all_skipped(self):
        from apps.ai_gateway.model_catalog import import_provider_models
        import_provider_models(self.provider, ["dall-e-3"])
        result = import_provider_models(self.provider, ["dall-e-3"])
        self.assertEqual(result["imported"], [])
        self.assertEqual(result["skipped"], ["dall-e-3"])


# ─── C.3 熔断跃迁并发防护 ─────────────────────────────────────────────────

class BreakerTransitionAtomicityTests(TestCase):
    """CLOSED->OPEN 用 set_nx 抢占：并发越界失败只有抢到者 _open。"""

    def setUp(self):
        reset_store()
        self.store = MemoryStore()
        self.cb = CircuitBreaker(store=self.store, cooldown_seconds=30)

    def test_second_crossing_failure_does_not_reopen(self):
        pid = 99
        # 第一名 worker 把计数推过阈值：抢到跃迁名额并 _open
        for _ in range(5):
            self.cb.record_failure(pid)
        self.assertEqual(self.cb.state(pid), OPEN)
        until = self.store.get(f"cb:{pid}:cooldown_until")
        # 模拟第二名并发失败：它读到的还是旧 state（删除 state key 复现交错），
        # 但 set_nx 抢不到名额 → 不重复 _open、不后推 cooldown_until
        self.store.delete(f"cb:{pid}:state")
        self.cb.record_failure(pid)
        self.assertEqual(self.store.get(f"cb:{pid}:cooldown_until"), until)

    def test_record_success_clears_transition_marker(self):
        pid = 100
        for _ in range(5):
            self.cb.record_failure(pid)
        self.cb.record_success(pid)
        self.assertEqual(self.cb.state(pid), CLOSED)
        self.assertIsNone(self.store.get(f"cb:{pid}:transition"))  # 名额归还，可再次打开
        for _ in range(5):
            self.cb.record_failure(pid)
        self.assertEqual(self.cb.state(pid), OPEN)  # 新周期能正常打开
