"""AgentConfig 模型引用可用性（2026-11-25 用户凭证消费缺口 §3）。

覆盖：主模型五字段、四条降级链、assistant_failover 叠加校验、clone 前置校验。
规则：platform 模型人人可用；custom 模型仅属主且有启用凭证时可用。
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.ai_gateway.models import (
    AIModel, AIProvider, ProviderType, ProviderVisibility, UserProviderCredential,
)
from apps.creation.models import AgentConfig

User = get_user_model()


def _mk_user(username, is_platform_admin=False):
    return User.objects.create_user(
        username=username, password="test-pass-12345",
        is_platform_admin=is_platform_admin,
        email=f"{username}@example.com",
    )


def _mk_provider(name, *, visibility=ProviderVisibility.CUSTOM, created_by=None):
    return AIProvider.objects.create(
        name=name,
        provider_type=ProviderType.LLM,
        adapter_type="openai",
        base_url="https://api.example.com/v1",
        visibility=visibility,
        created_by=created_by,
    )


def _mk_model(provider, model_name, display_name, model_type="LLM", supports_tools=True):
    caps = {"supports_tools": True} if supports_tools else {}
    return AIModel.objects.create(
        provider=provider, model_name=model_name, display_name=display_name,
        model_type=model_type, capabilities=caps,
    )


def _errors_of(resp):
    body = resp.json()
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        return data.get("errors") or data
    return body


class AgentConfigModelReferenceTests(TestCase):
    def setUp(self):
        self.alice = _mk_user("alice")
        self.bob = _mk_user("bob")
        self.client_alice = APIClient()
        self.client_alice.force_authenticate(self.alice)
        self.client_bob = APIClient()
        self.client_bob.force_authenticate(self.bob)

        self.platform_provider = _mk_provider(
            "Platform", visibility=ProviderVisibility.PLATFORM,
        )
        self.alice_provider = _mk_provider("Alice Custom", created_by=self.alice)
        self.bob_provider = _mk_provider("Bob Custom", created_by=self.bob)

        self.platform_llm = _mk_model(self.platform_provider, "gpt-4", "GPT-4")
        self.alice_llm = _mk_model(self.alice_provider, "alice-llm", "Alice LLM")
        self.bob_llm = _mk_model(self.bob_provider, "bob-llm", "Bob LLM")
        self.platform_tts = _mk_model(
            self.platform_provider, "tts-1", "TTS-1", model_type="TTS",
        )

    def _post(self, client, payload):
        return client.post("/api/agent-configs/", payload, format="json")

    # ─── 主模型字段 ────────────────────────────────────────────────────────

    def test_platform_model_accepted(self):
        resp = self._post(self.client_alice, {
            "name": "配方", "llm_model_id": self.platform_llm.pk,
        })
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_others_custom_model_rejected(self):
        resp = self._post(self.client_alice, {
            "name": "配方", "llm_model_id": self.bob_llm.pk,
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        errors = _errors_of(resp)
        self.assertIn("llm_model_id", errors)

    def test_own_custom_model_without_credential_rejected(self):
        resp = self._post(self.client_alice, {
            "name": "配方", "llm_model_id": self.alice_llm.pk,
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("llm_model_id", _errors_of(resp))

    def test_own_custom_model_with_credential_accepted(self):
        UserProviderCredential.objects.create(
            user=self.alice, provider=self.alice_provider, encrypted_key="v1:x",
        )
        resp = self._post(self.client_alice, {
            "name": "配方", "llm_model_id": self.alice_llm.pk,
        })
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_tts_field_validated(self):
        bob_tts = _mk_model(
            self.bob_provider, "bob-tts", "Bob TTS", model_type="TTS",
        )
        resp = self._post(self.client_alice, {
            "name": "配方", "tts_model_id": bob_tts.pk,
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("tts_model_id", _errors_of(resp))

    # ─── 降级链 ────────────────────────────────────────────────────────────

    def test_failover_with_platform_model_accepted(self):
        resp = self._post(self.client_alice, {
            "name": "配方", "llm_failover": [self.platform_llm.pk],
        })
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_failover_with_others_custom_rejected(self):
        resp = self._post(self.client_alice, {
            "name": "配方", "llm_failover": [self.platform_llm.pk, self.bob_llm.pk],
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("llm_failover", _errors_of(resp))

    def test_failover_with_own_custom_without_credential_rejected(self):
        resp = self._post(self.client_alice, {
            "name": "配方", "tts_failover": [self.alice_llm.pk],
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("tts_failover", _errors_of(resp))

    def test_failover_with_nonexistent_id_rejected(self):
        resp = self._post(self.client_alice, {
            "name": "配方", "llm_failover": [999999],
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("llm_failover", _errors_of(resp))

    def test_failover_malformed_type_rejected(self):
        resp = self._post(self.client_alice, {
            "name": "配方", "llm_failover": "oops",
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("llm_failover", _errors_of(resp))

    def test_failover_falsy_non_list_rejected(self):
        """0/false/"" 不能被当成空列表绕过类型校验。"""
        resp = self._post(self.client_alice, {
            "name": "配方", "llm_failover": 0,
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("llm_failover", _errors_of(resp))

    def test_failover_numeric_strings_normalized(self):
        """历史 JSON 数字字符串在写入时归一化为 int。"""
        resp = self._post(self.client_alice, {
            "name": "配方", "llm_failover": [str(self.platform_llm.pk)],
        })
        self.assertEqual(resp.status_code, 201, resp.content)
        cfg = AgentConfig.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(cfg.llm_failover, [self.platform_llm.pk])

    def test_assistant_failover_still_enforces_supports_tools(self):
        no_tools = _mk_model(
            self.platform_provider, "plain", "Plain", supports_tools=False,
        )
        resp = self._post(self.client_alice, {
            "name": "配方", "assistant_failover": [no_tools.pk],
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("assistant_failover", _errors_of(resp))

    def test_assistant_failover_others_custom_rejected(self):
        bob_tools = _mk_model(self.bob_provider, "bob-tools", "Bob Tools")
        resp = self._post(self.client_alice, {
            "name": "配方", "assistant_failover": [bob_tools.pk],
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("assistant_failover", _errors_of(resp))


class AgentConfigCloneModelReferenceTests(TestCase):
    def setUp(self):
        self.alice = _mk_user("alice")
        self.bob = _mk_user("bob")
        self.client_bob = APIClient()
        self.client_bob.force_authenticate(self.bob)

        self.platform_provider = _mk_provider(
            "Platform", visibility=ProviderVisibility.PLATFORM,
        )
        self.alice_provider = _mk_provider("Alice Custom", created_by=self.alice)
        self.platform_llm = _mk_model(self.platform_provider, "gpt-4", "GPT-4")
        self.alice_llm = _mk_model(self.alice_provider, "alice-llm", "Alice LLM")
        # 模板由管理员建、引用 alice 私有模型（历史数据/异常模板）
        self.template = AgentConfig.objects.create(
            name="模板", llm_model=self.alice_llm, is_template=True,
            created_by=self.alice,
        )

    def test_clone_template_with_others_custom_model_rejected(self):
        resp = self.client_bob.post(
            f"/api/agent-configs/{self.template.pk}/clone/", {}, format="json"
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(AgentConfig.objects.filter(created_by=self.bob).count(), 0)

    def test_clone_template_with_platform_models_ok(self):
        template = AgentConfig.objects.create(
            name="纯平台模板", llm_model=self.platform_llm,
            llm_failover=[self.platform_llm.pk], is_template=True,
            created_by=self.alice,
        )
        resp = self.client_bob.post(
            f"/api/agent-configs/{template.pk}/clone/", {}, format="json"
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        clone = AgentConfig.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(clone.created_by, self.bob)
        self.assertEqual(clone.llm_failover, [self.platform_llm.pk])

    def test_clone_copies_assistant_compaction(self):
        compaction = {"keep_recent_tokens": 900, "auto_compact": False}
        template = AgentConfig.objects.create(
            name="压缩模板", llm_model=self.platform_llm, is_template=True,
            created_by=self.alice, assistant_compaction=compaction,
        )
        resp = self.client_bob.post(
            f"/api/agent-configs/{template.pk}/clone/", {}, format="json"
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        clone = AgentConfig.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(clone.assistant_compaction, compaction)

    def test_clone_rejects_non_list_failover(self):
        template = AgentConfig.objects.create(
            name="脏模板", llm_model=self.platform_llm, llm_failover=5,
            is_template=True, created_by=self.alice,
        )
        resp = self.client_bob.post(
            f"/api/agent-configs/{template.pk}/clone/", {}, format="json"
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(AgentConfig.objects.filter(created_by=self.bob).count(), 0)

    def test_clone_rejects_missing_model_id(self):
        template = AgentConfig.objects.create(
            name="缺模型模板", llm_model=self.platform_llm, llm_failover=[999999],
            is_template=True, created_by=self.alice,
        )
        resp = self.client_bob.post(
            f"/api/agent-configs/{template.pk}/clone/", {}, format="json"
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(AgentConfig.objects.filter(created_by=self.bob).count(), 0)

    def test_clone_rejects_assistant_model_without_tools(self):
        no_tools = _mk_model(
            self.platform_provider, "plain", "Plain", supports_tools=False,
        )
        template = AgentConfig.objects.create(
            name="无工具模板", llm_model=self.platform_llm, assistant_model=no_tools,
            is_template=True, created_by=self.alice,
        )
        resp = self.client_bob.post(
            f"/api/agent-configs/{template.pk}/clone/", {}, format="json"
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(AgentConfig.objects.filter(created_by=self.bob).count(), 0)


class AgentConfigAssistantModelsTests(TestCase):
    """2026-11-24：assistant_models 三档写入/回显/校验。"""

    def setUp(self):
        self.alice = _mk_user("alice")
        self.bob = _mk_user("bob")
        self.client_alice = APIClient()
        self.client_alice.force_authenticate(self.alice)

        self.platform_provider = _mk_provider(
            "Platform", visibility=ProviderVisibility.PLATFORM,
        )
        self.alice_provider = _mk_provider("Alice Custom", created_by=self.alice)
        self.bob_provider = _mk_provider("Bob Custom", created_by=self.bob)

        self.small = _mk_model(self.platform_provider, "small-llm", "Small LLM")
        self.medium = _mk_model(self.platform_provider, "medium-llm", "Medium LLM")
        self.large = _mk_model(self.platform_provider, "large-llm", "Large LLM")

        self.config = AgentConfig.objects.create(name="配方", created_by=self.alice)
        self.url = f"/api/agent-configs/{self.config.pk}/"

    def _patch(self, payload):
        return self.client_alice.patch(self.url, payload, format="json")

    def test_read_default_empty_object(self):
        resp = self.client_alice.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["assistant_models"], {})

    def test_three_tiers_saved_and_echoed(self):
        tiers = {"small": self.small.pk, "medium": self.medium.pk, "large": self.large.pk}
        resp = self._patch({"assistant_models": tiers})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["assistant_models"], tiers)
        self.config.refresh_from_db()
        self.assertEqual(self.config.assistant_models, tiers)

    def test_null_clears_tier(self):
        self._patch({"assistant_models": {"small": self.small.pk, "medium": self.medium.pk}})
        resp = self._patch({"assistant_models": {"small": None}})
        self.assertEqual(resp.status_code, 200, resp.content)
        # PATCH 按档合并：null 只删除 small，medium 保持不变
        self.assertEqual(
            resp.json()["data"]["assistant_models"],
            {"medium": self.medium.pk},
        )

    def test_empty_object_clears_all_tiers(self):
        tiers = {"small": self.small.pk, "medium": self.medium.pk}
        self._patch({"assistant_models": tiers})
        resp = self._patch({"assistant_models": {}})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["assistant_models"], {})

    def test_small_tier_allows_no_tools(self):
        """small 档用于压缩摘要（supports_tool_calls=False），允许无工具模型。"""
        no_tools = _mk_model(
            self.platform_provider, "plain-small", "Plain Small", supports_tools=False,
        )
        resp = self._patch({"assistant_models": {"small": no_tools.pk}})
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_empty_object_accepted(self):
        resp = self._patch({"assistant_models": {}})
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_unknown_tier_key_rejected(self):
        resp = self._patch({"assistant_models": {"mid": self.small.pk}})
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("assistant_models", _errors_of(resp))

    def test_non_llm_model_rejected(self):
        image_model = _mk_model(
            self.platform_provider, "img", "Img", model_type="IMAGE",
        )
        resp = self._patch({"assistant_models": {"small": image_model.pk}})
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("assistant_models", _errors_of(resp))

    def test_no_tools_rejected(self):
        no_tools = _mk_model(
            self.platform_provider, "plain", "Plain", supports_tools=False,
        )
        resp = self._patch({"assistant_models": {"medium": no_tools.pk}})
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_inactive_model_rejected(self):
        inactive = _mk_model(self.platform_provider, "off", "Off")
        AIModel.objects.filter(pk=inactive.pk).update(is_active=False)
        resp = self._patch({"assistant_models": {"large": inactive.pk}})
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_others_custom_model_rejected(self):
        bob_llm = _mk_model(self.bob_provider, "bob-llm", "Bob LLM")
        resp = self._patch({"assistant_models": {"small": bob_llm.pk}})
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("assistant_models", _errors_of(resp))

    def test_own_custom_model_with_credential_accepted(self):
        alice_llm = _mk_model(self.alice_provider, "alice-llm", "Alice LLM")
        UserProviderCredential.objects.create(
            user=self.alice, provider=self.alice_provider, encrypted_key="v1:x",
        )
        resp = self._patch({"assistant_models": {"small": alice_llm.pk}})
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_non_dict_rejected(self):
        resp = self._patch({"assistant_models": [1, 2]})
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_patch_other_field_keeps_tiers(self):
        tiers = {"small": self.small.pk}
        self._patch({"assistant_models": tiers})
        resp = self._patch({"name": "改名"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["assistant_models"], tiers)
