"""LLM 文本输出解析助手：各 LLM 调用点共用。

- extract_json(text)：括号配平扫描提取首个 JSON 对象/数组
  （跳过字符串字面量内的括号；某起点配平失败后从下一位置重试；
  容忍 ```json 包裹与前后说明文字/示例代码块）
- extract_json_object / extract_json_array：带类型校验的便捷封装
- chat_text(AIGateway resp)：content_blocks → 拼接文本
"""

from __future__ import annotations

import json
from typing import Optional


def find_balanced_json(text: str, start: int) -> Optional[str]:
    """从 start 处（'{' 或 '['）取到配对的闭合括号，返回子串；不配对返回 None。

    用栈扫描并跳过字符串字面量与转义，避免非贪婪匹配只取到内层第一个对象。
    （自 apps.creation.tasks 抽出共用，phase1 修复 6.1。）
    """
    stack = []
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                return None
            open_ch = stack.pop()
            if (open_ch == "{" and ch != "}") or (open_ch == "[" and ch != "]"):
                return None
            if not stack:
                return text[start:i + 1]
    return None


def extract_json(text: Optional[str]):
    """提取首个 JSON 值；无匹配或解析失败返回 None。

    按位置升序尝试各顶层括号起点，取配对括号的最大跨度（兼容 ```json 围栏
    与前后说明文字/示例代码块）。升序保证最外层的值先被尝试，避免数组里的
    内层对象被误当作整个文档。某起点配平失败（如字符串里出现孤立括号导致
    误判起点）会从其后的下一个括号位置重试（phase1 修复 6.1：旧正则
    `\\{.*\\}|\\[.*\\]` 贪心跨到文末、且字符串字面量内的括号会截断/放大跨度）。
    """
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    positions = []
    for i, ch in enumerate(text):
        if ch in "{[":
            positions.append(i)
    for start in positions:
        span = find_balanced_json(text, start)
        if span is None:
            continue
        try:
            return json.loads(span)
        except ValueError:
            continue
    return None


def extract_json_object(text: Optional[str]) -> dict:
    data = extract_json(text)
    return data if isinstance(data, dict) else {}


def extract_json_array(text: Optional[str]) -> list:
    data = extract_json(text)
    return data if isinstance(data, list) else []


def chat_text(resp: dict) -> str:
    """AIGateway chat 响应 → 拼接的纯文本（取 content_blocks 中 type==text 块）。"""
    return "".join(b.get("text", "") for b in (resp.get("content_blocks") or [])
                   if b.get("type") == "text")
