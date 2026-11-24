"""统一响应格式测试（platform/api-conventions）。

断言渲染后的响应体（response.json()）符合 {code, msg, data} 结构，
覆盖：统一响应结构、分页、业务错误码、服务端异常不外泄、文件下载不包装。
"""
import json

from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.system.models import CustomUser
from apps.creation.models import AgentConfig, MemberRole, MemberStatus, Play, PlayMember


def _make(username="admin", **kw):
    return CustomUser.objects.create_user(username=username, password="Passw0rd!123", **kw)


def _own(play, user):
    """给 Play 建 owner 成员行（新语义：业务权限只看成员表）。"""
    return PlayMember.objects.create(
        play=play, user=user, role=MemberRole.OWNER, status=MemberStatus.ACTIVE
    )


def _play(title, user):
    play = Play.objects.create(title=title, created_by=user)
    _own(play, user)
    return play


class ResponseFormatTests(APITestCase):
    def setUp(self):
        self.user = _make()
        self.client.force_authenticate(self.user)

    def test_single_object_wrapped(self):
        """GET 单对象 -> {code:2000, msg, data:{id,...}}。"""
        play = _play("t", self.user)
        resp = self.client.get(f"/api/plays/{play.pk}/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["code"], 2000)
        self.assertEqual(body["msg"], "ok")
        self.assertEqual(body["data"]["id"], play.pk)

    def test_create_object_wrapped(self):
        """POST 创建 -> 201, code 2000。"""
        resp = self.client.post("/api/plays/", {"title": "new play"}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["code"], 2000)

    def test_pagination_structure(self):
        """分页 -> {code, msg, page, limit, total, data}。"""
        for i in range(25):
            _play(f"p{i}", self.user)
        resp = self.client.get("/api/plays/?page=2&limit=10")
        body = resp.json()
        self.assertEqual(body["code"], 2000)
        self.assertEqual(body["page"], 2)
        self.assertEqual(body["limit"], 10)
        self.assertEqual(body["total"], 25)
        self.assertEqual(len(body["data"]), 10)

    def test_page_out_of_range_returns_empty(self):
        for i in range(5):
            _play(f"p{i}", self.user)
        resp = self.client.get("/api/plays/?page=99&limit=10")
        body = resp.json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body["total"], 5)
        self.assertEqual(body["data"], [])

    def test_limit_clamped_to_max(self):
        for i in range(3):
            _play(f"p{i}", self.user)
        resp = self.client.get("/api/plays/?limit=1000")
        self.assertEqual(resp.json()["limit"], 200)

    def test_field_validation_error(self):
        """字段校验失败 -> 400, code 4000, data.errors.title。"""
        resp = self.client.post("/api/plays/", {}, format="json")
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["code"], 4000)
        self.assertIn("title", body["data"]["errors"])

    def test_unauthenticated_error_code(self):
        """未认证 -> 401, code 4010。"""
        self.client.force_authenticate(None)
        resp = self.client.get("/api/plays/")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], 4010)

    def test_forbidden_error_code(self):
        """非成员访问他人短剧 -> 404（列表过滤）/ 403（对象级写）。"""
        other = _make(username="other")
        play = _play("他人的剧", other)
        self.client.force_authenticate(self.user)
        resp = self.client.patch(f"/api/plays/{play.pk}/", {"title": "改"}, format="json")
        self.assertEqual(resp.status_code, 404)  # 不在可访问集合内
        # 对象级 403：成员但权限不足（viewer 改内容）
        viewer = _make(username="viewer")
        PlayMember.objects.create(play=play, user=viewer, role=MemberRole.VIEWER,
                                  status=MemberStatus.ACTIVE)
        self.client.force_authenticate(viewer)
        resp = self.client.patch(f"/api/plays/{play.pk}/", {"title": "改"}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], 4030)

    def test_error_extra_fields_passed_through(self):
        """错误体附加业务字段透传到信封顶层（提案 status / 记忆 content+revision 等）。"""
        from apps.core.utils.renderers import CustomJSONRenderer
        from rest_framework.test import APIRequestFactory
        from rest_framework.response import Response as DRFResponse

        renderer = CustomJSONRenderer()
        factory = APIRequestFactory()

        def _render(payload, status_code):
            request = factory.get("/")
            response = DRFResponse(payload, status=status_code)
            rendered = renderer.render(payload, "application/json",
                                       {"response": response, "request": request})
            return json.loads(rendered.decode())

        # detail + 附加字段：status 透传
        body = _render({"detail": "提案已过期", "status": "STALE"}, 409)
        self.assertEqual(body, {"code": 4090, "msg": "提案已过期", "data": None, "status": "STALE"})

        # detail + 记忆冲突（content/revision 附加字段）透传
        body = _render({"detail": "记忆已被修改", "content": "最新", "revision": "r2"}, 409)
        self.assertEqual(body["content"], "最新")
        self.assertEqual(body["revision"], "r2")

        # 冲突键（code/msg/data）被丢弃，信封优先
        body = _render({"detail": "会话费用已达上限", "code": "budget_exceeded"}, 409)
        self.assertEqual(body["code"], 4090)
        self.assertNotIn("budget_exceeded", json.dumps(body))

        # 仅 detail：输出与既有行为完全一致（无附加键混入）
        body = _render({"detail": "上一轮对话尚未结束"}, 409)
        self.assertEqual(body, {"code": 4090, "msg": "上一轮对话尚未结束", "data": None})

    def test_subtitle_export_not_wrapped(self):
        """字幕导出返回原始 SRT 文本，不被 JSON 包装。"""
        play = _play("t", self.user)
        from apps.creation.models import Episode
        from apps.distribution.models import SubtitleTrack, SubtitleSourceType, SubtitleStatus
        ep = Episode.objects.create(play=play, episode_number=1)
        track = SubtitleTrack.objects.create(
            episode=ep, language="zh", source_type=SubtitleSourceType.MANUAL,
            status=SubtitleStatus.READY, subtitle_data=[{"start": 0, "end": 1, "text": "hi"}],
        )
        resp = self.client.get(f"/api/subtitle-tracks/{track.pk}/export/srt/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/x-subrip", resp["Content-Type"])
        # 不是 JSON 包装：响应体不以 { 开头
        content = resp.content.decode("utf-8").strip()
        self.assertFalse(content.startswith("{"))
        self.assertIn("hi", content)
