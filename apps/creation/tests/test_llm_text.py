"""LLM 输出解析测试（phase1 修复 6.1/6.2：括号配平扫描 + 降级日志）。"""
from unittest.mock import patch

from django.test import TestCase

from apps.creation.assistant.llm_text import extract_json, extract_json_array, extract_json_object


class ExtractJsonBalancedScanTests(TestCase):
    """6.1：括号配平扫描（跳过字符串内括号、失败后从下一位置重试）。"""

    def test_json_followed_by_prose_with_brackets(self):
        """JSON 后跟含 {}/[] 的说明文字。"""
        text = '{"scenes": [{"n": 1}]} 结果如上 {另见附录}，细节 [略]。'
        out = extract_json(text)
        self.assertEqual(out, {"scenes": [{"n": 1}]})

    def test_json_followed_by_example_code_block(self):
        """JSON 后跟示例代码块（含嵌套括号与字符串）。"""
        text = ('{"episodes": 2}\n\n示例：\n```json\n{"bad": "unclosed { in string"}\n```\n'
                '以及 array [1, 2]')
        out = extract_json(text)
        self.assertEqual(out, {"episodes": 2})

    def test_prefix_contains_square_brackets(self):
        """前缀含方括号（如 markdown 链接），后面的对象仍被提取。"""
        text = '参考 [第1集](http://x/e1) 的设定：{"target": "scene_1", "op": "edit"}'
        out = extract_json(text)
        self.assertEqual(out, {"target": "scene_1", "op": "edit"})

    def test_array_with_braces_inside_strings(self):
        """数组元素字符串内含花括号，不影响配平。"""
        text = '说明 [ {\"tpl\": \"hi {name}\"} ]'
        out = extract_json(text)
        self.assertEqual(out, [{"tpl": "hi {name}"}])

    def test_prose_returns_none(self):
        """纯散文返回 None 不抛异常。"""
        self.assertIsNone(extract_json("今天天气不错，没有结构化内容。"))
        self.assertIsNone(extract_json(""))
        self.assertIsNone(extract_json(None))

    def test_unbalanced_returns_none(self):
        self.assertIsNone(extract_json('{"unclosed": '))
        self.assertIsNone(extract_json('[1, 2'))

    def test_first_balanced_span_wins(self):
        """起点配平失败后从下一位置重试：孤立括号后的完整对象仍可提取。"""
        text = '废弃 { 丢弃 {"ok": 1}'
        out = extract_json(text)
        self.assertEqual(out, {"ok": 1})

    def test_type_wrappers(self):
        self.assertEqual(extract_json_object('[1]'), {})
        self.assertEqual(extract_json_array('{"a": 1}'), [])
        self.assertEqual(extract_json_object('前缀 {"a": 1} 后缀'), {"a": 1})


class TasksDelegationTests(TestCase):
    """6.1：tasks._parse_json 委托共用实现，行为等价。"""

    def test_delegation(self):
        from apps.creation.tasks import _parse_json
        self.assertEqual(_parse_json('{"scenes":[{"n":1},{"n":2}]}')["scenes"][1]["n"], 2)
        self.assertIsNone(_parse_json("非 JSON"))
        self.assertEqual(len(_parse_json('[{"a":1},{"b":2}]')), 2)


class DegradationLoggingTests(TestCase):
    """6.2：结构化输出提取失败时降级且记录日志。"""

    def test_critic_feedback_degrades_with_log(self):
        """multi_agent：不可解析输出 → 空分数继续 + 警告日志。"""
        import logging
        from apps.creation.assistant.multi_agent import parse_critic_feedback
        with self.assertLogs("apps.creation.assistant.multi_agent", level="WARNING") as logs:
            out = parse_critic_feedback("审稿意见：整体不错（无法解析为 JSON）")
        self.assertEqual(out["overall"], None)
        self.assertEqual(out["scores"], {})
        self.assertEqual(out["suggestions"], [])
        self.assertTrue(any("无法解析" in m for m in logs.output))

    def test_reflection_degrades_with_log(self):
        """planning：LLM 反思失败 → 降级启发式 + 警告日志（不抛异常）。"""
        import logging
        from apps.creation.assistant.models import AssistantSession
        from apps.creation.assistant.planning import reflect_on_failure
        from apps.creation.models import AgentConfig, Play, PlayStatus
        from dvadmin.system.models import CustomUser
        user = CustomUser.objects.create_user(username="p1llm", password="t")
        play = Play.objects.create(title="t", status=PlayStatus.DRAFT, created_by=user)
        config = AgentConfig.objects.create(name="c", created_by=user)
        session = AssistantSession.objects.create(play=play, user=user, agent_config=config)
        with patch("apps.creation.assistant.planning.llm_reflect",
                   side_effect=RuntimeError("provider down")):
            with self.assertLogs("apps.creation.assistant.planning", level="WARNING") as logs:
                r = reflect_on_failure(session, "get_scene", [{"tool": "get_scene"}])
        self.assertIn("核对参数", r.lessons_learned)  # 启发式默认教训
        self.assertTrue(any("反思失败" in m for m in logs.output))

    def test_plan_degrades_with_log(self):
        """planning：LLM 计划分解失败 → 降级模板步骤 + 警告日志。"""
        import logging
        from apps.creation.assistant.planning import PlanningEngine
        from apps.creation.assistant.models import AssistantSession
        from apps.creation.models import AgentConfig, Play, PlayStatus
        from dvadmin.system.models import CustomUser
        user = CustomUser.objects.create_user(username="p1llm2", password="t")
        play = Play.objects.create(title="t", status=PlayStatus.DRAFT, created_by=user)
        config = AgentConfig.objects.create(name="c", created_by=user)
        session = AssistantSession.objects.create(play=play, user=user, agent_config=config)
        engine = PlanningEngine(session, planner=lambda g, c, t: (_ for _ in ()).throw(RuntimeError("down")))
        with self.assertLogs("apps.creation.assistant.planning", level="WARNING") as logs:
            plan = engine.create_plan("写第一集")
        self.assertIsNotNone(plan)
        self.assertTrue(plan.steps)  # 启发式模板步骤
        self.assertTrue(any("LLM规划失败" in m for m in logs.output))
