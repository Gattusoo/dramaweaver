"""公开只读接口测试（public/api）。

覆盖 openspec change: backend-public-api 验收项：
匿名可读、仅 PUBLISHED、非发布详情 404、分页信封、genre/search/ordering、白名单字段。
"""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.creation.models import (
    Character, CharacterRole, Episode, EpisodeStatus, Play, PlayStatus,
)


def _published_play(**kw):
    """造已发布短剧：_skip_pipeline_dispatch 避免触发流水线派发。"""
    defaults = dict(title="剧", genre="霸总", synopsis="梗概", total_episodes=2,
                    status=PlayStatus.PUBLISHED)
    defaults.update(kw)
    play = Play(**defaults)
    play._skip_pipeline_dispatch = True
    play.save()
    return play


def _completed_episode(play, number, **kw):
    defaults = dict(play=play, episode_number=number, title=f"第{number}集",
                    status=EpisodeStatus.COMPLETED, duration_seconds=90,
                    video_url=f"/media/e{number}.mp4", poster_url=f"/media/e{number}.jpg")
    defaults.update(kw)
    ep = Episode(**defaults)
    ep._skip_pipeline_dispatch = True
    ep.save()
    return ep


class PublicApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()  # 不带任何凭据，模拟匿名访客
        self.published = _published_play(title="霸总上司", genre="霸总", synopsis="办公室恋情")
        _completed_episode(self.published, 1)
        _completed_episode(self.published, 2)
        Character.objects.create(play=self.published, name="女主",
                                 role=CharacterRole.PROTAGONIST, description="坚强")
        self.draft = _published_play(title="未发草稿", status=PlayStatus.DRAFT, genre="霸总")

    def test_anonymous_list_only_published(self):
        resp = self.client.get("/api/public/plays/")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["code"], 2000)
        self.assertEqual(body["total"], 1)  # DRAFT 被排除
        rows = body["data"]
        self.assertEqual(len(rows), 1)
        play = rows[0]
        self.assertEqual(play["id"], self.published.pk)
        # 白名单字段：不含内部字段
        self.assertEqual(set(play.keys()), {"id", "title", "genre", "synopsis",
                                            "total_episodes", "cover_url", "published_at",
                                            "play_count"})

    def test_anonymous_detail_completed_episodes_and_characters(self):
        resp = self.client.get(f"/api/public/plays/{self.published.pk}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual([e["episode_number"] for e in data["episodes"]], [1, 2])
        self.assertEqual(data["episodes"][0]["video_url"], "/media/e1.mp4")
        self.assertEqual(data["characters"][0]["role"], CharacterRole.PROTAGONIST)
        self.assertEqual(set(data["characters"][0].keys()),
                         {"id", "name", "role", "description"})

    def test_unpublished_detail_404(self):
        # 未发布 / 不存在 → 404（非 403），不泄露内容存在性
        self.assertEqual(self.client.get(f"/api/public/plays/{self.draft.pk}/").status_code, 404)
        self.assertEqual(self.client.get("/api/public/plays/99999/").status_code, 404)
        # 未发布的剧也不出现在列表
        ids = [p["id"] for p in self.client.get("/api/public/plays/").json()["data"]]
        self.assertNotIn(self.draft.pk, ids)

    def test_stats_only_published(self):
        _completed_episode(self.published, 3, duration_seconds=30)  # 累计 90+90+30=210
        resp = self.client.get("/api/public/stats/")
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(set(data.keys()), {"plays", "episodes", "total_duration_seconds", "genres"})
        self.assertEqual(data["plays"], 1)          # DRAFT 不计
        self.assertEqual(data["episodes"], 3)       # 仅已完成集
        self.assertEqual(data["total_duration_seconds"], 210)
        self.assertEqual(data["genres"], [{"genre": "霸总", "count": 1}])

    def test_genre_search_ordering(self):
        _published_play(title="穿越日记", genre="穿越", synopsis="回到过去")
        _published_play(title="霸总归来", genre="霸总", synopsis="二次霸总")
        # genre 过滤
        ids = [p["id"] for p in self.client.get("/api/public/plays/?genre=穿越").json()["data"]]
        self.assertEqual(len(ids), 1)
        # search 命中标题与梗概
        self.assertEqual(len(self.client.get("/api/public/plays/?search=霸总").json()["data"]), 2)
        self.assertEqual(len(self.client.get("/api/public/plays/?search=过去").json()["data"]), 1)
        # ordering=-play_count 确定性兜底（按 -id），返回合法分页信封
        resp = self.client.get("/api/public/plays/?ordering=-play_count").json()
        self.assertEqual(resp["code"], 2000)
        self.assertEqual(resp["total"], 3)

    def test_pagination_envelope(self):
        for i in range(25):
            _published_play(title=f"批量剧{i}")
        resp = self.client.get("/api/public/plays/?limit=10&page=2").json()
        self.assertEqual(resp["code"], 2000)
        self.assertEqual(resp["limit"], 10)
        self.assertEqual(resp["page"], 2)
        self.assertEqual(len(resp["data"]), 10)
        self.assertEqual(resp["total"], 26)  # setUp 里 1 部 + 25 部

    def test_no_auth_required_and_expires_token_ignored(self):
        # 即便带上垃圾 Authorization 头也不报 401（authentication_classes=[]）
        resp = self.client.get("/api/public/plays/", HTTP_AUTHORIZATION="Bearer garbage")
        self.assertEqual(resp.status_code, 200)
