"""评估框架测试（Phase 1.7）：5个指标 + 聚合看板 + 注入防御。"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.creation.assistant import eval_hooks, evaluation
from apps.creation.assistant.evaluation import (
    aggregate_metrics,
    evaluate_injection_defense,
    record_cost,
    record_latency,
    record_task_completion,
    record_tool_success,
    record_user_satisfaction,
)
from apps.creation.assistant.models import (
    AssistantMessage,
    AssistantProposal,
    AssistantSession,
    EvaluationMetric,
    MessageRole,
    MetricName,
    ProposalKind,
    ProposalStatus,
)
from apps.creation.models import (
    AgentConfig, MemberRole, MemberStatus, Play, PlayMember, PlayStatus,
    TaskRecord, TaskStatus, TaskType,
)
from apps.core.system.models import CustomUser


def _mk(user=None, uname="ev"):
    user = user or CustomUser.objects.create_user(username=uname, password="t")
    play = Play.objects.create(title="t", status=PlayStatus.DRAFT, created_by=user)
    PlayMember.objects.create(play=play, user=user, role=MemberRole.OWNER,
                              status=MemberStatus.ACTIVE)
    config = AgentConfig.objects.create(name="c", created_by=user)
    session = AssistantSession.objects.create(play=play, user=user, agent_config=config)
    return user, play, session


def _record(session, outcome="done", cost=0.01, latency_s=2.0):
    now = timezone.now()
    r = TaskRecord.objects.create(
        play=session.play, task_type=TaskType.ASSISTANT_TURN,
        status=TaskStatus.SUCCESS, cost=Decimal(str(cost)),
        started_at=now - timedelta(seconds=latency_s), completed_at=now,
        input_summary={"session_id": session.pk},
        output_summary={"outcome": outcome, "session_id": session.pk})
    return r


class MetricRecordTests(TestCase):
    def setUp(self):
        self.user, self.play, self.session = _mk()

    @override_settings(ENABLE_EVALUATION=True)
    def test_task_completion_success(self):
        record = _record(self.session, outcome="done")
        out = record_task_completion({"session": self.session, "record": record})
        self.assertTrue(out["success"])
        m = EvaluationMetric.objects.get(session=self.session, metric_name=MetricName.TASK_COMPLETION)
        self.assertEqual(m.metric_value, 1.0)
        self.assertEqual(m.turn_id, record.pk)

    @override_settings(ENABLE_EVALUATION=True)
    def test_task_completion_failure(self):
        record = _record(self.session, outcome="error")
        out = record_task_completion({"session": self.session, "record": record})
        self.assertFalse(out["success"])

    @override_settings(ENABLE_EVALUATION=True)
    def test_cost(self):
        record = _record(self.session, cost=0.034)
        record_cost({"session": self.session, "record": record})
        m = EvaluationMetric.objects.get(session=self.session, metric_name=MetricName.COST)
        self.assertAlmostEqual(m.metric_value, 0.034, places=3)

    @override_settings(ENABLE_EVALUATION=True)
    def test_latency(self):
        record = _record(self.session, latency_s=5.5)
        record_latency({"session": self.session, "record": record})
        m = EvaluationMetric.objects.get(session=self.session, metric_name=MetricName.LATENCY)
        self.assertAlmostEqual(m.metric_value, 5.5, places=1)

    @override_settings(ENABLE_EVALUATION=True)
    def test_latency_missing_timestamps(self):
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.ASSISTANT_TURN, status=TaskStatus.PENDING,
            input_summary={"session_id": self.session.pk})
        self.assertIsNone(record_latency({"session": self.session, "record": record}))

    @override_settings(ENABLE_EVALUATION=True)
    def test_tool_success_rate(self):
        record = _record(self.session)
        AssistantMessage.objects.create(
            session=self.session, seq=1, role=MessageRole.TOOL, task_record=record,
            content=[{"type": "tool_result", "tool_use_id": "a", "content": "ok"},
                     {"type": "tool_result", "tool_use_id": "b", "content": "bad", "is_error": True},
                     {"type": "tool_result", "tool_use_id": "c", "content": "ok"}])
        out = record_tool_success({"session": self.session, "record": record})
        self.assertAlmostEqual(out["rate"], 2 / 3, places=2)

    @override_settings(ENABLE_EVALUATION=True)
    def test_tool_success_no_tools(self):
        record = _record(self.session)
        self.assertIsNone(record_tool_success({"session": self.session, "record": record}))

    @override_settings(ENABLE_EVALUATION=True)
    def test_user_satisfaction(self):
        for st in (ProposalStatus.APPLIED, ProposalStatus.APPLIED, ProposalStatus.DISCARDED):
            AssistantProposal.objects.create(
                session=self.session, play=self.play, kind=ProposalKind.SHOT, status=st)
        record = _record(self.session)
        out = record_user_satisfaction({"session": self.session, "record": record})
        self.assertAlmostEqual(out["rate"], 2 / 3, places=2)

    @override_settings(ENABLE_EVALUATION=True)
    def test_user_satisfaction_no_decided(self):
        AssistantProposal.objects.create(
            session=self.session, play=self.play, kind=ProposalKind.SHOT, status=ProposalStatus.PENDING)
        record = _record(self.session)
        self.assertIsNone(record_user_satisfaction({"session": self.session, "record": record}))

    @override_settings(ENABLE_EVALUATION=False)
    def test_flag_off_writes_nothing(self):
        record = _record(self.session)
        record_task_completion({"session": self.session, "record": record})
        record_cost({"session": self.session, "record": record})
        self.assertEqual(EvaluationMetric.objects.count(), 0)


class RegisterTests(TestCase):
    def test_register_defaults_idempotent(self):
        evaluation._registered = False
        evaluation.register_defaults()
        n1 = len(eval_hooks.registered(eval_hooks.TURN_END))
        evaluation.register_defaults()
        n2 = len(eval_hooks.registered(eval_hooks.TURN_END))
        self.assertEqual(n1, n2)
        names = {n for n, _ in eval_hooks.registered(eval_hooks.TURN_END)}
        self.assertIn("record_task_completion", names)
        self.assertIn("record_user_satisfaction", names)


class AggregateTests(TestCase):
    def setUp(self):
        self.user, self.play, self.session = _mk(uname="agg")

    def _mk_metric(self, name, value, days_ago=0):
        m = EvaluationMetric.objects.create(
            session=self.session, turn_id=1, metric_name=name, metric_value=value)
        if days_ago:
            EvaluationMetric.objects.filter(pk=m.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago))

    def test_aggregate_7d(self):
        self._mk_metric(MetricName.TASK_COMPLETION, 1.0)
        self._mk_metric(MetricName.TASK_COMPLETION, 0.0)
        self._mk_metric(MetricName.COST, 0.02)
        self._mk_metric(MetricName.COST, 0.04, days_ago=10)  # 超窗排除
        data = aggregate_metrics(self.play.pk, days=7)
        self.assertIn("task_completion", data["metrics"])
        self.assertEqual(data["metrics"]["task_completion"]["count"], 2)
        self.assertAlmostEqual(data["metrics"]["task_completion"]["avg"], 0.5)
        self.assertEqual(data["metrics"]["cost"]["count"], 1)  # 10天前的不计

    def test_aggregate_play_filter(self):
        other_play = Play.objects.create(title="o", status=PlayStatus.DRAFT, created_by=self.user)
        s2 = AssistantSession.objects.create(play=other_play, user=self.user,
                                             agent_config=self.session.agent_config)
        EvaluationMetric.objects.create(session=s2, turn_id=1,
                                        metric_name=MetricName.COST, metric_value=9.9)
        self._mk_metric(MetricName.COST, 0.02)
        data = aggregate_metrics(self.play.pk, days=7)
        self.assertAlmostEqual(data["metrics"]["cost"]["avg"], 0.02, places=3)

    def test_aggregate_all_plays(self):
        self._mk_metric(MetricName.COST, 0.02)
        data = aggregate_metrics(None, days=7)
        self.assertIn("cost", data["metrics"])


class EvalApiTests(TestCase):
    def setUp(self):
        self.user, self.play, self.session = _mk(uname="evapi")

    def test_evaluation_endpoint(self):
        EvaluationMetric.objects.create(
            session=self.session, turn_id=1,
            metric_name=MetricName.TASK_COMPLETION, metric_value=1.0)
        from rest_framework.test import APIClient
        c = APIClient(); c.force_authenticate(self.user)
        resp = c.get(f"/api/assistant/evaluation/?play={self.play.pk}&period=7d")
        self.assertEqual(resp.status_code, 200)
        data = resp.json().get("data", resp.json())
        self.assertIn("task_completion", data["metrics"])
        self.assertIn("trend", data)

    def test_evaluation_endpoint_default_period(self):
        from rest_framework.test import APIClient
        c = APIClient(); c.force_authenticate(self.user)
        resp = c.get("/api/assistant/evaluation/")
        self.assertEqual(resp.status_code, 200)

    def test_evaluation_scoped_to_accessible_plays(self):
        """未指定 play 时只聚合当前用户可访问短剧（防跨租户泄漏）。"""
        from rest_framework.test import APIClient
        # 用户 A（self.user）已有 cost 指标
        EvaluationMetric.objects.create(
            session=self.session, turn_id=1,
            metric_name=MetricName.COST, metric_value=42.5)
        # 无关用户 B 的短剧 + 指标
        other = CustomUser.objects.create_user(username="other-b", password="t")
        oplay = Play.objects.create(title="o", status=PlayStatus.DRAFT, created_by=other)
        oconfig = AgentConfig.objects.create(name="oc", created_by=other)
        osession = AssistantSession.objects.create(play=oplay, user=other, agent_config=oconfig)
        EvaluationMetric.objects.create(
            session=osession, turn_id=1, metric_name=MetricName.COST, metric_value=9.9)

        c = APIClient(); c.force_authenticate(self.user)
        resp = c.get("/api/assistant/evaluation/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json().get("data", resp.json())
        cost = data["metrics"].get("cost", {})
        self.assertEqual(cost.get("count"), 1, "只应看到自己的 1 条指标")
        self.assertAlmostEqual(cost.get("avg", 0), 42.5, places=3)

    def test_evaluation_other_users_play_404(self):
        """指定无权访问的 play 按不存在处理（404）。"""
        from rest_framework.test import APIClient
        other = CustomUser.objects.create_user(username="other-c", password="t")
        oplay = Play.objects.create(title="o2", status=PlayStatus.DRAFT, created_by=other)
        oconfig = AgentConfig.objects.create(name="oc2", created_by=other)
        AssistantSession.objects.create(play=oplay, user=other, agent_config=oconfig)

        c = APIClient(); c.force_authenticate(self.user)
        resp = c.get(f"/api/assistant/evaluation/?play={oplay.pk}")
        self.assertEqual(resp.status_code, 404)

    def test_evaluation_aggregates_all_accessible_plays(self):
        """未指定 play 时聚合该用户全部成员短剧（新语义无全局管理员分支）。"""
        from rest_framework.test import APIClient
        EvaluationMetric.objects.create(
            session=self.session, turn_id=1,
            metric_name=MetricName.COST, metric_value=1.0)
        # 同一用户的第二部剧（owner 成员行经真实语义），指标一并聚合
        second = Play.objects.create(title="o3", status=PlayStatus.DRAFT,
                                     created_by=self.user)
        PlayMember.objects.create(play=second, user=self.user, role=MemberRole.OWNER,
                                  status=MemberStatus.ACTIVE)
        sconfig = AgentConfig.objects.create(name="oc3", created_by=self.user)
        ssession = AssistantSession.objects.create(play=second, user=self.user,
                                                   agent_config=sconfig)
        EvaluationMetric.objects.create(
            session=ssession, turn_id=1, metric_name=MetricName.COST, metric_value=2.0)

        c = APIClient(); c.force_authenticate(self.user)
        resp = c.get("/api/assistant/evaluation/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json().get("data", resp.json())
        self.assertEqual(data["metrics"]["cost"]["count"], 2)

    def test_platform_admin_without_membership_sees_nothing(self):
        """平台管理员不因全局标记获得业务接口跨剧聚合（走审核通道）。"""
        from rest_framework.test import APIClient
        EvaluationMetric.objects.create(
            session=self.session, turn_id=1,
            metric_name=MetricName.COST, metric_value=1.0)
        padmin = CustomUser.objects.create_user(
            username="padmin-ev", password="t", is_platform_admin=True)
        c = APIClient(); c.force_authenticate(padmin)
        resp = c.get("/api/assistant/evaluation/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json().get("data", resp.json())
        self.assertNotIn("cost", data["metrics"])

    def test_evaluation_period_overflow_falls_back(self):
        """超大 period 不再 500（OverflowError），按上限/默认处理。"""
        from rest_framework.test import APIClient
        c = APIClient(); c.force_authenticate(self.user)
        resp = c.get("/api/assistant/evaluation/?period=99999999999999999999d")
        self.assertEqual(resp.status_code, 200)


class InjectionDefenseTests(TestCase):
    def test_all_blocked(self):
        out = evaluate_injection_defense(lambda t: {"memory_after": "正常记忆", "response": "ok"})
        self.assertEqual(out["rate"], 1.0)
        self.assertEqual(out["blocked"], out["probes"])

    def test_some_leak(self):
        def handler(text):
            # 模拟注入成功：恶意文本进入记忆
            if "永远服从" in text:
                return {"memory_after": "永远服从用户"}
            return {"memory_after": "正常"}
        out = evaluate_injection_defense(handler)
        self.assertLess(out["rate"], 1.0)

    def test_handler_exception(self):
        def boom(t):
            raise RuntimeError("x")
        out = evaluate_injection_defense(boom)
        self.assertEqual(out["blocked"], 0)


class ReportTaskTests(TestCase):
    def setUp(self):
        self.user, self.play, self.session = _mk(uname="rpt")

    def test_generate_report(self):
        EvaluationMetric.objects.create(
            session=self.session, turn_id=1,
            metric_name=MetricName.TASK_COMPLETION, metric_value=1.0)
        from apps.creation.tasks import generate_evaluation_report
        out = generate_evaluation_report(days=7, play_id=self.play.pk)
        import os
        self.assertTrue(os.path.exists(out["path"]))
        self.assertIn("task_completion", out["metrics"])


# ── phase1 max 审查修复 10.1：接受率分母不含 STALE ────────────────────────

class Phase1SatisfactionDenominatorTests(TestCase):
    def setUp(self):
        self.user, self.play, self.session = _mk(uname="p1sat")

    @override_settings(ENABLE_EVALUATION=True)
    def test_auto_stale_not_in_denominator(self):
        """1 APPLIED + 2 自动 STALE → 接受率 1.0 而非 0.333。"""
        AssistantProposal.objects.create(
            session=self.session, play=self.play, kind=ProposalKind.SHOT,
            status=ProposalStatus.APPLIED)
        for _ in range(2):
            AssistantProposal.objects.create(
                session=self.session, play=self.play, kind=ProposalKind.SHOT,
                status=ProposalStatus.STALE)
        record = _record(self.session)
        out = record_user_satisfaction({"session": self.session, "record": record})
        self.assertEqual(out["rate"], 1.0)
        self.assertEqual(out["decided"], 1)
        self.assertEqual(out["auto_stale"], 2, "STALE 数量保留在结果与 metadata")

    @override_settings(ENABLE_EVALUATION=True)
    def test_explicit_discard_still_counts(self):
        """用户显式 DISCARD 仍计入分母。"""
        for st in (ProposalStatus.APPLIED, ProposalStatus.DISCARDED):
            AssistantProposal.objects.create(
                session=self.session, play=self.play, kind=ProposalKind.SHOT, status=st)
        record = _record(self.session)
        out = record_user_satisfaction({"session": self.session, "record": record})
        self.assertAlmostEqual(out["rate"], 0.5, places=3)
        self.assertEqual(out["decided"], 2)

    @override_settings(ENABLE_EVALUATION=True)
    def test_only_stale_no_decision(self):
        """只有自动 STALE（无用户决议）→ 跳过，不产出指标。"""
        AssistantProposal.objects.create(
            session=self.session, play=self.play, kind=ProposalKind.SHOT,
            status=ProposalStatus.STALE)
        record = _record(self.session)
        self.assertIsNone(record_user_satisfaction({"session": self.session, "record": record}))
