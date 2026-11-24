"""预置技能库种子（Phase 2.2 / 2.2.10）。

用法：
    python manage.py seed_skill_library                 # 写入 50 条全局技能模板
    python manage.py seed_skill_library --play 3        # 写入指定短剧（PLAY 作用域）
    python manage.py seed_skill_library --dry-run       # 只统计不写入

幂等：按（scope, play, name）update_or_create；已存在的技能不重置成功/失败计数。
"""
from django.core.management.base import BaseCommand, CommandError

from apps.creation.assistant.learning import confidence_of
from apps.creation.assistant.models import SkillLibrary, SkillScope, SkillType
from apps.creation.models import Play

# (name, type, triggers, steps, description)
TEMPLATES = [
    ("开场三秒钩子", SkillType.STRUCTURE, ["开场", "钩子", "第一集"], ["读取第一集剧本", "重写开场 3 句", "提交提案"],
     "用冲突或悬念在开场三秒抓住观众"),
    ("结尾反转前置伏笔", SkillType.STRUCTURE, ["反转", "结尾", "伏笔"], ["读取结尾场景", "在前文补一处细节伏笔", "提交提案"],
     "反转前必须在前文埋一处可回收的细节"),
    ("对白去书面语", SkillType.STYLE, ["对白", "台词", "书面"], ["读取目标场景", "逐句改写为口语", "提交提案"],
     "把书面表达改为符合角色身份的口语"),
    ("角色语言差异化", SkillType.STYLE, ["角色", "语言", "风格"], ["读取角色设定", "为每角色定一句口头禅", "提交提案"],
     "不同角色使用可辨识的句式与词汇"),
    ("节奏压缩三分之一", SkillType.STYLE, ["节奏", "拖沓", "冗长"], ["读取分集结构", "删减过渡性场景", "提交提案"],
     "删掉不影响主线的过渡段落"),
    ("悬念递进三段式", SkillType.STRUCTURE, ["悬念", "紧张", "钩子"], ["读取当前集", "设计三段递进", "提交提案"],
     "按信息差逐段加大压力"),
    ("误会推进冲突", SkillType.STRUCTURE, ["误会", "冲突", "矛盾"], ["读取关系线", "设计一次误会", "提交提案"],
     "用一次可澄清的误会推进关系冲突"),
    ("打脸爽点前置", SkillType.STRUCTURE, ["打脸", "爽点", "逆袭"], ["读取冲突场景", "提前暴露反派傲慢", "提交提案"],
     "先让反派嚣张，再集中打脸"),
    ("身份反转铺垫", SkillType.STRUCTURE, ["身份", "反转", "揭秘"], ["读取角色关系", "补两处身份线索", "提交提案"],
     "身份揭晓前至少两处线索"),
    ("情绪高点留白", SkillType.STYLE, ["情绪", "高潮", "哭戏"], ["读取高潮场景", "删掉解释性台词", "提交提案"],
     "情绪高点少解释，用动作与留白"),
    ("每集一钩结尾", SkillType.STRUCTURE, ["分集", "结尾", "追剧"], ["读取本集结尾", "改写最后一句", "提交提案"],
     "每集结尾留下一个未解问题"),
    ("配角功能性台词", SkillType.STYLE, ["配角", "台词", "工具人"], ["读取配角出场", "赋予一个具体动机", "提交提案"],
     "配角台词要服务自身动机"),
    ("闪回控制时长", SkillType.STYLE, ["闪回", "回忆", "时长"], ["读取含闪回场景", "压缩到一句", "提交提案"],
     "闪回不超过一句台词"),
    ("冲突升级阶梯", SkillType.STRUCTURE, ["冲突", "升级", "矛盾"], ["列出当前冲突", "设计三级升级", "提交提案"],
     "冲突按小-中-大三级升级"),
    ("反派动机合理化", SkillType.STRUCTURE, ["反派", "动机", "逻辑"], ["读取反派设定", "补一段动机", "提交提案"],
     "反派行为要有可理解动机"),
    ("信息差制造张力", SkillType.STRUCTURE, ["信息差", "张力", "误会"], ["确定谁知道什么", "安排一次错位", "提交提案"],
     "让观众与角色信息不对等"),
    ("短句加快节奏", SkillType.STYLE, ["短句", "节奏", "快"], ["读取目标对白", "拆分长句", "提交提案"],
     "关键冲突段落改用短句"),
    ("道具伏笔回收", SkillType.STRUCTURE, ["道具", "伏笔", "回收"], ["读取前文道具", "在后文回收", "提交提案"],
     "出现过的道具必须被使用"),
    ("称谓变化暗示关系", SkillType.STYLE, ["称谓", "关系", "变化"], ["读取关系转折点", "调整称谓", "提交提案"],
     "称谓变化暗示关系转变"),
    ("场景转换压缩", SkillType.STYLE, ["场景", "转换", "跳切"], ["读取转场", "用动作衔接", "提交提案"],
     "用动作而非字幕转场"),
    ("冷开场直接冲突", SkillType.STRUCTURE, ["冷开场", "开场", "冲突"], ["读取第一场", "直接从冲突切入", "提交提案"],
     "开场不做背景铺垫"),
    ("台词藏潜台词", SkillType.STYLE, ["潜台词", "台词", "含蓄"], ["读取关键对白", "改写为言外之意", "提交提案"],
     "把直说改为暗示"),
    ("三幕结构校验", SkillType.STRUCTURE, ["结构", "三幕", "分集"], ["标注三幕边界", "检查转折点", "提交提案"],
     "确认建置/对抗/解决三段完整"),
    ("人物弧光一致性", SkillType.STRUCTURE, ["人物", "弧光", "成长"], ["列出角色变化", "检查每集递进", "提交提案"],
     "角色变化需逐集可追溯"),
    ("情绪节奏交替", SkillType.STYLE, ["情绪", "节奏", "松紧"], ["标注情绪曲线", "交替松紧", "提交提案"],
     "紧张段落后安排一个缓冲"),
    ("台词信息密度", SkillType.STYLE, ["台词", "信息", "密度"], ["逐句删冗余", "保留推动剧情信息", "提交提案"],
     "每句台词至少承担一个功能"),
    ("反派登场威慑", SkillType.STRUCTURE, ["反派", "登场", "威慑"], ["设计登场动作", "展示实力", "提交提案"],
     "反派首次登场要有具体威慑动作"),
    ("误会解开时机", SkillType.STRUCTURE, ["误会", "解开", "真相"], ["确定解开时机", "安排揭开方式", "提交提案"],
     "误会要在大冲突前解开"),
    ("配角支线收束", SkillType.STRUCTURE, ["支线", "配角", "收束"], ["列出支线", "逐条收束", "提交提案"],
     "支线在结局前全部收束"),
    ("情绪锚点重复", SkillType.STYLE, ["情绪", "锚点", "重复"], ["选一个意象", "重复出现三次", "提交提案"],
     "用一个意象反复强化情绪"),
    ("旁白使用克制", SkillType.STYLE, ["旁白", "解说", "克制"], ["找出旁白", "改为画面表达", "提交提案"],
     "能用画面表达就不用旁白"),
    ("快节奏对话交锋", SkillType.STYLE, ["对话", "交锋", "快节奏"], ["缩短双方台词", "增加打断", "提交提案"],
     "冲突对话用短促打断"),
    ("时间线一致性检查", SkillType.STRUCTURE, ["时间线", "逻辑", "一致性"], ["梳理时间点", "检查矛盾", "提交提案"],
     "确认时间推进无矛盾"),
    ("爽点密度控制", SkillType.STRUCTURE, ["爽点", "密度", "节奏"], ["标注爽点位置", "调整间隔", "提交提案"],
     "每两集至少一个爽点"),
    ("角色动机链条", SkillType.STRUCTURE, ["动机", "角色", "行为"], ["列出关键行为", "补动机", "提交提案"],
     "每个关键行为都有动机支撑"),
    ("口语化称谓", SkillType.STYLE, ["称谓", "口语", "称呼"], ["读取对白", "统一称谓", "提交提案"],
     "称谓符合人物关系与场合"),
    ("冲突场景留出口", SkillType.STRUCTURE, ["冲突", "出口", "悬念"], ["设计未解决问题", "收尾留扣", "提交提案"],
     "冲突解决后留一个新问题"),
    ("情绪转折点标记", SkillType.STYLE, ["情绪", "转折", "节点"], ["标注转折点", "强化转折", "提交提案"],
     "情绪转折处加一个动作"),
    ("台词去重复", SkillType.STYLE, ["重复", "台词", "冗余"], ["扫描重复表达", "合并删减", "提交提案"],
     "删除重复表达"),
    ("悬念伏笔密度", SkillType.STRUCTURE, ["悬念", "伏笔", "密度"], ["标注伏笔", "补一处伏笔", "提交提案"],
     "每集埋一处可回收伏笔"),
    ("角色关系图梳理", SkillType.TOOL_SEQUENCE, ["关系", "角色", "梳理"],
     ["调用 get_characters", "整理关系", "提交提案"], "先梳理关系再改剧情"),
    ("读剧本后改场景", SkillType.TOOL_SEQUENCE, ["场景", "修改", "读取"],
     ["调用 get_episode_script", "调用 get_scene", "提交提案"], "改动场景前必须先读原场景"),
    ("提案前读原值", SkillType.TOOL_SEQUENCE, ["提案", "修改", "快照"],
     ["调用对应 get_* 工具", "对照原值", "提交提案"], "提案必须基于最新原值"),
    ("失败后换工具重试", SkillType.RECOVERY, ["失败", "报错", "重试"],
     ["读取错误", "改用等价只读工具", "汇报结论"], "工具失败先换等价路径"),
    ("成本超限提前收尾", SkillType.RECOVERY, ["预算", "成本", "超限"],
     ["检查成本", "停止新增调用", "汇报部分结果"], "预算耗尽时保留部分结果"),
    ("审查后逐条修订", SkillType.TOOL_SEQUENCE, ["审查", "修订", "建议"],
     ["调用 review_episode", "逐条对照", "提交提案"], "审查结果逐条落地"),
    ("多方案对比选择", SkillType.STRUCTURE, ["方案", "对比", "选择"],
     ["生成两到三个方案", "对比差异", "提交最优"], "创意任务先出多方案再选"),
    ("角色一致性检查", SkillType.STRUCTURE, ["一致性", "角色", "设定"],
     ["调用 get_characters", "对照剧本", "提交修正提案"], "改动前核对角色设定"),
    ("记忆写入前合并", SkillType.TOOL_SEQUENCE, ["记忆", "偏好", "约定"],
     ["调用 memory_read", "合并已有内容", "调用 memory_save"], "写记忆前先读并合并"),
    ("场景氛围一句话", SkillType.STYLE, ["氛围", "场景", "画面"],
     ["读取场景描述", "用一句话定氛围", "提交提案"], "场景开头用一句话定调"),
]

