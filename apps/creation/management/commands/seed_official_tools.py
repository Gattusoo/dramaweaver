"""预置官方工具种子（Phase 3.1 市场侧 / 3.1.11）。

用法：
    python manage.py seed_official_tools           # 写入 10 个官方纯函数工具
    python manage.py seed_official_tools --dry-run # 只统计不写入

幂等：按 tool_name 存在即跳过（不更新既有条目——预填充语义是上架官方工具，
不覆盖本地可能已改的版本；取新版=删除后重跑）。

条目形状：OFFICIAL + is_verified=True + is_active=False（进市场目录，
激活由管理员决定；命令仅平台管理员场景使用，seed 不经 API）。
全部 handler 仅用既有沙箱 import 白名单，走既有沙箱执行。
"""
from django.core.management.base import BaseCommand

from apps.creation.assistant import registry
from apps.creation.assistant.models import ToolRegistry, ToolType

# (tool_name, category, description, input_schema, handler_code)
TOOLS = [
    ("text_keywords", "文本处理",
     "按词频提取文本关键词（中文按 2-4 字符切分，英文按单词），返回前 N 个词与频次",
     {"type": "object",
      "properties": {"text": {"type": "string", "maxLength": 5000},
                     "top_n": {"type": "integer", "minimum": 1, "maximum": 50}},
      "required": ["text"]},
     '''
def run(args):
    import re
    from collections import Counter
    text = (args.get("text") or "")[:5000]
    top_n = min(max(args.get("top_n", 10), 1), 50)
    words = re.findall(r"[a-zA-Z]{2,}", text)
    cn = re.findall(r"[\\u4e00-\\u9fa5]{2,4}", text)
    counts = Counter(w.lower() for w in words)
    for w in cn:
        for i in range(len(w) - 1):
            counts[w[i:i + 2]] += 1
    return {"keywords": [[w, c] for w, c in counts.most_common(top_n)]}
'''),
    ("text_stats", "文本处理",
     "统计文本基本信息：字符数、汉字数、英文单词数、句子数、段落数",
     {"type": "object",
      "properties": {"text": {"type": "string", "maxLength": 20000}},
      "required": ["text"]},
     '''
def run(args):
    import re
    text = args.get("text") or ""
    cn = len(re.findall(r"[\\u4e00-\\u9fa5]", text))
    words = len(re.findall(r"[a-zA-Z]+", text))
    sentences = len([s for s in re.split(r"[。！？.!?\\n]+", text) if s.strip()])
    paragraphs = len([p for p in text.split("\\n") if p.strip()])
    return {"chars": len(text), "chinese_chars": cn, "english_words": words,
            "sentences": sentences, "paragraphs": paragraphs}
'''),
    ("slug_convert", "格式转换",
     "把中文短语或标题转换为 slug（拼音首字母不可用时退化为：非字母数字转连字符）",
     {"type": "object",
      "properties": {"text": {"type": "string", "maxLength": 100}},
      "required": ["text"]},
     '''
def run(args):
    import re
    text = (args.get("text") or "").strip()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return {"slug": slug or "item"}
'''),
    ("regex_extract", "文本处理",
     "用正则表达式提取文本中的捕获组（re.findall），返回全部匹配",
     {"type": "object",
      "properties": {"text": {"type": "string", "maxLength": 10000},
                     "pattern": {"type": "string", "maxLength": 200}},
      "required": ["text", "pattern"]},
     '''
def run(args):
    import re
    text = (args.get("text") or "")[:10000]
    pattern = args.get("pattern") or ""
    try:
        matches = re.findall(pattern, text)
    except re.error as exc:
        return {"error": f"正则非法：{exc}"}
    return {"matches": [m if isinstance(m, str) else list(m) for m in matches][:200]}
'''),
    ("json_validate", "格式转换",
     "校验 JSON 字符串是否合法；合法时返回解析结果与紧凑形式，非法时返回错误位置",
     {"type": "object",
      "properties": {"text": {"type": "string", "maxLength": 20000}},
      "required": ["text"]},
     '''
def run(args):
    import json
    text = args.get("text") or ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"valid": False, "error": f"第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"}
    return {"valid": True, "compact": json.dumps(data, ensure_ascii=False,
                                                 separators=(",", ":"))}
'''),
    ("text_diff_chars", "文本处理",
     "字符级对比两段文本，返回第一处差异位置与两侧片段",
     {"type": "object",
      "properties": {"left": {"type": "string", "maxLength": 10000},
                     "right": {"type": "string", "maxLength": 10000}},
      "required": ["left", "right"]},
     '''
def run(args):
    import difflib
    left = args.get("left") or ""
    right = args.get("right") or ""
    if left == right:
        return {"equal": True}
    sm = difflib.SequenceMatcher(None, left, right, autojunk=False)
    opcodes = [op for op in sm.get_opcodes() if op[0] != "equal"]
    return {"equal": False, "diffs": [
        {"op": tag, "left": left[i1:i2][:100], "right": right[j1:j2][:100]}
        for tag, i1, i2, j1, j2 in opcodes[:50]]}
'''),
    ("char_count_classify", "文本处理",
     "按类别统计字符：汉字/字母/数字/标点/空白/其他各多少",
     {"type": "object",
      "properties": {"text": {"type": "string", "maxLength": 20000}},
      "required": ["text"]},
     '''
def run(args):
    import re
    text = args.get("text") or ""
    cn = len(re.findall(r"[\\u4e00-\\u9fa5]", text))
    letters = len(re.findall(r"[a-zA-Z]", text))
    digits = len(re.findall(r"[0-9]", text))
    spaces = len(re.findall(r"\\s", text))
    total = len(text)
    punct = total - cn - letters - digits - spaces
    return {"chinese": cn, "letters": letters, "digits": digits,
            "punctuation_or_other": punct, "whitespace": spaces, "total": total}
'''),
    ("list_dedup_sort", "数据处理",
     "列表去重并排序（去重保序可选；数字列表按数值排序，其余按字符串排序）",
     {"type": "object",
      "properties": {"items": {"type": "array", "maxItems": 1000},
                     "keep_order": {"type": "boolean"}},
      "required": ["items"]},
     '''
def run(args):
    items = (args.get("items") or [])[:1000]
    keep_order = bool(args.get("keep_order", False))
    seen = set()
    unique = []
    for it in items:
        key = json_key(it)
        if key not in seen:
            seen.add(key)
            unique.append(it)
    if not keep_order:
        try:
            unique = sorted(unique)
        except TypeError:
            unique = sorted(unique, key=lambda x: str(x))
    return {"count": len(items), "unique_count": len(unique), "items": unique}


def json_key(v):
    import json
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
'''),
    ("date_calc", "日期计算",
     "日期加减与差值：给定基准日期、加减天数（或求两日期相差天数）",
     {"type": "object",
      "properties": {"date": {"type": "string", "maxLength": 10},
                     "offset_days": {"type": "integer"},
                     "other_date": {"type": "string", "maxLength": 10}},
      "required": ["date"]},
     '''
def run(args):
    from datetime import date, timedelta

    def parse(s):
        y, m, d = (int(x) for x in s.split("-"))
        return date(y, m, d)

    base = parse(args.get("date") or "2000-01-01")
    offset = args.get("offset_days")
    result = {"date": base.isoformat()}
    if isinstance(offset, int):
        result["shifted"] = (base + timedelta(days=offset)).isoformat()
    other = args.get("other_date")
    if other:
        result["diff_days"] = (parse(other) - base).days
    return result
'''),
    ("random_sample_seed", "数据处理",
     "带种子的随机抽样（同一种子与输入永远得到同一样本，可复现）",
     {"type": "object",
      "properties": {"items": {"type": "array", "maxItems": 10000},
                     "k": {"type": "integer", "minimum": 1},
                     "seed": {"type": "integer"}},
      "required": ["items", "k"]},
     '''
def run(args):
    import random
    items = args.get("items") or []
    k = min(max(args.get("k", 1), 1), len(items)) if items else 0
    seed = args.get("seed", 0)
    rng = random.Random(seed)
    sample = rng.sample(items, k) if k else []
    return {"sample": sample, "k": k, "seed": seed}
'''),
]


class Command(BaseCommand):
    help = "预置 10 个官方纯函数工具（幂等：tool_name 存在即跳过不更新）"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="只统计不写入")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        created = skipped = 0
        for tool_name, category, description, schema, code in TOOLS:
            if ToolRegistry.objects.filter(tool_name=tool_name).exists():
                skipped += 1
                continue
            if dry_run:
                created += 1
                continue
            # author 必填（FK User）：官方种子挂到首个平台管理员，无则首个用户
            ToolRegistry.objects.create(
                tool_name=tool_name, tool_type=ToolType.OFFICIAL,
                category=category, description=description,
                input_schema=schema, handler_code=code,
                author=self._seed_author(),
                is_verified=True, is_active=False, version=1,
            )
            registry._cache.invalidate(tool_name)
            created += 1
        label = "将创建" if dry_run else "新建"
        self.stdout.write(f"官方工具种子完成：{label} {created}，跳过 {skipped}")

    def _seed_author(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        admin = User.objects.filter(is_platform_admin=True).first()
        if admin is not None:
            return admin
        user = User.objects.first()
        if user is None:
            from django.core.management.base import CommandError
            raise CommandError("无可用用户作为官方工具作者；请先创建用户")
        return user
