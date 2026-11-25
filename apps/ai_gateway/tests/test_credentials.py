"""UserProviderCredential 与 AIProvider visibility/行级权限测试。

任务 2.1-2.4 验证场景：
- 密文落库、明文不落日志/审计
- GET 不回明文（只回 key_last4 + label）
- verify 端点：成功/失败（401 分类文案）两态
- 属主私有：他人（含属主查成员）不可见
- AIProvider：管理员可建 platform（可内网）；普通用户仅 custom（强制公网 HTTPS）
- 配方引用校验：引用属主无凭证的 custom 供应商模型被拒
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.ai_gateway.models import (
    AIModel, AIProvider, ProviderType, ProviderVisibility, UserProviderCredential,
)
from apps.core.utils.encryption import EncryptionError, decrypt_value, encrypt_value

User = get_user_model()


def _mk_user(username, is_staff=False, is_platform_admin=False):
    return User.objects.create_user(
        username=username, password="test-pass-12345", is_staff=is_staff,
        is_platform_admin=is_platform_admin,
        email=f"{username}@example.com",
    )


def _mk_provider(name, *, visibility=ProviderVisibility.CUSTOM, created_by=None,
                 base_url="https://api.example.com/v1", api_key=""):
    return AIProvider.objects.create(
        name=name,
        provider_type=ProviderType.LLM,
        adapter_type="openai",
        base_url=base_url,
        api_key_encrypted=api_key,
        visibility=visibility,
        created_by=created_by,
    )


def _unwrap(resp):
    """apps.core envelope：{code, msg, data}；返回 (data, raw_json)。"""
    body = resp.json()
    if isinstance(body, dict) and "data" in body and "code" in body:
        return body["data"], body
    return body, body


def _errors_of(resp):
    """从 envelope 错误响应里取 errors dict。"""
    body = resp.json()
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        return data.get("errors") or data
    return body


class EncryptionUtilTests(TestCase):
    """apps.core.utils.encryption：Fernet 加解密 + 版本前缀 + 目的隔离。"""

    def test_encrypt_decrypt_round_trip(self):
        cipher = encrypt_value("sk-test-12345", purpose="user_provider_credential")
        self.assertTrue(cipher.startswith("v1:"))
        self.assertNotIn("sk-test-12345", cipher)  # 密文不含明文
        plain = decrypt_value(cipher, purpose="user_provider_credential")
        self.assertEqual(plain, "sk-test-12345")

    def test_wrong_purpose_fails(self):
        """HKDF info 隔离：用错的 purpose 解密必须失败。"""
        cipher = encrypt_value("sk-test", purpose="user_provider_credential")
        with self.assertRaises(EncryptionError):
            decrypt_value(cipher, purpose="other_purpose")

    def test_missing_version_prefix_fails(self):
        with self.assertRaises(EncryptionError):
            decrypt_value("not-a-token", purpose="user_provider_credential")

    def test_empty_string_passthrough(self):
        self.assertEqual(encrypt_value("", purpose="x"), "")
        self.assertEqual(decrypt_value("", purpose="x"), "")


class CredentialModelTests(TestCase):
    """UserProviderCredential：set_key/get_key/key_last4/UNIQUE 约束。"""

    def setUp(self):
        self.user = _mk_user("alice")
        self.provider = _mk_provider("Custom OpenAI", created_by=self.user)

    def test_set_key_encrypts_and_decrypts(self):
        cred = UserProviderCredential(user=self.user, provider=self.provider)
        cred.set_key("sk-plain-text-key")
        cred.save()
        # 落库字段不含明文
        self.assertNotIn("sk-plain-text-key", cred.encrypted_key)
        self.assertTrue(cred.encrypted_key.startswith("v1:"))
        # 读取能解密回原值
        self.assertEqual(cred.get_key(), "sk-plain-text-key")

    def test_key_last4(self):
        cred = UserProviderCredential(user=self.user, provider=self.provider)
        cred.set_key("sk-abcdefghijklmnop")
        self.assertEqual(cred.key_last4, "mnop")

    def test_key_last4_handles_short_and_empty(self):
        cred = UserProviderCredential(user=self.user, provider=self.provider)
        cred.set_key("ab")
        self.assertEqual(cred.key_last4, "")  # 长度 <4 不透出
        cred2 = UserProviderCredential(user=self.user, provider=self.provider)
        cred2.encrypted_key = ""
        self.assertEqual(cred2.key_last4, "")

    def test_unique_user_provider(self):
        UserProviderCredential.objects.create(
            user=self.user, provider=self.provider, encrypted_key="v1:xxx",
        )
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            UserProviderCredential.objects.create(
                user=self.user, provider=self.provider, encrypted_key="v1:yyy",
            )


class CredentialAPITests(TestCase):
    """凭证 API：CRUD / 末 4 位回显 / verify / 属主私有。"""

    def setUp(self):
        self.alice = _mk_user("alice")
        self.bob = _mk_user("bob")
        self.provider = _mk_provider("Custom OpenAI", created_by=self.alice)
        self.client_alice = APIClient()
        self.client_alice.force_authenticate(self.alice)
        self.client_bob = APIClient()
        self.client_bob.force_authenticate(self.bob)

    def _create_credential(self, client, plain="sk-test-alice"):
        return client.post("/api/user-credentials/", {
            "provider": self.provider.pk,
            "label": "主力 Key",
            "api_key": plain,
        }, format="json")

    def test_create_and_get_returns_last4_only(self):
        resp = self._create_credential(self.client_alice)
        self.assertEqual(resp.status_code, 201, resp.content)
        body, _ = _unwrap(resp)
        self.assertEqual(body["key_last4"], "lice")
        self.assertEqual(body["label"], "主力 Key")
        self.assertNotIn("api_key", body)  # 读侧不回明文
        self.assertNotIn("encrypted_key", body)

        # GET detail 也不回明文
        detail = self.client_alice.get(f"/api/user-credentials/{body['id']}/")
        self.assertEqual(detail.status_code, 200)
        detail_body, _ = _unwrap(detail)
        self.assertNotIn("api_key", detail_body)
        self.assertEqual(detail_body["key_last4"], "lice")

    def test_credential_is_owner_only(self):
        """bob 看不到 alice 的凭证（queryset 过滤）；用 alice 的 id 直接 GET 也 404。"""
        resp = self._create_credential(self.client_alice)
        body, _ = _unwrap(resp)
        cred_id = body["id"]

        # bob list 应为空
        bob_list = self.client_bob.get("/api/user-credentials/")
        self.assertEqual(bob_list.status_code, 200)
        bob_body, _ = _unwrap(bob_list)
        results = bob_body.get("results", bob_body) if isinstance(bob_body, dict) else bob_body
        self.assertEqual(len(results), 0)

        # bob 直 GET alice 的凭证 404
        bob_detail = self.client_bob.get(f"/api/user-credentials/{cred_id}/")
        self.assertEqual(bob_detail.status_code, 404)

    def test_credential_only_for_custom_provider(self):
        """platform 行不可作为用户凭证目标。"""
        platform_provider = _mk_provider(
            "Platform OpenAI",
            visibility=ProviderVisibility.PLATFORM,
            api_key="platform-key",
        )
        resp = self.client_alice.post("/api/user-credentials/", {
            "provider": platform_provider.pk,
            "api_key": "sk-x",
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        errors = _errors_of(resp)
        self.assertIn("provider", errors)

    def test_verify_endpoint_marks_last_verified_at(self):
        create_resp = self._create_credential(self.client_alice)
        body, _ = _unwrap(create_resp)
        cred_id = body["id"]

        # mock safe_request 返回 200
        class _FakeResp:
            status_code = 200

        with patch("apps.ai_gateway.views.safe_request", return_value=_FakeResp()):
            resp = self.client_alice.post(f"/api/user-credentials/{cred_id}/verify/")
        self.assertEqual(resp.status_code, 200)
        body, _ = _unwrap(resp)
        self.assertTrue(body["ok"])
        self.assertIsNotNone(body["last_verified_at"])

    def test_verify_endpoint_401_classified(self):
        """verify 收到 401 → ok=False + 中文分类文案。"""
        create_resp = self._create_credential(self.client_alice)
        body, _ = _unwrap(create_resp)
        cred_id = body["id"]

        class _FakeResp:
            status_code = 401

        with patch("apps.ai_gateway.views.safe_request", return_value=_FakeResp()):
            resp = self.client_alice.post(f"/api/user-credentials/{cred_id}/verify/")
        body, _ = _unwrap(resp)
        self.assertFalse(body["ok"])
        self.assertIn("鉴权", body["reason"])

    # ─── 2026-11-25 消费缺口：属主绑定 / 重复 400 / 稳定排序 ───────────────

    def test_credential_owner_binding_rejects_others_custom_provider(self):
        """凭证只能配在本人创建的自定义供应商上；他人 custom → 400。"""
        resp = self.client_bob.post("/api/user-credentials/", {
            "provider": self.provider.pk,  # bob 不是该 custom 行属主
            "api_key": "sk-bob",
        }, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)
        errors = _errors_of(resp)
        self.assertIn("provider", errors)
        self.assertIn("只能为自己的自定义供应商", str(errors["provider"]))
        self.assertEqual(UserProviderCredential.objects.count(), 0)

    def test_duplicate_credential_returns_400_not_500(self):
        first = self._create_credential(self.client_alice)
        self.assertEqual(first.status_code, 201)
        second = self._create_credential(self.client_alice, plain="sk-second")
        self.assertEqual(second.status_code, 400, second.content)
        errors = _errors_of(second)
        self.assertEqual(errors["provider"], ["该供应商已存在凭证。"])
        self.assertEqual(
            UserProviderCredential.objects.filter(
                user=self.alice, provider=self.provider
            ).count(),
            1,
        )

    def test_patch_same_credential_not_treated_as_duplicate(self):
        """更新排除自身：PATCH 自己的凭证改 label 不应触发重复校验。"""
        create_resp = self._create_credential(self.client_alice)
        body, _ = _unwrap(create_resp)
        cred_id = body["id"]
        resp = self.client_alice.patch(
            f"/api/user-credentials/{cred_id}/", {"label": "改名"}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_integrity_error_on_create_returns_400(self):
        """并发兜底：预查通过但插入撞唯一约束 → 400 同形文案，不 500。"""
        from django.db import IntegrityError as DjangoIntegrityError
        from rest_framework import serializers
        from rest_framework.test import APIRequestFactory

        from apps.ai_gateway.serializers import UserProviderCredentialSerializer

        # 模拟另一个并发事务已插入同一 (user, provider) 行
        UserProviderCredential.objects.create(
            user=self.alice, provider=self.provider, encrypted_key="v1:exists",
        )
        request = APIRequestFactory().post("/api/user-credentials/")
        request.user = self.alice
        serializer = UserProviderCredentialSerializer(context={"request": request})
        with patch(
            "apps.ai_gateway.models.UserProviderCredential.save",
            side_effect=DjangoIntegrityError("uniq_user_provider_credential"),
        ):
            with self.assertRaises(serializers.ValidationError) as ctx:
                serializer.create({"provider": self.provider, "api_key": "sk-new"})
        self.assertEqual(ctx.exception.detail["provider"], ["该供应商已存在凭证。"])

    def test_integrity_error_on_update_returns_400(self):
        """update() 同样把并发唯一冲突翻译为 400，而非 500。"""
        from django.db import IntegrityError as DjangoIntegrityError
        from rest_framework import serializers
        from rest_framework.test import APIRequestFactory

        from apps.ai_gateway.serializers import UserProviderCredentialSerializer

        other_provider = _mk_provider("Other Custom", created_by=self.alice)
        credential = UserProviderCredential.objects.create(
            user=self.alice, provider=self.provider, encrypted_key="v1:a",
        )
        UserProviderCredential.objects.create(
            user=self.alice, provider=other_provider, encrypted_key="v1:b",
        )
        request = APIRequestFactory().patch("/")
        request.user = self.alice
        serializer = UserProviderCredentialSerializer(
            instance=credential, context={"request": request},
        )
        with patch(
            "apps.ai_gateway.models.UserProviderCredential.save",
            side_effect=DjangoIntegrityError("uniq_user_provider_credential"),
        ):
            with self.assertRaises(serializers.ValidationError) as ctx:
                serializer.update(
                    credential,
                    {"provider": other_provider, "api_key": "sk-new"},
                )
        self.assertEqual(ctx.exception.detail["provider"], ["该供应商已存在凭证。"])

    def test_change_provider_requires_new_api_key(self):
        create_resp = self._create_credential(self.client_alice)
        body, _ = _unwrap(create_resp)
        other_provider = _mk_provider("Other Custom", created_by=self.alice)
        resp = self.client_alice.patch(
            f"/api/user-credentials/{body['id']}/",
            {"provider": other_provider.pk}, format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("api_key", _errors_of(resp))

    def test_change_provider_with_key_clears_verification(self):
        from django.utils import timezone

        create_resp = self._create_credential(self.client_alice)
        body, _ = _unwrap(create_resp)
        credential = UserProviderCredential.objects.get(pk=body["id"])
        credential.last_verified_at = timezone.now()
        credential.save(update_fields=["last_verified_at"])
        other_provider = _mk_provider("Other Custom", created_by=self.alice)

        resp = self.client_alice.patch(
            f"/api/user-credentials/{credential.pk}/",
            {"provider": other_provider.pk, "api_key": "sk-new"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        credential.refresh_from_db()
        self.assertIsNone(credential.last_verified_at)
        self.assertEqual(credential.provider_id, other_provider.pk)

    def test_credential_list_stable_order(self):
        """分页稳定：两次请求的 id 顺序一致（-created_at, -id）。"""
        providers = [self.provider] + [
            _mk_provider(f"P{i}", created_by=self.alice) for i in range(2)
        ]
        created = []
        for p in providers:
            resp = self.client_alice.post("/api/user-credentials/", {
                "provider": p.pk, "api_key": "sk-test",
            }, format="json")
            self.assertEqual(resp.status_code, 201, resp.content)
            body, _ = _unwrap(resp)
            created.append(body["id"])

        def ids():
            body, _ = _unwrap(self.client_alice.get("/api/user-credentials/"))
            rows = body.get("results", body) if isinstance(body, dict) else body
            return [r["id"] for r in rows]

        first, second = ids(), ids()
        self.assertEqual(len(first), 3)
        self.assertEqual(first, second)
        # 断言文档承诺的 newest-first（-created_at, -id），而非仅「两次一致」
        self.assertEqual(first, list(reversed(created)))


class CredentialRuntimeOwnershipTests(TestCase):
    """运行期凭证解析还须校验供应商属主（历史越权绑定行不生效）。"""

    def setUp(self):
        self.alice = _mk_user("runtime-alice")
        self.bob = _mk_user("runtime-bob")
        self.provider = _mk_provider("Bob Private", created_by=self.bob)

    def test_cross_owner_legacy_credential_not_resolved(self):
        UserProviderCredential.objects.create(
            user=self.alice, provider=self.provider, encrypted_key="v1:x",
        )
        from apps.ai_gateway.credentials import resolve_api_key, resolve_credential

        self.assertIsNone(resolve_credential(self.alice, self.provider))
        self.assertEqual(resolve_api_key(self.alice, self.provider), (None, ""))

    def test_owner_credential_still_resolved(self):
        credential = UserProviderCredential.objects.create(
            user=self.bob, provider=self.provider, encrypted_key="v1:x",
        )
        from apps.ai_gateway.credentials import resolve_credential

        self.assertEqual(resolve_credential(self.bob, self.provider), credential)


class AIProviderVisibilityTests(TestCase):
    """AIProvider visibility 字段 + 行级权限（任务 2.3）。"""

    def setUp(self):
        self.admin = _mk_user("admin", is_platform_admin=True)
        self.alice = _mk_user("alice")
        self.client_admin = APIClient()
        self.client_admin.force_authenticate(self.admin)
        self.client_alice = APIClient()
        self.client_alice.force_authenticate(self.alice)

    def test_admin_can_create_platform_with_internal_url(self):
        """管理员可建 platform 行（允许内网白名单场景，仅校验通过即可）。"""
        resp = self.client_admin.post("/api/ai-providers/", {
            "name": "Platform DS",
            "provider_type": "LLM",
            "adapter_type": "openai",
            "base_url": "https://api.deepseek.com/v1",
            "api_key_encrypted": "sk-platform",
            "visibility": "platform",
        }, format="json")
        self.assertIn(resp.status_code, (200, 201), resp.content)
        body, _ = _unwrap(resp)
        self.assertEqual(body["visibility"], "platform")
        self.assertTrue(body["has_key"])

    def test_user_cannot_create_platform(self):
        """普通用户建 platform 行被拒。"""
        resp = self.client_alice.post("/api/ai-providers/", {
            "name": "Fake Platform",
            "provider_type": "LLM",
            "adapter_type": "openai",
            "base_url": "https://api.example.com/v1",
            "api_key_encrypted": "sk-x",
            "visibility": "platform",
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        errors = _errors_of(resp)
        self.assertIn("visibility", errors)

    def test_user_can_create_custom_https_only(self):
        """普通用户可建 custom 行（强制 HTTPS）。"""
        resp = self.client_alice.post("/api/ai-providers/", {
            "name": "My OpenAI",
            "provider_type": "LLM",
            "adapter_type": "openai",
            "base_url": "https://api.example.com/v1",
            "visibility": "custom",
        }, format="json")
        self.assertIn(resp.status_code, (200, 201), resp.content)
        body, _ = _unwrap(resp)
        self.assertEqual(body["visibility"], "custom")

    def test_user_custom_rejects_http(self):
        """custom 行强制 HTTPS（http 拒绝）。"""
        resp = self.client_alice.post("/api/ai-providers/", {
            "name": "My HTTP",
            "provider_type": "LLM",
            "adapter_type": "openai",
            "base_url": "http://api.example.com/v1",
            "visibility": "custom",
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        errors = _errors_of(resp)
        self.assertIn("base_url", errors)

    def test_custom_rejects_api_key(self):
        """custom 行不允许携带 api_key_encrypted（Key 走凭证表）。"""
        resp = self.client_alice.post("/api/ai-providers/", {
            "name": "My OpenAI",
            "provider_type": "LLM",
            "adapter_type": "openai",
            "base_url": "https://api.example.com/v1",
            "api_key_encrypted": "sk-x",
            "visibility": "custom",
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        errors = _errors_of(resp)
        self.assertIn("api_key_encrypted", errors)

    def test_user_patch_platform_row_returns_403(self):
        """对象级权限先于请求体校验：PATCH platform 行 → 403（不是 400）。"""
        platform_provider = _mk_provider(
            "Platform DS", visibility=ProviderVisibility.PLATFORM, api_key="k",
        )
        resp = self.client_alice.patch(
            f"/api/ai-providers/{platform_provider.pk}/", {"name": "hack"}, format="json"
        )
        self.assertEqual(resp.status_code, 403, resp.content)
        platform_provider.refresh_from_db()
        self.assertEqual(platform_provider.name, "Platform DS")

    def test_user_patch_own_custom_returns_200(self):
        custom = _mk_provider("Mine", created_by=self.alice)
        resp = self.client_alice.patch(
            f"/api/ai-providers/{custom.pk}/", {"name": "Mine 2"}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_user_patch_others_custom_returns_404(self):
        bob = _mk_user("bob")
        client_bob = APIClient()
        client_bob.force_authenticate(bob)
        custom = _mk_provider("Alice's", created_by=self.alice)
        resp = client_bob.patch(
            f"/api/ai-providers/{custom.pk}/", {"name": "hack"}, format="json"
        )
        self.assertEqual(resp.status_code, 404)

    @override_settings(SERVICE_FEE_PER_CALL=5)
    def test_service_fee_per_call_readonly_disclosure(self):
        """custom 行回显部署费率；platform 行为 null；只读不可写。"""
        platform = _mk_provider(
            "Platform DS", visibility=ProviderVisibility.PLATFORM, api_key="k",
        )
        custom = _mk_provider("Mine", created_by=self.alice)
        body, _ = _unwrap(self.client_alice.get("/api/ai-providers/"))
        rows = body.get("results", body) if isinstance(body, dict) else body
        by_id = {r["id"]: r for r in rows}
        self.assertIsNone(by_id[platform.pk]["service_fee_per_call"])
        self.assertEqual(by_id[custom.pk]["service_fee_per_call"], 5)
        # 只读：PATCH 携带该字段被忽略，实际仍由配置决定
        resp = self.client_alice.patch(
            f"/api/ai-providers/{custom.pk}/",
            {"name": "Mine2", "service_fee_per_call": 999}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body, _ = _unwrap(resp)
        self.assertEqual(body["service_fee_per_call"], 5)


class RecipeReferenceValidationTests(TestCase):
    """任务 2.4：配方引用校验（平台模型 or 属主凭证可用）。

    本测试通过 is_model_usable_for_user 辅助函数验证；视图层在配方保存时调用。
    """

    def setUp(self):
        self.alice = _mk_user("alice")
        self.platform_provider = _mk_provider(
            "Platform", visibility=ProviderVisibility.PLATFORM, api_key="k",
        )
        self.custom_provider = _mk_provider(
            "My Custom", visibility=ProviderVisibility.CUSTOM, created_by=self.alice,
        )
        self.platform_model = AIModel.objects.create(
            provider=self.platform_provider, model_name="gpt-4",
            display_name="GPT-4", model_type="LLM",
        )
        self.custom_model = AIModel.objects.create(
            provider=self.custom_provider, model_name="my-model",
            display_name="My Model", model_type="LLM",
        )

    def test_platform_model_usable_for_anyone(self):
        from apps.ai_gateway.credentials import is_model_usable_for_user
        ok, reason = is_model_usable_for_user(self.alice, self.platform_model)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_custom_model_usable_with_credential(self):
        from apps.ai_gateway.credentials import is_model_usable_for_user
        UserProviderCredential.objects.create(
            user=self.alice, provider=self.custom_provider, encrypted_key="v1:x",
        )
        ok, _ = is_model_usable_for_user(self.alice, self.custom_model)
        self.assertTrue(ok)

    def test_custom_model_unusable_without_credential(self):
        from apps.ai_gateway.credentials import is_model_usable_for_user
        bob = _mk_user("bob")
        ok, reason = is_model_usable_for_user(bob, self.custom_model)
        self.assertFalse(ok)
        # 缺凭证或私有供应商两种文案均可
        self.assertTrue("凭证" in reason or "私有供应商" in reason)

    def test_custom_model_unusable_with_inactive_credential(self):
        from apps.ai_gateway.credentials import is_model_usable_for_user
        UserProviderCredential.objects.create(
            user=self.alice, provider=self.custom_provider,
            encrypted_key="v1:x", is_active=False,
        )
        ok, _ = is_model_usable_for_user(self.alice, self.custom_model)
        self.assertFalse(ok)


# ─── 任务 3：渠道解析链 + 降级标注 + 服务费埋点 ──────────────────────────────


class ChannelResolutionChainTests(TestCase):
    """任务 3.1：build_*_chain 在 user 提供时跳过属主无凭证的 custom 节点。"""

    def setUp(self):
        from apps.creation.models.agent import AgentConfig

        self.alice = _mk_user("alice")
        self.bob = _mk_user("bob")
        self.platform_provider = _mk_provider(
            "Platform", visibility=ProviderVisibility.PLATFORM, api_key="k",
        )
        self.platform_model = AIModel.objects.create(
            provider=self.platform_provider, model_name="gpt-4",
            display_name="GPT-4", model_type="LLM",
            capabilities={"supports_tools": True},
        )
        self.custom_provider = _mk_provider(
            "Alice's Custom", visibility=ProviderVisibility.CUSTOM, created_by=self.alice,
        )
        self.custom_model = AIModel.objects.create(
            provider=self.custom_provider, model_name="my-model",
            display_name="My Model", model_type="LLM",
            capabilities={"supports_tools": True},
        )
        # alice 的配方：custom 模型为主，platform 模型为全局 fallback
        self.agent_config = AgentConfig.objects.create(
            name="alice-recipe",
            llm_model=self.custom_model,
            created_by=self.alice,
        )

    def test_chain_skips_custom_when_user_has_no_credential(self):
        """bob 的配方引用 alice 的 custom provider：bob 无凭证 → custom 节点被跳过。"""
        from apps.ai_gateway.router import build_candidate_chain

        # bob 视角的 chain（user=bob，但 custom provider 属 alice；按设计属主=配方 created_by）
        # 此处我们直接传 user=bob 验证 custom 节点被过滤
        chain = build_candidate_chain(self.agent_config, "LLM", user=self.bob)
        # bob 无 alice 的 custom provider 凭证 → custom 节点跳过；仅剩 platform fallback
        providers_in_chain = [p for _, p in chain]
        self.assertNotIn(self.custom_provider, providers_in_chain)
        self.assertIn(self.platform_provider, providers_in_chain)

    def test_chain_includes_custom_when_user_has_credential(self):
        """alice（属主）有凭证 → custom 节点保留。"""
        from apps.ai_gateway.router import build_candidate_chain

        cred = UserProviderCredential(user=self.alice, provider=self.custom_provider)
        cred.set_key("sk-alice")
        cred.save()

        chain = build_candidate_chain(self.agent_config, "LLM", user=self.alice)
        providers_in_chain = [p for _, p in chain]
        self.assertIn(self.custom_provider, providers_in_chain)

    def test_chain_without_user_skips_custom(self):
        """user=None（无属主上下文）时保守跳过 custom 节点：无法判定凭证可用性。"""
        from apps.ai_gateway.router import build_candidate_chain

        chain = build_candidate_chain(self.agent_config, "LLM", user=None)
        # 无 user 时无法验证 custom 凭证 → 保守跳过；platform 节点不受影响
        providers_in_chain = [p for _, p in chain]
        self.assertNotIn(self.custom_provider, providers_in_chain)
        self.assertIn(self.platform_provider, providers_in_chain)


class AnnotateChainWithSourceTests(TestCase):
    """任务 3.2：annotate_chain_with_source 由 provider.visibility 推导 source。"""

    def setUp(self):
        self.alice = _mk_user("alice")
        self.platform_provider = _mk_provider(
            "P", visibility=ProviderVisibility.PLATFORM, api_key="k",
        )
        self.custom_provider = _mk_provider(
            "C", visibility=ProviderVisibility.CUSTOM, created_by=self.alice,
        )
        self.platform_model = AIModel.objects.create(
            provider=self.platform_provider, model_name="gpt-4",
            display_name="GPT-4", model_type="LLM",
        )
        self.custom_model = AIModel.objects.create(
            provider=self.custom_provider, model_name="x",
            display_name="X", model_type="LLM",
        )

    def test_platform_node_source_is_platform(self):
        from apps.ai_gateway.router import annotate_chain_with_source
        chain = [(self.platform_model, self.platform_provider)]
        out = annotate_chain_with_source(chain, user=self.alice)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "platform")
        self.assertEqual(out[0]["model"], self.platform_model)

    def test_custom_node_with_credential_source_is_credential(self):
        from apps.ai_gateway.router import annotate_chain_with_source
        cred = UserProviderCredential(user=self.alice, provider=self.custom_provider)
        cred.set_key("sk-x")
        cred.save()
        chain = [(self.custom_model, self.custom_provider)]
        out = annotate_chain_with_source(chain, user=self.alice)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "credential")

    def test_custom_node_without_credential_dropped(self):
        """custom 节点 + 无凭证 → 整条节点被剔除（降级链既有跳过语义）。"""
        from apps.ai_gateway.router import annotate_chain_with_source
        chain = [
            (self.custom_model, self.custom_provider),
            (self.platform_model, self.platform_provider),
        ]
        out = annotate_chain_with_source(chain, user=self.alice)
        # custom 节点跳过；platform 节点保留
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "platform")

    def test_no_redundant_source_field_on_model(self):
        """D4：source 由 provider.visibility 推导，不在 AIModel 上加冗余字段。"""
        # 模型表无 source 字段
        self.assertFalse(hasattr(self.custom_model, "source"))
        # 注解后输出携带 source
        from apps.ai_gateway.router import annotate_chain_with_source
        out = annotate_chain_with_source(
            [(self.platform_model, self.platform_provider)], user=self.alice,
        )
        self.assertIn("source", out[0])


class ServiceFeeEmissionTests(TestCase):
    """任务 3.3：SERVICE_FEE_PER_CALL 配置 + 属主凭证路由成功调用 emit 事件。"""

    def setUp(self):
        self.alice = _mk_user("alice")
        self.platform_provider = _mk_provider(
            "P", visibility=ProviderVisibility.PLATFORM, api_key="k",
        )
        self.custom_provider = _mk_provider(
            "C", visibility=ProviderVisibility.CUSTOM, created_by=self.alice,
        )
        self.custom_cred = UserProviderCredential(
            user=self.alice, provider=self.custom_provider,
        )
        self.custom_cred.set_key("sk-x")
        self.custom_cred.save()

    @override_settings(SERVICE_FEE_PER_CALL=5)
    def test_service_fee_emitted_on_custom_credential_success(self):
        """custom 节点 + 属主凭证 + 配置 5 分 → emit amount=5 事件。"""
        from apps.ai_gateway.gateway import AIGateway
        gw = AIGateway()
        with patch.object(gw, "_record_cost", return_value=None), \
             patch("apps.ai_gateway.gateway.get_store") as mock_store:
            captured = {}

            class _Store:
                def get(self, k):
                    return None

                def set(self, k, v):
                    import json as _j
                    if "service_fee" in k:
                        captured["events"] = _j.loads(v)

            mock_store.return_value = _Store()
            gw._emit_service_fee(self.custom_provider, user=self.alice)

        events = captured.get("events", [])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "service_fee")
        self.assertEqual(events[0]["amount"], 5)
        self.assertEqual(events[0]["user_id"], self.alice.pk)
        self.assertEqual(events[0]["provider_id"], self.custom_provider.pk)
        self.assertEqual(events[0]["credential_id"], self.custom_cred.pk)

    @override_settings(SERVICE_FEE_PER_CALL=5)
    def test_platform_route_does_not_emit(self):
        """平台模型路由（visibility=platform）不 emit。"""
        from apps.ai_gateway.gateway import AIGateway
        gw = AIGateway()
        with patch("apps.ai_gateway.gateway.get_store") as mock_store:
            mock_store.return_value.get.return_value = None
            gw._emit_service_fee(self.platform_provider, user=self.alice)
            # platform 路由：根本不应触达 store.set
            mock_store.return_value.set.assert_not_called()

    @override_settings(SERVICE_FEE_PER_CALL=0)
    def test_default_zero_disables_emission(self):
        """默认 0 → 不 emit。"""
        from apps.ai_gateway.gateway import AIGateway
        gw = AIGateway()
        with patch("apps.ai_gateway.gateway.get_store") as mock_store:
            mock_store.return_value.get.return_value = None
            gw._emit_service_fee(self.custom_provider, user=self.alice)
            mock_store.return_value.set.assert_not_called()

    @override_settings(SERVICE_FEE_PER_CALL=5)
    def test_no_user_no_emission(self):
        """user=None（无属主上下文）→ 不 emit。"""
        from apps.ai_gateway.gateway import AIGateway
        gw = AIGateway()
        with patch("apps.ai_gateway.gateway.get_store") as mock_store:
            mock_store.return_value.get.return_value = None
            gw._emit_service_fee(self.custom_provider, user=None)
            mock_store.return_value.set.assert_not_called()
