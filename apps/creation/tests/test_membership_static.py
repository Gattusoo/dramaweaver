"""静态一致性防护（任务 6.1）：全局四级角色与 authorized_users 不得残留消费方。

漏改点的症状是"权限比预期松"——用扫描兜底，防止后续改动重新引入全局角色分支。
对齐 dvadmin/system/tests/test_config.py 的既有风格。
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

REPO = Path(__file__).resolve().parents[3]
SCAN_DIRS = ("apps", "dvadmin")

# 允许保留的引用（迁移历史、模型定义、测试、注释性文档）
ALLOW = {
    # 模型字段定义（停读但保留列）
    "dvadmin/system/models.py",
    "dvadmin/system/migrations",
    "apps/creation/migrations",
    "apps/public_api/serializers.py",  # 注释说明
    "apps/ai_gateway/adapters.py",     # OpenAI 消息 role= 参数，非 UserRole
}


def _scan(pattern, skip_parts=("tests", "__pycache__")):
    hits = []
    rx = re.compile(pattern)
    for base in SCAN_DIRS:
        for path in (REPO / base).rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if any(part in path.parts for part in skip_parts):
                continue
            if any(rel.startswith(a) for a in ALLOW):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            code = "\n".join(
                ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
            )
            for i, line in enumerate(code.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()}")
    return hits


class LegacyRoleRetirementTests(SimpleTestCase):
    def test_no_global_role_branch_in_production_code(self):
        """production 代码不得再按全局四级角色分支判定业务权限。"""
        hits = _scan(r"UserRole\.(ADMIN|PRODUCER|EDITOR|VIEWER)")
        self.assertEqual(hits, [], "残留全局角色分支（应改剧内成员表判定）：\n" + "\n".join(hits))

    def test_no_authorized_users_in_production_code(self):
        """authorized_users 字段已删除，不得残留消费方。"""
        hits = _scan(r"authorized_users")
        self.assertEqual(hits, [], "残留 authorized_users 引用：\n" + "\n".join(hits))

    def test_platform_admin_marker_used_for_platform_endpoints(self):
        """平台端点权限应读 is_platform_admin 标记。"""
        hits = _scan(r"is_platform_admin")
        self.assertGreater(len(hits), 0, "is_platform_admin 标记无任何消费方（接线缺失）")