assert len(TEMPLATES) == 50, len(TEMPLATES)


class Command(BaseCommand):
    help = "预置技能库种子（50 条短剧创作技能模板）"

    def add_arguments(self, parser):
        parser.add_argument("--play", type=int, help="写入指定短剧（PLAY 作用域）；缺省为全局")
        parser.add_argument("--dry-run", action="store_true", help="只统计不写入")

    def handle(self, *args, **options):
        play = None
        if options.get("play"):
            play = Play.objects.filter(pk=options["play"]).first()
            if play is None:
                raise CommandError(f"短剧 {options['play']} 不存在")
        scope = SkillScope.PLAY if play else SkillScope.GLOBAL
        created = updated = 0
        if options.get("dry_run"):
            existing = set(SkillLibrary.objects.filter(scope=scope, play=play).values_list("name", flat=True))
            created = len([t for t in TEMPLATES if t[0] not in existing])
            updated = len(TEMPLATES) - created
            self.stdout.write(f"dry-run: 将新建 {created} 条，已存在 {updated} 条")
            return
        for name, skill_type, triggers, steps, description in TEMPLATES:
            _, was_created = SkillLibrary.objects.update_or_create(
                scope=scope, play=play, name=name,
                defaults={"skill_type": skill_type, "trigger_conditions": triggers,
                          "execution_steps": steps, "description": description,
                          "confidence": confidence_of(0, 0)})
            created += int(was_created)
            updated += int(not was_created)
        self.stdout.write(self.style.SUCCESS(
            f"技能库种子完成（{scope}{' 短剧#' + str(play.pk) if play else ''}）：新建 {created}，已存在 {updated}"))
