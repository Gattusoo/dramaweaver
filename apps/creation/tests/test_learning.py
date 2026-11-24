"""经验学习机制测试（openspec change: 2026-11-14-backend-learning / Phase 2.2）。"""
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from apps.creation.assistant import events, learning
from apps.creation.assistant.models import (
    AssistantProposal, AssistantSession, MessageRole, ProposalKind, SkillLibrary, SkillScope,
    SkillType, StrategyExperiment,
)
from apps.creation.assistant.runner import _save, run_assistant_turn, system_prompt
from apps.creation.models import TaskRecord, TaskStatus, TaskType

from .test_assistant import AssistantFixtureMixin, FakeChatGateway, _say, _use

LEARN_FLAGS = {"ENABLE_LEARNING": True, "ENABLE_SKILL_LIBRARY": True}


class ConfidenceTests(TestCase):
    def test_laplace_formula(self):
        self.assertEqual(learning.confidence_of(9, 1), 0.8333)
        self.assertEqual(learning.confidence_of(0, 0), 0.5)
        self.assertEqual(learning.confidence_of(0, 5), round(1 / 7, 4))
        self.assertEqual(learning.confidence_of(-3, -1), 0.5)  # 负值归零

    def test_refresh_persists(self):
        skill = SkillLibrary.objects.create(name="s", success_count=9, failure_count=1)
        self.assertEqual(learning._refresh(skill), 0.8333)
        skill.refresh_from_db()
        self.assertEqual(skill.confidence, 0.8333)


class ExtractTests(AssistantFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                                input_summary={"session_id": self.session.pk})
        _save(self.session, MessageRole.USER, [{"type": "text", "text": "第3集结尾反转再狠一点"}], self.record)

    def test_llm_extraction(self):
        gw = FakeChatGateway([_say('{"name":"结尾反转强化","skill_type":"STRUCTURE",'
                                   '"trigger_conditions":["反转","结尾"],'
                                   '"execution_steps":["读剧本","提提案"],"description":"加强反转"}')])
        exp = learning.extract_experience(self.session, self.record, gateway=gw)
        self.assertEqual(exp["name"], "结尾反转强化")
        self.assertEqual(exp["skill_type"], SkillType.STRUCTURE)
        self.assertEqual(exp["trigger_conditions"], ["反转", "结尾"])

    def test_llm_failure_falls_back_to_heuristic(self):
        with self.assertLogs("apps.creation.assistant.learning", "ERROR"):
            exp = learning.extract_experience(self.session, self.record,
                                              gateway=FakeChatGateway([lambda r: (_ for _ in ()).throw(RuntimeError("x"))]))
        self.assertEqual(exp["name"], "反转钩子强化")
        self.assertIn("反转", exp["trigger_conditions"])
        self.assertTrue(exp["execution_steps"])

    def test_heuristic_generic_when_no_keyword(self):
        # 通用兜底不带触发条件（单字符触发会污染检索）；名称兜底在 extract_experience 末尾
        exp = learning.heuristic_experience("随便聊聊")
        self.assertEqual(exp["name"], "通用创作任务流程")
        self.assertEqual(exp["trigger_conditions"], [])

    def test_invalid_type_normalized(self):
        exp = learning._normalize_experience({"name": "x", "skill_type": "NOPE"})
        self.assertEqual(exp["skill_type"], SkillType.OTHER)


