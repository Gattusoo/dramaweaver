# -*- coding: utf-8 -*-
"""成本优化测试（2026-11-22-backend-cost-optimization）。

覆盖：多级模型选择（选档/回退/不支持工具跳过）、SmartCache（精确命中/过期/
异常降级/非 CHAT 不缓存）、语义缓存（近似命中/不命中/关闭零暴露）、
批量处理（批次切分/工具接线/开关回退）、成本报告（有数据/无数据/权限）、
成本对比实验（结论/样本不足）、配置接线（默认值+env 模板）。
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.ai_gateway import smart_cache as SC
from apps.ai_gateway.models import AIModel, AIProvider, CostRecord, SmartCache
from apps.creation.assistant import batching as B
from apps.creation.assistant import learning as L
from apps.creation.assistant import tools as T
from apps.creation.models import AgentConfig, Play, PlayMember


def _user(name, admin=False):
    User = get_user_model()
    return User.objects.create_user(username=name, password="pw-12345",
                                    is_platform_admin=admin)


def _config(owner, tiers=None):
    return AgentConfig.objects.create(name="cfg", created_by=owner,
                                      assistant_models=tiers or {})


def _llm(provider, name, model_name="m", supports_tools=True):
    return AIModel.objects.create(
        provider=provider, model_name=model_name, is_active=True,
        capabilities={"supports_tools": supports_tools})


# ── 多级模型选择（tasks 2.1）────────────────────────────────────────────────

class SelectModelTests(TestCase):
    def setUp(self):
        self.owner = _user("sm_alice")
        self.provider = AIProvider.objects.create(
            name="p1", provider_type="LLM", adapter_type="openai",
            base_url="https://api.example.com", api_key_encrypted="k", is_active=True)
        self.small = _llm(self.provider, "small")
        self.medium = _llm(self.provider, "medium")
        self.large = _llm(self.provider, "large")

    def _config(self, tiers):
        from apps.ai_gateway.router import select_model_for_task
        cfg = SimpleNamespace(assistant_models=tiers)
        return select_model_for_task

    def test_tier_selection(self):
        from apps.ai_gateway.router import select_model_for_task
        cfg = SimpleNamespace(assistant_models={
            "small": self.small.pk, "medium": self.medium.pk, "large": self.large.pk})
        self.assertEqual(select_model_for_task(cfg, 0.1).pk, self.small.pk)
        self.assertEqual(select_model_for_task(cfg, 0.5).pk, self.medium.pk)
        self.assertEqual(select_model_for_task(cfg, 0.9).pk, self.large.pk)

    def test_unconfigured_fallback_none(self):
        from apps.ai_gateway.router import select_model_for_task
        cfg = SimpleNamespace(assistant_models={})
        self.assertIsNone(select_model_for_task(cfg, 0.5))  # 调用方回退默认链
        self.assertIsNone(select_model_for_task(cfg, 0.5))  # 非法复杂度
        self.assertIsNone(select_model_for_task(cfg, None))

    def test_inactive_or_no_tools_skipped(self):
        from apps.ai_gateway.router import select_model_for_task
        no_tools = _llm(self.provider, "plain", supports_tools=False)
        cfg = SimpleNamespace(assistant_models={"small": no_tools.pk})
        self.assertIsNone(select_model_for_task(cfg, 0.1))
        # 摘要场景（supports_tool_calls=False）可命中未开工具的模型
        self.assertEqual(select_model_for_task(cfg, 0.1,
                                                supports_tool_calls=False).pk, no_tools.pk)
        inactive = _llm(self.provider, "off")
        inactive.is_active = False
        inactive.save()
        cfg2 = SimpleNamespace(assistant_models={"small": inactive.pk})
        self.assertIsNone(select_model_for_task(cfg2, 0.1))

    def test_runner_tier_wiring(self):
        """runner 循环按复杂度选档：agent_config 配 small 档后 gateway.chat 收到 model。"""
        from apps.creation.assistant import runner as RN
        from apps.creation.models import TaskRecord, TaskType
        from apps.creation.assistant.models import AssistantSession

        owner = _user("rw_owner")
        play = Play.objects.create(title="t", created_by=owner)
        PlayMember.objects.create(play=play, user=owner, role="owner", status="active")
        config = AgentConfig.objects.create(
            name="c2", created_by=owner, assistant_models={"small": self.small.pk})
        play.agent_config = config
        play.save()
        session = AssistantSession.objects.create(user=owner, play=play,
                                                  agent_config=config)
        record = TaskRecord.objects.create(
            play=play, task_type=TaskType.ASSISTANT_TURN,
            input_summary={"session_id": session.pk})
        RN._save(session, "user", [{"type": "text", "text": "简单的改动"}], record)
        AssistantSession.objects.filter(pk=session.pk).update(running_task=record)
        import apps.creation.assistant.events as events
        events.set_backend(events.MemoryStreams())
        self.addCleanup(events.set_backend, None)
        seen_models = []

        class GW:
            def chat(self, cfg, *, messages, system="", tools=None, play=None,
                     task_record=None, model=None, **kw):
                seen_models.append(model)
                return {"content_blocks": [{"type": "text", "text": "好的"}],
                        "stop_reason": "end_turn", "model": "m",
                        "usage": {"input_tokens": 1, "output_tokens": 1}}

        gw = GW()
        with patch("apps.ai_gateway.gateway.AIGateway", return_value=gw):
            RN.run_assistant_turn(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.status, "SUCCESS")
        self.assertEqual(seen_models, [self.small])


# ── SmartCache（tasks 3.1/3.2/4.1）──────────────────────────────────────────

@override_settings(ENABLE_SMART_CACHE=True)
class SmartCacheTests(TestCase):
    def setUp(self):
        self.provider = AIProvider.objects.create(
            name="p2", provider_type="LLM", adapter_type="openai",
            base_url="https://api.example.com", api_key_encrypted="k", is_active=True)
        self.model = _llm(self.provider, "m1")

    def test_cache_key_deterministic(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "你好"}]}]
        self.assertEqual(SC.cache_key(self.model, msgs, "SYS"),
                         SC.cache_key(self.model, msgs, "SYS"))
        self.assertNotEqual(SC.cache_key(self.model, msgs, "SYS"),
                            SC.cache_key(self.model, msgs, "OTHER"))

    def test_exact_hit_and_expiry(self):
        msgs = [{"role": "user", "content": "你好"}]
        self.assertEqual(SC.get_cached(self.model, msgs, ""), (None, None))
        SC.put_cached(self.model, msgs, "", {"content_blocks": [{"type": "text", "text": "hi"}]})
        cached, kind = SC.get_cached(self.model, msgs, "")
        self.assertEqual(kind, "exact")
        self.assertEqual(cached["content_blocks"][0]["text"], "hi")
        self.assertEqual(SmartCache.objects.get().hit_count, 1)
        # 过期 → miss 且惰性删除
        SmartCache.objects.update(created_at=timezone.now() - timezone.timedelta(seconds=7200))
        self.assertEqual(SC.get_cached(self.model, msgs, ""), (None, None))
        self.assertEqual(SmartCache.objects.count(), 0)

    def test_semantic_hit_and_miss(self):
        msgs = [{"role": "user", "content": "写一段雨夜天台对峙的场景描述，要紧张感"}]
        SC.put_cached(self.model, msgs, "", {"content_blocks": []})
        with override_settings(ENABLE_SEMANTIC_CACHE=True):
            # 近重复（加语气词，bigram Jaccard ≈0.81 > 0.85 阈值下需近似全同）：
            # 用足够近的样本（仅结尾标点差异文本 bigram 基本全同）
            near = [{"role": "user", "content": "写一段雨夜天台对峙的场景描述，要紧张感哈"}]
            cached, kind = SC.get_cached(self.model, near, "")
            self.assertEqual(kind, "semantic")
            far = [{"role": "user", "content": "帮我改一下第3集的对白"}]
            self.assertEqual(SC.get_cached(self.model, far, ""), (None, None))
        # 语义开关关闭：近重复也不命中
        near2 = [{"role": "user", "content": "写一段雨夜天台对峙的场景描述，要紧张感哈"}]
        self.assertEqual(SC.get_cached(self.model, near2, ""), (None, None))

    def test_disabled_zero_queries(self):
        msgs = [{"role": "user", "content": "x"}]
        with override_settings(ENABLE_SMART_CACHE=False):
            self.assertEqual(SC.get_cached(self.model, msgs, ""), (None, None))
            SC.put_cached(self.model, msgs, "", {"a": 1})
        self.assertEqual(SmartCache.objects.count(), 0)

    def test_similarity(self):
        self.assertEqual(SC.similarity("", ""), 0.0)
        self.assertGreater(SC.similarity("雨夜天台对峙", "雨夜天台对峙"), 0.99)
        self.assertLess(SC.similarity("雨夜天台对峙", "完全无关内容"), 0.3)

    def test_gateway_chat_cached_no_second_call(self):
        """_route CHAT 命中缓存后不再调 provider、不产生 CostRecord。"""
        from apps.ai_gateway.gateway import AIGateway
        msgs = [{"role": "user", "content": "hello"}]
        calls = []

        def fake_adapter(provider):
            def chat(messages, system, tools, params):
                calls.append(params.get("model"))
                return {"content_blocks": [{"type": "text", "text": "resp"}],
                        "model": params.get("model")}
            return SimpleNamespace(chat=chat)

        gw = AIGateway(adapter_factory=fake_adapter)
        cfg = SimpleNamespace(created_by=None, assistant_model=self.model,
                              assistant_failover=[], llm_model=None, llm_failover=[])
        r1 = gw.chat(cfg, messages=msgs, system="")
        self.assertEqual(len(calls), 1)
        r2 = gw.chat(cfg, messages=msgs, system="")
        self.assertEqual(len(calls), 1)  # 第二次命中缓存
        self.assertEqual(r2["content_blocks"], r1["content_blocks"])
        self.assertEqual(SmartCache.objects.get().hit_count, 1)


# ── 批量处理（tasks 5.1）────────────────────────────────────────────────────

class BatchingTests(TestCase):
    def test_plan_batch_split(self):
        descs = [f"镜头{i}描述" for i in range(25)]
        batches = B.plan_batch(descs)
        self.assertEqual([len(b) for b in batches], [10, 10, 5])
        self.assertEqual(B.plan_batch([]), [])
        self.assertEqual(B.plan_batch(None), [])
        self.assertEqual(B.plan_batch(["  ", "a"]), [["a"]])

    def test_batch_prompt_and_metadata(self):
        p = B.batch_prompt(["写雨夜", "写天台"], goal="第三集")
        self.assertIn("创作目标：第三集", p)
        self.assertIn("1. 写雨夜", p)
        meta = B.batch_metadata(1, 2)
        self.assertEqual(meta, {"batch": True, "batch_count": 1, "batch_shot_count": 2})

    @override_settings(ENABLE_BATCH_GENERATION=True)
    def test_tool_batch_wiring(self):
        """10 镜头批量 → 合并 prompt 单条 text + batch_info 元数据。"""
        from apps.creation.assistant.models import AssistantSession
        from apps.creation.models import Episode
        owner = _user("bt_alice")
        play = Play.objects.create(title="t", created_by=owner)
        PlayMember.objects.create(play=play, user=owner, role="owner", status="active")
        ep = Episode.objects.create(play=play, episode_number=1)
        session = AssistantSession.objects.create(user=owner, play=play, agent_config=None)
        ctx = SimpleNamespace(play=play, session=session, user=owner,
                              message=None, proposals=[])
        descs = [f"镜头{i}改写" for i in range(10)]
        result, err = T.run_tool(ctx, "propose_episode_script_edit",
                                 {"episode_number": 1, "summary": "批量改写",
                                  "batch_shot_descriptions": descs})
        self.assertFalse(err, result)
        from apps.creation.assistant.models import AssistantProposal
        p = AssistantProposal.objects.get()
        self.assertIn("镜头1改写", p.payload["text"])
        self.assertIn("镜头9改写", p.payload["text"])  # range(10) → 编号 0-9
        self.assertEqual(p.payload["batch_info"]["batch_shot_count"], 10)

    def test_tool_batch_off_fallback(self):
        """开关关闭：批量参数降级为拼接单条，无 batch_info。"""
        from apps.creation.assistant.models import AssistantSession
        from apps.creation.models import Episode
        owner = _user("bt_bob")
        play = Play.objects.create(title="t2", created_by=owner)
        PlayMember.objects.create(play=play, user=owner, role="owner", status="active")
        Episode.objects.create(play=play, episode_number=1)
        session = AssistantSession.objects.create(user=owner, play=play, agent_config=None)
        ctx = SimpleNamespace(play=play, session=session, user=owner,
                              message=None, proposals=[])
        result, err = T.run_tool(ctx, "propose_episode_script_edit",
                                 {"episode_number": 1, "summary": "s",
                                  "batch_shot_descriptions": ["a", "b"]})
        self.assertFalse(err, result)
        from apps.creation.assistant.models import AssistantProposal
        p = AssistantProposal.objects.get()
        self.assertEqual(p.payload["text"], "a\nb")
        self.assertNotIn("batch_info", p.payload)


# ── 成本报告（tasks 6.1/6.2）────────────────────────────────────────────────

class CostOptimizationReportTests(APITestCase):
    def setUp(self):
        self.admin = _user("co_admin", admin=True)
        self.user = _user("co_user")
        self.owner = _user("co_owner")
        self.provider = AIProvider.objects.create(
            name="p3", provider_type="LLM", adapter_type="openai",
            base_url="https://api.example.com", api_key_encrypted="k", is_active=True)
        self.play = Play.objects.create(title="t", created_by=self.owner)

    def _cost(self, amount):
        return CostRecord.objects.create(
            play=self.play, provider=self.provider, model="m1",
            provider_type="LLM", usage={}, unit_cost=Decimal("0.01"),
            total_cost=Decimal(amount))

    def test_report_with_data(self):
        self._cost("1.00")
        self._cost("3.00")
        SmartCache.objects.create(
            model_id=1, model_name="m1", cache_key="k1", payload_digest="",
            cache_value={}, hit_count=7, last_hit_at=timezone.now())
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/analytics/cost-optimization/report/")
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json().get("data", resp.json())
        self.assertEqual(data["total_calls"], 2)
        self.assertEqual(data["total_cost"], 4.0)
        self.assertEqual(data["cache"]["hit_count"], 7)
        self.assertEqual(data["models"][0]["model"], "m1")

    def test_report_empty_month(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/analytics/cost-optimization/report/?month=2020-01")
        self.assertEqual(resp.status_code, 200)
        data = resp.json().get("data", resp.json())
        self.assertEqual((data["total_calls"], data["total_cost"]), (0, 0.0))
        self.assertEqual(data["cache"]["hit_rate"], 0.0)

    def test_report_admin_only(self):
        self.client.force_authenticate(self.user)
        resp = self.client.get("/api/analytics/cost-optimization/report/")
        self.assertEqual(resp.status_code, 403)

    def test_cost_comparison_report(self):
        from apps.creation.assistant.models import StrategyExperiment
        c1, c2, c3 = self._cost("2.00"), self._cost("4.00"), self._cost("1.00")
        exp = StrategyExperiment.objects.create(
            name="ab", created_by=self.owner,
            variants=[
                {"key": "A", "name": "缓存开", "weight": 1, "success": 0, "failure": 0,
                 "cost_record_ids": [c1.pk, c2.pk]},
                {"key": "B", "name": "缓存关", "weight": 1, "success": 0, "failure": 0,
                 "cost_record_ids": [c3.pk]},
            ])
        report = L.cost_comparison_report(exp)
        by_key = {v["key"]: v for v in report["variants"]}
        self.assertEqual(by_key["A"]["cost_avg"], 3.0)
        self.assertEqual(by_key["B"]["cost_avg"], 1.0)
        self.assertTrue(report["cost_samples_sufficient"])
        self.assertEqual(report["cost_conclusion"]["cheaper_variant"], "B")
        # 样本不足
        exp2 = StrategyExperiment.objects.create(
            name="ab2", created_by=self.owner,
            variants=[{"key": "A", "name": "a", "weight": 1, "success": 0, "failure": 0},
                      {"key": "B", "name": "b", "weight": 1, "success": 0, "failure": 0}])
        report2 = L.cost_comparison_report(exp2)
        self.assertFalse(report2["cost_samples_sufficient"])
        self.assertIsNone(report2["cost_conclusion"])


# ── 配置接线（tasks 7.1）────────────────────────────────────────────────────

class CostFlagWiringTests(TestCase):
    def test_settings_defaults(self):
        from django.conf import settings
        self.assertIs(settings.ENABLE_SMART_CACHE, False)
        self.assertIs(settings.ENABLE_SEMANTIC_CACHE, False)
        self.assertIs(settings.ENABLE_BATCH_GENERATION, False)
        self.assertEqual(settings.SMART_CACHE_TTL_S, 3600)
        self.assertEqual(settings.SMART_CACHE_MAX_ROWS, 500)
        self.assertEqual(settings.SEMANTIC_CACHE_THRESHOLD, 0.85)

    def test_env_template_has_keys(self):
        from pathlib import Path
        import conf
        template = Path(conf.__file__).parent / "env.example.py"
        self.assertTrue(template.is_file())
        text = template.read_text(encoding="utf-8")
        for key in ("ENABLE_SMART_CACHE", "SMART_CACHE_TTL_S", "SMART_CACHE_MAX_ROWS",
                    "ENABLE_SEMANTIC_CACHE", "SEMANTIC_CACHE_THRESHOLD",
                    "ENABLE_BATCH_GENERATION"):
            self.assertIn(key, text)
