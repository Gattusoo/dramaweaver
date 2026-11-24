"""Skills 机制测试（Phase 1.5）：渐进式披露、触发匹配、内置Skill。"""
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.creation.assistant import skills as sk
from apps.creation.assistant.models import AssistantSession
from apps.creation.assistant.runner import system_prompt
from apps.creation.assistant.tools import ToolContext, get_tool, run_tool
from apps.creation.models import AgentConfig, Play, PlayStatus
from dvadmin.system.models import CustomUser

BUILTIN = Path(sk.__file__).resolve().parent / "builtin" / "skills"


def _reset_cache():
    sk._cache = {}


class SkillParseTests(TestCase):
    def test_parse_builtin_skill(self):
        skill = sk._load_skill_file(BUILTIN / "pacing_analysis" / "skill.md", "builtin")
        self.assertEqual(skill.name, "pacing_analysis")
        self.assertIn("节奏", skill.description)
        self.assertIn("节奏", skill.instructions)
        self.assertIn("get_episode", skill.tools)
        self.assertIn("节奏", skill.triggers)

    def test_all_builtin_skills_load(self):
        reg = sk._scan(BUILTIN, "builtin")
        self.assertGreaterEqual(len(reg), 6)
        for name in ("pacing_analysis", "dialogue_polish", "suspense_techniques",
                     "character_voice", "hook_design", "plot_twist"):
            self.assertIn(name, reg)
            self.assertTrue(reg[name].description)
            self.assertTrue(reg[name].instructions)
            self.assertTrue(reg[name].triggers)

    def test_missing_frontmatter(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "raw"
            d.mkdir()
            (d / "skill.md").write_text("# 纯正文无frontmatter", encoding="utf-8")
            skill = sk._load_skill_file(d / "skill.md", "custom")
        self.assertEqual(skill.name, "raw")
        self.assertEqual(skill.description, "")
        self.assertIn("纯正文", skill.instructions)


class SkillRegistryTests(TestCase):
    def setUp(self):
        _reset_cache()

    def test_registry_includes_builtin(self):
        reg = sk.skill_registry(refresh=True)
        self.assertIn("pacing_analysis", reg)
        self.assertIn("hook_design", reg)

    def test_custom_overrides_builtin(self):
        import tempfile
        from django.conf import settings
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "skills" / "pacing_analysis"
            root.mkdir(parents=True)
            (root / "skill.md").write_text(
                "---\nname: pacing_analysis\ndescription: 自定义节奏\n---\n覆盖版", encoding="utf-8")
            with override_settings(MEDIA_ROOT=td):
                _reset_cache()
                skill = sk.get_skill("pacing_analysis")
        self.assertEqual(skill.description, "自定义节奏")
        self.assertEqual(skill.source, "custom")

    def test_list_skills_brief(self):
        briefs = sk.list_skills()
        self.assertTrue(all("instructions" not in b for b in briefs))
        self.assertTrue(any(b["name"] == "plot_twist" for b in briefs))

    def test_load_skill_full(self):
        full = sk.load_skill("hook_design")
        self.assertIn("开场", full["instructions"])
        self.assertIn("tools", full)

    def test_load_missing(self):
        with self.assertRaises(KeyError):
            sk.load_skill("nonexistent")


class SkillMatchTests(TestCase):
    def setUp(self):
        _reset_cache()

    def test_match_by_trigger(self):
        hits = sk.match("帮我看看第3集节奏是不是太拖沓了")
        self.assertTrue(hits)
        self.assertEqual(hits[0].name, "pacing_analysis")

    def test_match_dialogue(self):
        hits = sk.match("这段对白写得太书面了，帮我润色")
        names = [s.name for s in hits]
        self.assertIn("dialogue_polish", names)

    def test_match_multi(self):
        hits = sk.match("结尾悬念不够，想要个反转")
        names = [s.name for s in hits]
        self.assertIn("suspense_techniques", names)
        self.assertIn("plot_twist", names)

    def test_no_match(self):
        self.assertEqual(sk.match("今天天气怎么样"), [])
        self.assertEqual(sk.match(""), [])


class SkillPromptTests(TestCase):
    def setUp(self):
        _reset_cache()

    @override_settings(ENABLE_SKILLS=False)
    def test_disabled_returns_empty(self):
        self.assertEqual(sk.build_skills_prompt_section(), "")

    @override_settings(ENABLE_SKILLS=True)
    def test_section_lists_all_briefs(self):
        section = sk.build_skills_prompt_section()
        self.assertIn("pacing_analysis", section)
        self.assertIn("hook_design", section)
        self.assertIn("load_skill", section)
        # 第一层只给描述，不给全文
        self.assertNotIn("分析框架", section)

    @override_settings(ENABLE_SKILLS=True)
    def test_matched_marked(self):
        matched = sk.match("节奏太慢")
        section = sk.build_skills_prompt_section(matched)
        self.assertIn("可能适用", section)


class SkillToolTests(TestCase):
    def setUp(self):
        _reset_cache()
        self.user = CustomUser.objects.create_user(username="sk", password="t")
        self.play = Play.objects.create(title="t", status=PlayStatus.DRAFT, created_by=self.user)
        self.config = AgentConfig.objects.create(name="c", created_by=self.user)
        self.session = AssistantSession.objects.create(
            play=self.play, user=self.user, agent_config=self.config)
        self.ctx = ToolContext(self.session)

    def test_tools_registered(self):
        self.assertIsNotNone(get_tool("list_skills"))
        self.assertIsNotNone(get_tool("load_skill"))

    def test_list_skills_tool(self):
        result, err = run_tool(self.ctx, "list_skills", {})
        self.assertFalse(err)
        self.assertTrue(any(s["name"] == "hook_design" for s in result["skills"]))

    def test_load_skill_tool(self):
        result, err = run_tool(self.ctx, "load_skill", {"name": "plot_twist"})
        self.assertFalse(err)
        self.assertIn("反转", result["instructions"])
        self.assertIn("get_episode", result["tools"])

    def test_load_skill_unknown(self):
        result, err = run_tool(self.ctx, "load_skill", {"name": "nope"})
        self.assertTrue(err)


class SkillApiTests(TestCase):
    def setUp(self):
        _reset_cache()
        self.user = CustomUser.objects.create_user(username="skapi", password="t")

    @override_settings(ENABLE_SKILLS=True)
    def test_list_endpoint(self):
        from rest_framework.test import APIClient
        c = APIClient()
        c.force_authenticate(self.user)
        resp = c.get("/api/assistant/skills/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json().get("data", resp.json())
        self.assertTrue(data["enabled"])
        self.assertTrue(any(s["name"] == "pacing_analysis" for s in data["skills"]))

    @override_settings(ENABLE_SKILLS=True)
    def test_retrieve_endpoint(self):
        from rest_framework.test import APIClient
        c = APIClient()
        c.force_authenticate(self.user)
        resp = c.get("/api/assistant/skills/dialogue_polish/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json().get("data", resp.json())
        self.assertIn("对白", data["instructions"])

    @override_settings(ENABLE_SKILLS=True)
    def test_retrieve_missing(self):
        from rest_framework.test import APIClient
        c = APIClient()
        c.force_authenticate(self.user)
        resp = c.get("/api/assistant/skills/nope/")
        self.assertEqual(resp.status_code, 404)

    @override_settings(ENABLE_SKILLS=False)
    def test_disabled_list_empty(self):
        from rest_framework.test import APIClient
        c = APIClient()
        c.force_authenticate(self.user)
        resp = c.get("/api/assistant/skills/")
        data = resp.json().get("data", resp.json())
        self.assertFalse(data["enabled"])
        self.assertEqual(data["skills"], [])


class SkillRunnerIntegrationTests(TestCase):
    def setUp(self):
        _reset_cache()
        self.user = CustomUser.objects.create_user(username="skr", password="t")
        self.play = Play.objects.create(title="t", status=PlayStatus.DRAFT, created_by=self.user)
        self.config = AgentConfig.objects.create(name="c", created_by=self.user)
        self.session = AssistantSession.objects.create(
            play=self.play, user=self.user, agent_config=self.config)

    @override_settings(ENABLE_SKILLS=True, ENABLE_MEMORY_SYSTEM=False)
    def test_prompt_includes_skills(self):
        prompt = system_prompt(self.session)
        self.assertIn("可用Skills", prompt)
        self.assertIn("pacing_analysis", prompt)

    @override_settings(ENABLE_SKILLS=False, ENABLE_MEMORY_SYSTEM=False)
    def test_prompt_excludes_when_disabled(self):
        prompt = system_prompt(self.session)
        self.assertNotIn("可用Skills", prompt)


# ── phase1 max 审查修复 9.1–9.3：技能注册表容错 ──────────────────────────

class Phase1SkillRobustnessTests(TestCase):
    def setUp(self):
        _reset_cache()
        self.addCleanup(_reset_cache)

    def test_compile_triggers_non_string(self):
        """9.1：triggers: [1, 2] 不抛异常，match() 正常。"""
        compiled = sk._compile_triggers([1, 2])
        self.assertEqual(len(compiled), 2)
        # match() 在含对应文本时不抛异常
        skills = sk.match("随便一句话 1")
        self.assertIsInstance(skills, list)

    def test_bad_skill_file_isolated(self):
        """9.2：畸形文件被跳过，合法技能仍注册且不抛异常。"""
        import tempfile
        from django.conf import settings
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "skills" / "broken"
            bad.mkdir(parents=True)
            (bad / "skill.md").write_text("---\nname: broken\ntriggers: [1, 2]\n"
                                          "description: 坏技能\n---\n内容", encoding="utf-8")
            good = Path(td) / "skills" / "goodone"
            good.mkdir(parents=True)
            (good / "skill.md").write_text("---\nname: goodone\n"
                                           "description: 合法技能\n---\n说明", encoding="utf-8")
            with override_settings(MEDIA_ROOT=td):
                _reset_cache()
                reg = sk.skill_registry(refresh=True)
                self.assertIn("goodone", reg)
                self.assertIn("broken", reg, "非字符串 triggers 现在能容忍加载")
        # 完全不可解析的文件（非 UTF-8 / 解析异常）也隔离
        with tempfile.TemporaryDirectory() as td:
            bad2 = Path(td) / "skills" / "bad2"
            bad2.mkdir(parents=True)
            (bad2 / "skill.md").write_bytes(b"\xff\xfe\x00bad frontmatter")
            ok = Path(td) / "skills" / "ok2"
            ok.mkdir(parents=True)
            (ok / "skill.md").write_text("---\nname: ok2\ndescription: d\n---\nb",
                                         encoding="utf-8")
            with override_settings(MEDIA_ROOT=td):
                _reset_cache()
                reg2 = sk.skill_registry(refresh=True)
                self.assertIn("ok2", reg2)

    def test_system_prompt_with_bad_file(self):
        """9.3：存在坏技能文件时 system_prompt 正常构建（ENABLE_SKILLS=true）。"""
        import tempfile
        from apps.creation.models import MemberRole, MemberStatus, PlayMember
        from django.conf import settings
        user = CustomUser.objects.create_user(username="p1sk", password="t")
        play = Play.objects.create(title="t", status=PlayStatus.DRAFT, created_by=user)
        PlayMember.objects.create(play=play, user=user, role=MemberRole.OWNER,
                                  status=MemberStatus.ACTIVE)
        config = AgentConfig.objects.create(name="c", created_by=user)
        session = AssistantSession.objects.create(play=play, user=user,
                                                  agent_config=config)
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "skills" / "bad3"
            bad.mkdir(parents=True)
            (bad / "skill.md").write_bytes(b"\x00\xff\xfe garbage")
            good = Path(td) / "skills" / "good3"
            good.mkdir(parents=True)
            (good / "skill.md").write_text(
                "---\nname: good3\ndescription: 合法技能条目\n---\n说明", encoding="utf-8")
            with override_settings(MEDIA_ROOT=td, ENABLE_SKILLS=True):
                _reset_cache()
                prompt = system_prompt(session)
        self.assertIsInstance(prompt, str)
        self.assertIn("合法技能条目", prompt)