class SkillLibraryTests(AssistantFixtureMixin, TestCase):
    def make(self, name, triggers, success=0, failure=0, play=None, scope=None):
        return SkillLibrary.objects.create(
            name=name, scope=scope or (SkillScope.PLAY if play else SkillScope.GLOBAL), play=play,
            trigger_conditions=triggers, success_count=success, failure_count=failure,
            confidence=learning.confidence_of(success, failure))

    def test_upsert_accumulates_and_merges_triggers(self):
        exp = {"name": "反转钩子强化", "skill_type": SkillType.STRUCTURE,
               "trigger_conditions": ["反转"], "execution_steps": ["读剧本"]}
        s1 = learning.upsert_skill(self.session, exp)
        s2 = learning.upsert_skill(self.session, {**exp, "trigger_conditions": ["钩子"]})
        self.assertEqual(s1.pk, s2.pk)
        self.assertEqual(s2.success_count, 2)
        self.assertEqual(s2.trigger_conditions, ["反转", "钩子"])
        self.assertEqual(s2.confidence, learning.confidence_of(2, 0))

    def test_upsert_failure_increments_failure(self):
        exp = {"name": "x", "trigger_conditions": ["a"], "execution_steps": []}
        skill = learning.upsert_skill(self.session, exp, succeeded=False)
        self.assertEqual((skill.success_count, skill.failure_count), (0, 1))

    def test_search_scoped_and_ranked(self):
        self.make("全局反转", ["反转"], success=9, failure=1)
        self.make("本剧反转", ["反转"], success=1, failure=1, play=self.play)
        self.make("别的剧反转", ["反转"], success=9, failure=0, play=self.play.__class__.objects.create(
            title="别的", created_by=self.producer))
        self.make("无关技能", ["对白"])
        hits = learning.search_skills("把反转再加强", play=self.play)
        self.assertEqual([s.name for s in hits], ["全局反转", "本剧反转"])

    def test_search_limit_five(self):
        for i in range(8):
            self.make(f"技能{i}", ["反转"], success=i)
        self.assertEqual(len(learning.search_skills("反转", play=self.play)), 5)

    def test_inactive_excluded(self):
        skill = self.make("停用", ["反转"], success=5)
        skill.is_active = False
        skill.save()
        self.assertEqual(learning.search_skills("反转", play=self.play), [])

    def test_prompt_section(self):
        skill = self.make("反转钩子强化", ["反转"], success=9, failure=1)
        section = learning.build_learning_prompt_section([skill])
        self.assertIn("反转钩子强化", section)
        self.assertIn("0.83", section)
        self.assertIn("反转", section)
        self.assertEqual(learning.build_learning_prompt_section([]), "")

    def test_update_requires_flags(self):
        with override_settings(ENABLE_LEARNING=False, ENABLE_SKILL_LIBRARY=True):
            self.assertIsNone(learning.update_skill_library(self.session, None, gateway=FakeChatGateway([])))
        with override_settings(ENABLE_LEARNING=True, ENABLE_SKILL_LIBRARY=False):
            self.assertIsNone(learning.update_skill_library(self.session, None, gateway=FakeChatGateway([])))

    @override_settings(**LEARN_FLAGS)
    def test_should_learn_only_success_with_proposal(self):
        record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                           status=TaskStatus.SUCCESS,
                                           output_summary={"outcome": "done"})
        self.assertFalse(learning.should_learn(self.session, record))  # 无提案
        AssistantProposal.objects.create(session=self.session, play=self.play, kind=ProposalKind.SCENE,
                                         target_scene=self.scene)
        self.assertTrue(learning.should_learn(self.session, record))
        record.output_summary = {"outcome": "error"}
        self.assertFalse(learning.should_learn(self.session, record))


class OptimizeStrategyTests(AssistantFixtureMixin, TestCase):
    def test_none_when_samples_insufficient(self):
        SkillLibrary.objects.create(name="a", trigger_conditions=["x"], success_count=1, failure_count=0)
        self.assertIsNone(learning.optimize_strategy())

    def test_picks_best_success_rate(self):
        SkillLibrary.objects.create(name="a", trigger_conditions=["x"], success_count=4, failure_count=1,
                                    confidence=0.8)
        SkillLibrary.objects.create(name="b", trigger_conditions=["x"], success_count=2, failure_count=2,
                                    confidence=0.5)
        best = learning.optimize_strategy()
        self.assertEqual(best["name"], "a")
        self.assertEqual(best["success_rate"], 0.8)
        self.assertEqual(best["samples"], 5)


