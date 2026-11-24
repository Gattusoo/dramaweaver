"""创作模型端点 + Play.gen_params（openspec change: 2026-11-12-backend-play-gen-params）。"""
from rest_framework.test import APITestCase

from apps.ai_gateway.models import AIModel, AIProvider
from apps.creation.models import MemberRole, MemberStatus, Play, PlayMember
from apps.core.system.models import CustomUser

GEN = {
    "create_types": ["文生短剧", "图生短剧"],
    "styles": ["电影感", "写实"],
    "aspects": ["16:9", "9:16"],
    "durations": ["1-2 分钟"],
    "count": 4,
}

URL = "/api/ai-models/creation/"


def _user(name, **kw):
    return CustomUser.objects.create_user(username=name, password="Passw0rd!123", **kw)


def _own(play, user):
    return PlayMember.objects.create(
        play=play, user=user, role=MemberRole.OWNER, status=MemberStatus.ACTIVE
    )


class CreationModelsFixtureMixin:
    def setUp(self):
        self.admin = _user("cmadm", is_platform_admin=True)
        self.producer = _user("cmprd")
        self.provider = AIProvider.objects.create(
            name="Seedream", provider_type="IMAGE", adapter_type="seedream",
            base_url="https://api.example.com/v1", api_key_encrypted="k1",
        )
        self.image_model = AIModel.objects.create(
            provider=self.provider, model_name="seedream-4", display_name="Seedream 4",
            model_type="IMAGE", capabilities={"gen_params": GEN},
            cost_config={"unit": 0.2, "currency": "CNY"},
        )
        self.video_model = AIModel.objects.create(
            provider=self.provider, model_name="kling-v1", display_name="Kling v1",
            model_type="VIDEO", capabilities={"gen_params": {"create_types": ["文生短剧"], "count": 2}},
        )
        # 无 gen_params / 空 gen_params / 停用：都不该出现在创作模型端点
        self.llm_model = AIModel.objects.create(
            provider=self.provider, model_name="deepseek-v3", display_name="DeepSeek V3",
            model_type="LLM", capabilities={"supports_tools": True},
        )
        self.empty_caps_model = AIModel.objects.create(
            provider=self.provider, model_name="empty-caps", display_name="Empty Caps",
            model_type="IMAGE", capabilities={},
        )
        self.empty_gen_model = AIModel.objects.create(
            provider=self.provider, model_name="empty-gen", display_name="Empty Gen",
            model_type="IMAGE", capabilities={"gen_params": {}},
        )
        self.inactive_model = AIModel.objects.create(
            provider=self.provider, model_name="offline", display_name="Offline",
            model_type="IMAGE", capabilities={"gen_params": {"create_types": ["文生短剧"]}},
            is_active=False,
        )


class CreationModelsEndpointTests(CreationModelsFixtureMixin, APITestCase):
    """任务 2.1–2.4：创作者可读端点 + 轻量序列化 + 过滤。"""

    def test_producer_reads_only_gen_models_without_cost_config(self):
        self.client.force_authenticate(self.producer)
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["total"], 2)
        results = resp.data["results"]
        self.assertEqual({m["id"] for m in results}, {self.image_model.pk, self.video_model.pk})
        item = next(m for m in results if m["id"] == self.image_model.pk)
        self.assertEqual(item["capabilities"]["gen_params"], GEN)
        # 仅 id/display_name/model_type/capabilities：无 cost_config / model_name / provider
        self.assertEqual(set(item.keys()), {"id", "display_name", "model_type", "capabilities"})

    def test_unauthenticated_401(self):
        self.assertEqual(self.client.get(URL).status_code, 401)

    def test_pagination_shape(self):
        self.client.force_authenticate(self.producer)
        resp = self.client.get(URL, {"page": 1, "limit": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["page"], 1)
        self.assertEqual(resp.data["limit"], 1)
        self.assertEqual(resp.data["total"], 2)
        self.assertEqual(len(resp.data["results"]), 1)

    def test_admin_endpoint_unchanged(self):
        self.client.force_authenticate(self.producer)
        self.assertEqual(self.client.get("/api/ai-models/").status_code, 403)
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/ai-models/")
        self.assertEqual(resp.status_code, 200)
        item = next(m for m in resp.data["results"] if m["id"] == self.image_model.pk)
        self.assertEqual(item["cost_config"]["unit"], 0.2)


class PlayGenParamsTests(APITestCase):
    """Play.gen_params：创建/更新可写、读取返回、缺省空 dict。"""

    def setUp(self):
        self.producer = _user("pgprd")
        self.client.force_authenticate(self.producer)

    def test_create_with_gen_params_roundtrip(self):
        payload = {
            "title": "隐婚霸总", "genre": "霸总", "total_episodes": 10,
            "gen_params": {
                "create_type": "文生短剧", "style": "电影感",
                "aspect_ratio": "9:16", "duration": "1-2 分钟", "count": 2,
            },
        }
        resp = self.client.post("/api/plays/", payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["gen_params"], payload["gen_params"])
        detail = self.client.get(f"/api/plays/{resp.data['id']}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["gen_params"], payload["gen_params"])

    def test_create_without_gen_params_defaults_to_empty_dict(self):
        resp = self.client.post(
            "/api/plays/", {"title": "无参数剧", "genre": "都市"}, format="json"
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["gen_params"], {})
        self.assertEqual(Play.objects.get(pk=resp.data["id"]).gen_params, {})

    def test_update_gen_params(self):
        play = Play.objects.create(title="剧", created_by=self.producer)
        _own(play, self.producer)
        resp = self.client.patch(
            f"/api/plays/{play.pk}/", {"gen_params": {"style": "写实"}}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        play.refresh_from_db()
        self.assertEqual(play.gen_params, {"style": "写实"})
