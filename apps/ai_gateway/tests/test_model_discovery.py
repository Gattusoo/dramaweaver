"""模型发现测试：fetch_provider_models（live/static）+ 两个端点 + 导入幂等 + 权限。"""
import json
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APITestCase
import httpx

from apps.ai_gateway.model_catalog import fetch_provider_models, KNOWN_MODELS
from apps.ai_gateway.models import AIModel, AIProvider
from apps.creation.models import AgentConfig
from apps.core.system.models import CustomUser, UserRole


def _json_response(payload):
    return httpx.Response(200, json=payload, request=httpx.Request("GET", "https://example.com"))


class _CatalogClientPatcher:
    """把 model_catalog 里的出站请求替换为带 MockTransport 的 httpx.Client。

    model_catalog 已收口到 apps.ai_gateway.http.safe_request（SSRF 统一出口），
    此处 patch http 层的 httpx.Client 并绕过 check_url/check_peer，
    让模型发现逻辑测试命中 MockTransport。
    """

    def __init__(self, handler):
        self.transport = httpx.MockTransport(handler)
        self._real = httpx.Client

    def _factory(self, *args, **kwargs):
        kwargs.pop("transport", None)
        kwargs.pop("follow_redirects", None)
        return self._real(transport=self.transport, **kwargs)

    def __enter__(self):
        self._p_client = patch("apps.ai_gateway.http.httpx.Client", self._factory)
        self._p_check_url = patch("apps.ai_gateway.http.check_url", lambda url: "")
        self._p_check_peer = patch("apps.ai_gateway.http.check_peer", lambda host, peer: None)
        self._p_client.start()
        self._p_check_url.start()
        self._p_check_peer.start()
        return self

    def __exit__(self, *exc):
        self._p_client.stop()
        self._p_check_url.stop()
        self._p_check_peer.stop()
        return False