class ExperimentTests(TestCase):
    def setUp(self):
        from dvadmin.system.models import CustomUser
        self.user = CustomUser.objects.create_user(username="exp", password="Passw0rd!123")
        self.exp = StrategyExperiment.objects.create(
            name="策略A/B", created_by=self.user,
            variants=learning.normalize_variants([
                {"key": "A", "name": "先读后写", "weight": 1},
                {"key": "B", "name": "先写后读", "weight": 1},
            ]))

    def test_deterministic_bucket(self):
        picks = {learning.assign_variant(self.exp, "play-3-turn-7")["key"] for _ in range(5)}
        self.assertEqual(len(picks), 1)
        # 不同 key 能覆盖到两个变体
        keys = {learning.assign_variant(self.exp, f"k{i}")["key"] for i in range(40)}
        self.assertEqual(keys, {"A", "B"})

    def test_weighted_bucket(self):
        exp = StrategyExperiment.objects.create(name="w", variants=learning.normalize_variants([
            {"key": "A", "weight": 9}, {"key": "B", "weight": 1}]))
        counts = {"A": 0, "B": 0}
        for i in range(400):
            counts[learning.assign_variant(exp, f"key-{i}")["key"]] += 1
        self.assertGreater(counts["A"], counts["B"] * 3)

    def test_record_outcome_and_report(self):
        for _ in range(8):
            learning.record_outcome(self.exp, "A", success=True)
        for _ in range(2):
            learning.record_outcome(self.exp, "A", success=False)
        for _ in range(5):
            learning.record_outcome(self.exp, "B", success=True)
        for _ in range(5):
            learning.record_outcome(self.exp, "B", success=False)
        report = learning.experiment_report(self.exp)
        rates = {r["key"]: r["success_rate"] for r in report["variants"]}
        self.assertEqual(rates, {"A": 0.8, "B": 0.5})
        self.assertEqual(report["best_variant"], "A")
        self.assertEqual(report["winner"]["samples"], 10)

    def test_finish_when_conclusive(self):
        for _ in range(9):
            learning.record_outcome(self.exp, "A", success=True)
        learning.record_outcome(self.exp, "A", success=False)
        for _ in range(3):
            learning.record_outcome(self.exp, "B", success=True)
        for _ in range(7):
            learning.record_outcome(self.exp, "B", success=False)
        report = learning.finish_if_conclusive(self.exp)
        self.exp.refresh_from_db()
        self.assertEqual(self.exp.status, "COMPLETED")
        self.assertEqual(report["best_variant"], "A")

    def test_unknown_variant_raises(self):
        with self.assertRaises(KeyError):
            learning.record_outcome(self.exp, "Z", success=True)


class RunnerLearningTests(AssistantFixtureMixin, TestCase):
    def turn(self, gw, text="第3集结尾反转再狠一点", flags=None):
        record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                           input_summary={"session_id": self.session.pk})
        _save(self.session, MessageRole.USER, [{"type": "text", "text": text}], record)
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        with override_settings(**(flags or LEARN_FLAGS)), patch(
                "apps.ai_gateway.gateway.AIGateway", return_value=gw):
            run_assistant_turn(record.pk)
        record.refresh_from_db()
        return record

    @override_settings(**LEARN_FLAGS)
    def test_successful_turn_creates_skill(self):
        gw = FakeChatGateway([
            _use("t1", "propose_scene_edit", {"scene_id": self.scene.pk, "description": "她亮出录音", "summary": "反转"}),
            _say("已提案"),
            _say('{"name":"反转钩子强化","skill_type":"STRUCTURE","trigger_conditions":["反转"],'
                 '"execution_steps":["读剧本","提提案"],"description":"加强反转"}'),
        ])
        record = self.turn(gw)
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        skill = SkillLibrary.objects.get(play=self.play, name="反转钩子强化")
        self.assertEqual(skill.success_count, 1)
        self.assertEqual(skill.confidence, learning.confidence_of(1, 0))

    @override_settings(**LEARN_FLAGS)
    def test_no_learning_without_proposal(self):
        record = self.turn(FakeChatGateway([_say("只是聊天")]))
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        self.assertFalse(SkillLibrary.objects.exists())

    def test_disabled_no_learning(self):
        gw = FakeChatGateway([
            _use("t1", "propose_scene_edit", {"scene_id": self.scene.pk, "description": "x", "summary": "s"}),
            _say("ok"),
        ])
        self.turn(gw, flags={"ENABLE_LEARNING": False, "ENABLE_SKILL_LIBRARY": False})
        self.assertFalse(SkillLibrary.objects.exists())

    @override_settings(**LEARN_FLAGS)
    def test_skill_injected_into_system_prompt(self):
        SkillLibrary.objects.create(play=self.play, scope=SkillScope.PLAY, name="反转钩子强化", trigger_conditions=["反转"],
                                    description="加强反转", success_count=9, failure_count=1,
                                    confidence=0.8333)
        _save(self.session, MessageRole.USER, [{"type": "text", "text": "把反转再加强"}], None)
        prompt = system_prompt(self.session)
        self.assertIn("相关经验（可能适用）", prompt)
        self.assertIn("反转钩子强化", prompt)

    @override_settings(ENABLE_LEARNING=True, ENABLE_SKILL_LIBRARY=False)
    def test_skill_not_injected_when_disabled(self):
        SkillLibrary.objects.create(play=self.play, scope=SkillScope.PLAY, name="反转钩子强化", trigger_conditions=["反转"],
                                    description="加强反转")
        _save(self.session, MessageRole.USER, [{"type": "text", "text": "反转"}], None)
        self.assertNotIn("相关经验（可能适用）", system_prompt(self.session))


