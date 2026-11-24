"""剧本专业排版（script-layout，2026-11-23 组 3）。

Fountain 五要素渲染（场次标题/角色/对话/动作指示/转场）+ 三格式导出
（fountain/txt/pdf）。只读渲染：数据源为既有 Episode/Scene/Shot（当前版）或
ScriptVersion 快照（历史版），不引入编辑态（内容修改仍走 redo/提案链路）。

Fountain 语法要点（https://fountain.io/syntax）：
- 场景标题（Scene Heading）：INT./EXT. 前缀大写行
- 角色（Character）：大写行后跟对话
- 对话（Dialogue）：角色行下一行
- 动作（Action）：普通行
- 转场（Transition）：大写 + TO 结尾（如 CUT TO:）
"""
import io
import logging

logger = logging.getLogger(__name__)


def _time_of_day_label(value):
    mapping = {"DAY": "日", "NIGHT": "夜", "DAWN": "黎明", "DUSK": "黄昏", "MORNING": "清晨", "AFTERNOON": "午后"}
    return mapping.get(value, value or "")


def render_fountain(episode_title, scenes):
    """五要素 → Fountain 文本。scenes 为 Scene 查询集或快照 dict 列表。

    dict 快照（ScriptVersion.content.scenes）：{场景号: {location, time_of_day,
    description, mood, shots: [...]}}——按场景号排序遍历。
    """
    lines = [f"Title: {episode_title}", ""]
    for scene in scenes:
        if isinstance(scene, dict):
            number = scene.get("scene_number")
            location = scene.get("location", "")
            tod = _time_of_day_label(scene.get("time_of_day"))
            desc = scene.get("description", "")
            shots = scene.get("shots") or []
        else:
            number = scene.scene_number
            location = scene.location
            tod = _time_of_day_label(scene.time_of_day)
            desc = scene.description
            shots = list(scene.shots.order_by("shot_number"))
        prefix = "INT." if tod == "内" else "EXT."
        heading = f"{prefix} {location or '未知地点'} - {tod}.".upper()
        lines.append(f"{heading} 场景 {number}")
        lines.append("")
        if desc:
            lines.append(desc)
            lines.append("")
        for shot in shots:
            if isinstance(shot, dict):
                action = shot.get("action_note") or shot.get("description", "")
                dialogue = shot.get("dialogue", "")
                character = shot.get("dialogue_character", "")
            else:
                action = shot.action_note or shot.visual_prompt
                dialogue = shot.dialogue
                character = (shot.dialogue_character.name
                             if getattr(shot, "dialogue_character", None) else "")
            if action:
                lines.append(action)
                lines.append("")
            if dialogue:
                if character:
                    lines.append(character.upper())
                else:
                    lines.append("旁白")
                lines.append(dialogue)
                lines.append("")
        lines.append("CUT TO:")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_plain_text(episode_title, scenes):
    """txt 导出：中文可读分镜式排版（无 Fountain 语法约束）。"""
    lines = [f"《{episode_title}》", ""]
    for scene in scenes:
        if isinstance(scene, dict):
            number = scene.get("scene_number")
            location = scene.get("location", "")
            tod = _time_of_day_label(scene.get("time_of_day"))
            desc = scene.get("description", "")
            shots = scene.get("shots") or []
        else:
            number = scene.scene_number
            location = scene.location
            tod = _time_of_day_label(scene.time_of_day)
            desc = scene.description
            shots = list(scene.shots.order_by("shot_number"))
        lines.append(f"【场景 {number}】{location}（{tod}）")
        if desc:
            lines.append(desc)
        for i, shot in enumerate(shots, start=1):
            if isinstance(shot, dict):
                action = shot.get("action_note") or shot.get("description", "")
                dialogue = shot.get("dialogue", "")
                character = shot.get("dialogue_character", "")
            else:
                action = shot.action_note or shot.visual_prompt
                dialogue = shot.dialogue
                character = (shot.dialogue_character.name
                             if getattr(shot, "dialogue_character", None) else "")
            lines.append(f"  镜头{i}：{action}".rstrip())
            if dialogue:
                lines.append(f"    {character or '旁白'}：{dialogue}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_pdf(episode_title, fountain_text):
    """fpdf2 渲染 PDF。中文字体：优先系统字体，缺失时回退内置（拉丁）
    并记录警告——本地优先形态不捆绑字体文件（打包体积决策）。"""
    from fpdf import FPDF

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    font_loaded = _load_cjk_font(pdf)
    pdf.set_font("cjk" if font_loaded else "helvetica", size=12)
    pdf.multi_cell(0, 7, fountain_text)
    return bytes(pdf.output())


def _load_cjk_font(pdf):
    """按平台探测一个可用的中文 TTF；找不到返回 False（回退拉丁字体）。"""
    import sys
    from pathlib import Path

    candidates = []
    if sys.platform == "win32":
        windir = Path("C:/Windows/Fonts")
        candidates = [windir / "msyh.ttc", windir / "simsun.ttc", windir / "simhei.ttf"]
    else:
        candidates = [Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
                      Path("/System/Library/Fonts/PingFang.ttc")]
    for font in candidates:
        if font.is_file():
            try:
                pdf.add_font("cjk", style="", fname=str(font))
                return True
            except Exception:  # noqa: BLE001 — 字体损坏等，回退
                logger.warning("CJK 字体 %s 加载失败，PDF 回退拉丁字体", font)
                return False
    logger.warning("未找到可用中文字体，PDF 将以拉丁字体渲染（中文乱码风险）")
    return False


def snapshot_scenes(episode):
    """当前版结构化快照（与 ScriptVersion.content.scenes 同形）。"""
    out = {}
    for scene in episode.scenes.order_by("scene_number"):
        shots = []
        for shot in scene.shots.order_by("shot_number"):
            shots.append({
                "shot_number": shot.shot_number,
                "description": shot.visual_prompt,
                "dialogue": shot.dialogue,
                "action_note": shot.action_note,
                "dialogue_character": (shot.dialogue_character.name
                                       if shot.dialogue_character_id else ""),
            })
        out[str(scene.scene_number)] = {
            "location": scene.location,
            "time_of_day": scene.time_of_day,
            "description": scene.description,
            "mood": scene.mood,
            "shots": shots,
        }
    return out


def scenes_for_export(episode, script_version=None):
    """导出数据源：script_version 给定走快照（历史版），否则当前结构。"""
    if script_version is not None:
        content = script_version.content or {}
        raw = content.get("scenes") or {}
        scenes = []
        for number in sorted(raw.keys(), key=lambda x: int(x)):
            data = dict(raw[number])
            data["scene_number"] = int(number)
            scenes.append(data)
        return scenes
    return list(episode.scenes.order_by("scene_number"))