class FetchProviderModelsTests(SimpleTestCase):
    def test_live_openai_compat(self):
        provider = AIProvider(adapter_type="openai", provider_type="LLM",
                              base_url="https://api.openai.com/v1", api_key_encrypted="k")

        def handler(request):
            assert request.url.path == "/v1/models"
            return _json_response({"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]})

        with _CatalogClientPatcher(handler):
            result = fetch_provider_models(provider)
        self.assertEqual(result["source"], "live")
        names = [m["model_name"] for m in result["models"]]
        self.assertEqual(names, ["gpt-4o", "gpt-4o-mini"])
        self.assertTrue(all(m["model_type"] == "LLM" for m in result["models"]))

    def test_live_claude(self):
        provider = AIProvider(adapter_type="claude", provider_type="LLM",
                              base_url="https://api.anthropic.com", api_key_encrypted="k")

        def handler(request):
            assert request.url.path == "/v1/models"
            return _json_response({"data": [{"id": "claude-3-5-sonnet",
                                             "display_name": "Claude 3.5 Sonnet"}]})

        with _CatalogClientPatcher(handler):
            result = fetch_provider_models(provider)
        self.assertEqual(result["source"], "live")
        self.assertEqual(result["models"][0]["display_name"], "Claude 3.5 Sonnet")

    def test_live_failure_falls_back_to_static(self):
        provider = AIProvider(adapter_type="deepseek", provider_type="LLM",
                              base_url="https://api.deepseek.com/v1", api_key_encrypted="k")

        def handler(request):
            return httpx.Response(401, request=request)

        with _CatalogClientPatcher(handler):
            result = fetch_provider_models(provider)
        # LLM 无静态清单 -> 空，但不抛 500
        self.assertEqual(result["source"], "static")
        self.assertEqual(result["models"], [])

    def test_non_llm_static(self):
        for adapter_type in ("flux", "kling", "minimax_tts"):
            provider = AIProvider(adapter_type=adapter_type,
                                  provider_type={"flux": "IMAGE", "kling": "VIDEO",
                                                 "minimax_tts": "TTS"}[adapter_type],
                                  base_url="https://example.com", api_key_encrypted="k")
            result = fetch_provider_models(provider)
            self.assertEqual(result["source"], "static", adapter_type)
            self.assertTrue(result["models"], adapter_type)
            for m in result["models"]:
                self.assertEqual(m["model_type"], provider.provider_type)

    def test_unknown_adapter_type_empty(self):
        provider = AIProvider(adapter_type="nope", provider_type="IMAGE",
                              base_url="https://example.com", api_key_encrypted="k")
        result = fetch_provider_models(provider)
        self.assertEqual(result["models"], [])


class ProviderModelsAPITests(APITestCase):
    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username="catadmin", password="Passw0rd!123", is_platform_admin=True)
        self.nonadmin = CustomUser.objects.create_user(
            username="catprod", password="Passw0rd!123", role=UserRole.PRODUCER)

    def _kling_provider(self):
        return AIProvider.objects.create(
            name="可灵", provider_type="VIDEO", adapter_type="kling",
            base_url="https://api.klingai.com", api_key_encrypted="ak:sk", priority=1)

    def test_models_endpoint_static(self):
        provider = self._kling_provider()
        self.client.force_authenticate(self.admin)
        resp = self.client.get(f"/api/ai-gateway/providers/{provider.pk}/models/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["source"], "static")
        names = [m["model_name"] for m in resp.data["models"]]
        self.assertIn("kling-v1", names)

    def test_models_endpoint_404(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/ai-gateway/providers/99999/models/")
        self.assertEqual(resp.status_code, 404)

    def test_requires_admin(self):
        provider = self._kling_provider()
        self.client.force_authenticate(self.nonadmin)
        resp = self.client.get(f"/api/ai-gateway/providers/{provider.pk}/models/")
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(f"/api/ai-gateway/providers/{provider.pk}/models/import/",
                                {"model_names": ["kling-v1"]}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_import_creates_models(self):
        provider = self._kling_provider()
        self.client.force_authenticate(self.admin)
        resp = self.client.post(f"/api/ai-gateway/providers/{provider.pk}/models/import/",
                                {"model_names": ["kling-v1", "kling-v2"]}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.data["imported"]), {"kling-v1", "kling-v2"})
        created = AIModel.objects.filter(provider=provider)
        self.assertEqual(created.count(), 2)
        self.assertTrue(all(m.model_type == "VIDEO" for m in created))
        v1 = created.get(model_name="kling-v1")
        self.assertEqual(v1.display_name, "可灵 V1")

    def test_import_idempotent(self):
        provider = self._kling_provider()
        self.client.force_authenticate(self.admin)
        body = {"model_names": ["kling-v1"]}
        self.client.post(f"/api/ai-gateway/providers/{provider.pk}/models/import/", body, format="json")
        resp = self.client.post(f"/api/ai-gateway/providers/{provider.pk}/models/import/", body, format="json")
        self.assertEqual(resp.data["imported"], [])
        self.assertEqual(resp.data["skipped"], ["kling-v1"])
        self.assertEqual(AIModel.objects.filter(provider=provider, model_name="kling-v1").count(), 1)

    def test_import_unknown_name_uses_provider_type(self):
        provider = self._kling_provider()
        self.client.force_authenticate(self.admin)
        resp = self.client.post(f"/api/ai-gateway/providers/{provider.pk}/models/import/",
                                {"model_names": ["kling-custom-x"]}, format="json")
        self.assertEqual(resp.status_code, 200)
        m = AIModel.objects.get(provider=provider, model_name="kling-custom-x")
        self.assertEqual(m.model_type, "VIDEO")
        self.assertEqual(m.display_name, "kling-custom-x")

    def test_import_default_imports_all_known(self):
        provider = self._kling_provider()
        self.client.force_authenticate(self.admin)
        resp = self.client.post(f"/api/ai-gateway/providers/{provider.pk}/models/import/", {}, format="json")
        self.assertEqual(resp.status_code, 200)
        expected = {m["model_name"] for m in KNOWN_MODELS["kling"]}
        self.assertEqual(set(resp.data["imported"]), expected)


class GatewayVideoIntegrationTests(SimpleTestCase):
    """确认 VIDEO 走同步返回 {url} 的假适配器时，gateway._invoke 契约不破（无需改动）。"""

    def test_video_sync_contract(self):
        from apps.ai_gateway.gateway import AIGateway
        from apps.ai_gateway.redis_client import MemoryStore, reset_store

        reset_store()

        class FakeVideoAdapter:
            def image_to_video(self, image_url, parameters=None, *, wait=True):
                return {"url": "http://v/x.mp4", "duration_seconds": 5}

        class _Cfg:
            video_model = None
            video_failover = None

        gateway = AIGateway(adapter_factory=lambda p: FakeVideoAdapter(), store=MemoryStore())
        # 无候选时 router 会走全局 fallback（DB 查询）；SimpleTestCase 无 DB，
        # 因此直接验证 _invoke 分发逻辑。
        result = gateway._invoke(FakeVideoAdapter(), _Model(), "VIDEO",
                                 {"image_url": "http://img/1.png", "duration": 5})
        self.assertEqual(result["url"], "http://v/x.mp4")


class _Model:
    model_name = "kling-v1"