class SkillLibraryApiTests(AssistantFixtureMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.url = "/api/assistant/skill-library/"

    def test_create_and_list(self):
        self.client.force_authenticate(self.producer)
        resp = self.client.post(self.url, {"name": "反转钩子强化", "scope": "PLAY", "play": self.play.pk,
                                          "skill_type": "STRUCTURE", "trigger_conditions": ["反转"],
                                          "execution_steps": ["读剧本"], "description": "d"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["success_count"], 0)
        self.assertEqual(resp.data["confidence"], 0.5)
        listing = self.client.get(f"{self.url}?play={self.play.pk}")
        self.assertEqual([r["name"] for r in listing.data["results"]], ["反转钩子强化"])

    def test_duplicate_name_rejected(self):
        SkillLibrary.objects.create(play=self.play, scope=SkillScope.PLAY, name="重复",
                                    trigger_conditions=["a"])
        self.client.force_authenticate(self.producer)
        resp = self.client.post(self.url, {"name": "重复", "scope": "PLAY", "play": self.play.pk,
                                          "trigger_conditions": [], "execution_steps": []}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_play_scope_requires_play(self):
        self.client.force_authenticate(self.producer)
        resp = self.client.post(self.url, {"name": "x", "scope": "PLAY"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_viewer_cannot_create(self):
        self.client.force_authenticate(self.viewer)
        resp = self.client.post(self.url, {"name": "x", "scope": "PLAY", "play": self.play.pk,
                                          "trigger_conditions": [], "execution_steps": []}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_global_requires_admin(self):
        self.client.force_authenticate(self.producer)
        resp = self.client.post(self.url, {"name": "全局x", "scope": "GLOBAL"}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_archive(self):
        skill = SkillLibrary.objects.create(play=self.play, scope=SkillScope.PLAY, name="归档", trigger_conditions=["a"])
        self.client.force_authenticate(self.producer)
        resp = self.client.post(f"{self.url}{skill.pk}/archive/")
        self.assertEqual(resp.status_code, 200)
        skill.refresh_from_db()
        self.assertFalse(skill.is_active)

    def test_invalid_trigger_types_rejected(self):
        self.client.force_authenticate(self.producer)
        resp = self.client.post(self.url, {"name": "x", "scope": "PLAY", "play": self.play.pk,
                                          "trigger_conditions": "反转"}, format="json")
        self.assertEqual(resp.status_code, 400)


class ExperimentApiTests(AssistantFixtureMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.url = "/api/assistant/experiments/"

    def create(self):
        self.client.force_authenticate(self.producer)
        return self.client.post(self.url, {"name": "A/B", "variants": [
            {"key": "A", "name": "先读", "weight": 1}, {"key": "B", "name": "先写", "weight": 1}]},
            format="json")

    def test_create_requires_two_variants(self):
        self.client.force_authenticate(self.producer)
        resp = self.client.post(self.url, {"name": "x", "variants": [{"key": "A"}]}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_outcome_by_bucket_and_report(self):
        resp = self.create()
        self.assertEqual(resp.status_code, 201, resp.data)
        pk = resp.data["id"]
        for i in range(6):
            r = self.client.post(f"{self.url}{pk}/outcome/", {"bucket_key": f"turn-{i}", "success": True},
                                 format="json")
            self.assertEqual(r.status_code, 200)
        report = self.client.get(f"{self.url}{pk}/report/")
        self.assertEqual(report.status_code, 200)
        self.assertEqual(sum(v["samples"] for v in report.data["variants"]), 6)

    def test_outcome_requires_key(self):
        pk = self.create().data["id"]
        resp = self.client.post(f"{self.url}{pk}/outcome/", {"success": True}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_outcome_unknown_variant(self):
        pk = self.create().data["id"]
        resp = self.client.post(f"{self.url}{pk}/outcome/", {"variant_key": "Z", "success": True},
                                format="json")
        self.assertEqual(resp.status_code, 400)

    def test_other_user_cannot_see(self):
        pk = self.create().data["id"]
        self.client.force_authenticate(self.viewer)
        self.assertEqual(self.client.get(f"{self.url}{pk}/").status_code, 404)


class SeedCommandTests(TestCase):
    def test_seed_fifty_and_idempotent(self):
        call_command("seed_skill_library", verbosity=0)
        self.assertEqual(SkillLibrary.objects.filter(scope=SkillScope.GLOBAL).count(), 50)
        call_command("seed_skill_library", verbosity=0)
        self.assertEqual(SkillLibrary.objects.count(), 50)

    def test_seed_play_scope(self):
        from apps.creation.models import Play
        from dvadmin.system.models import CustomUser
        user = CustomUser.objects.create_user(username="s", password="Passw0rd!123")
        play = Play.objects.create(title="t", created_by=user)
        call_command("seed_skill_library", "--play", str(play.pk), verbosity=0)
        self.assertEqual(SkillLibrary.objects.filter(scope=SkillScope.PLAY, play=play).count(), 50)


# ── Phase 2 code-review 修复回归 ─────────────────────────────────────────────

class HeuristicTriggerFixTests(AssistantFixtureMixin, TestCase):
    """修复：启发式兜底用单字符做触发条件污染检索。"""

    def test_generic_fallback_has_no_char_triggers(self):
        exp = learning.heuristic_experience("随便聊聊点什么")
        self.assertEqual(exp["name"], "通用创作任务流程")
        # 修复前 trigger_conditions 是 ["随","便","聊","聊",...]（前 20 字符拆单字）
        self.assertEqual(exp["trigger_conditions"], [])

    def test_extract_fills_triggers_from_name(self):
        # LLM 提取失败 → 启发式兜底 → 末尾用技能名补触发条件（多字符）
        class BadGateway:
            def chat(self, *args, **kwargs):
                raise RuntimeError("provider down")

        exp = learning.extract_experience(self.session, gateway=BadGateway())
        self.assertEqual(exp["trigger_conditions"], [exp["name"]])
        self.assertTrue(all(len(t) > 1 for t in exp["trigger_conditions"]),
                        exp["trigger_conditions"])


class GlobalSkillVisibilityTests(AssistantFixtureMixin, APITestCase):
    """修复：非管理员列表查不到 GLOBAL 技能（IN 不匹配 NULL）。"""

    def test_viewer_sees_global_skills(self):
        SkillLibrary.objects.create(scope=SkillScope.GLOBAL, name="全局技能", play=None,
                                    trigger_conditions=["反转"], description="d")
        self.client.force_authenticate(self.viewer)
        resp = self.client.get("/api/assistant/skill-library/")
        self.assertEqual(resp.status_code, 200)
        names = [r["name"] for r in resp.data["results"]]
        self.assertIn("全局技能", names, names)

    def test_play_filter_still_scoped(self):
        SkillLibrary.objects.create(scope=SkillScope.GLOBAL, name="全局技能", play=None,
                                    trigger_conditions=["反转"])
        self.client.force_authenticate(self.viewer)
        # 指定 play 过滤时只看该 play 的技能（不过滤出全局技能）
        resp = self.client.get(f"/api/assistant/skill-library/?play={self.play.pk}")
        self.assertEqual([r["name"] for r in resp.data["results"]], [])


class StrictBoolOutcomeTests(AssistantFixtureMixin, APITestCase):
    """修复：outcome 的 success 入参用 bool() 解析，字符串 "false" 被记成成功。"""

    def setUp(self):
        super().setUp()
        self.url = "/api/assistant/experiments/"
        self.client.force_authenticate(self.producer)
        resp = self.client.post(self.url, {"name": "A/B", "variants": [
            {"key": "A", "weight": 1}, {"key": "B", "weight": 1}]}, format="json")
        self.pk = resp.data["id"]

    def report(self):
        return self.client.get(f"{self.url}{self.pk}/report/").data

    def test_string_false_recorded_as_failure(self):
        resp = self.client.post(f"{self.url}{self.pk}/outcome/",
                                {"bucket_key": "turn-x", "success": "false"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(sum(v["failure"] for v in self.report()["variants"]), 1)
        self.assertEqual(sum(v["success"] for v in self.report()["variants"]), 0)

    def test_invalid_string_rejected(self):
        resp = self.client.post(f"{self.url}{self.pk}/outcome/",
                                {"bucket_key": "turn-x", "success": "yes"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_missing_success_keeps_old_false_default(self):
        resp = self.client.post(f"{self.url}{self.pk}/outcome/",
                                {"bucket_key": "turn-x"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(sum(v["failure"] for v in self.report()["variants"]), 1)
