# -*- coding: utf-8 -*-
"""
LLM Prompt 与输出解析工具（协议第 5 节、规则 §8.1）。

【合规】随机事件必须交由 LLM 理解，禁止正则/关键词/固定句式解析事件文本。
本模块的正则仅用于「解析 LLM 的 JSON 输出」，不用于解析事件公告原文。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..models import Intent

# 中文方向 → 协议方向（用于 LLM 输出的方向归一化）
_ZH_DIRECTION = {
    "上": "up", "向上": "up", "下": "down", "向下": "down",
    "左": "left", "向左": "left", "右": "right", "向右": "right",
}

_FENCE_RE = re.compile(r"```[ \t]*(?:json)?[ \t]*\r?\n?(.*?)```", re.S | re.I)
_INVISIBLE = dict.fromkeys(map(ord, "\ufeff\u200b\u200c\u200d"), None)

# 事件解析系统提示词（赛前冻结，不得现场修改）
SYSTEM_PROMPT = (
    "你是 RMTC 机甲对战机器人的事件解析器。裁判会给你一段中文事件公告，"
    "你要判断它此刻最合适的立即动作。\n"
    "输出要求：只输出一个 json 对象，不要 Markdown 代码块，不要任何解释文字。\n"
    "字段（仅这三个）：\n"
    '- kind: "move" | "attack" | "defend" | "wait"\n'
    '- direction: "up" | "down" | "left" | "right"（move/attack 必填；'
    "move 表示下一步移动方向，attack 表示目标所在方向；up 为 y 减小）\n"
    "- reason: 8 字以内中文短语\n"
    "判定规则：\n"
    "1) 要求撤离/躲避/远离某处 → move，方向为远离该威胁的方向；\n"
    "2) 给出敌人方位或要求集火 → attack，方向为敌人所在方向；\n"
    "3) 要求防守/固守/回防 → defend；\n"
    "4) 只是通知、无明确战术指令 → wait。\n"
    "示例：\n"
    '输入「陨石将落在你的左侧，请向右躲避」→ {"kind":"move","direction":"right","reason":"躲避陨石"}\n'
    '输入「右侧出现敌方机甲，集中火力」→ {"kind":"attack","direction":"right","reason":"集火右侧"}\n'
    '输入「堡垒已被占领，回防」→ {"kind":"defend","reason":"回防"}\n'
)


def extract_json_text(content: str) -> str:
    """从模型回复中抽取 JSON 候选文本。

    兼容三类常见输出：
      1) 纯 JSON；
      2) ```json ... ``` / ```JSON ... ``` 围栏（围栏前后可带解释文字）；
      3) 解释文字包裹 JSON（截取首个 '{' 到最后一个 '}'）。
    """
    text = content.translate(_INVISIBLE).strip()
    m = _FENCE_RE.search(text)
    if m is not None:
        text = m.group(1).strip()
    else:
        i, j = text.find("{"), text.rfind("}")
        if i != -1 and j > i:
            text = text[i:j + 1]
        elif text.startswith("`"):
            text = text.strip("`").strip()
    return text.strip()


def repair_json(text: str) -> str:
    """对模型 JSON 的常见瑕疵做轻量修复（尽力而为，失败仍走降级）。

    - 全角/中文引号 → 半角；
    - 尾随逗号（`,` 紧跟 `}` / `]`）；
    - 单引号包裹的键与值；
    - 未加引号且非数字的裸词值（如 "kind": move）。
    """
    t = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    t = re.sub(r",\s*([}\]])", r"\1", t)
    t = re.sub(r"'([^'\"{}:,]*?)'\s*:", r'"\1":', t)
    t = re.sub(r":\s*'([^']*)'", r': "\1"', t)
    t = re.sub(r":\s*([A-Za-z_][A-Za-z0-9_]*)\s*([,}])", r': "\1"\2', t)
    return t


def parse_response(content: Optional[str]) -> Optional[Intent]:
    """从回复提取 JSON 并构造 Intent；解析失败返回 None。

    流程：先抽取（兼容 Markdown 围栏/前后解释文字）→ 原样解析 →
    失败则轻量修复后再解析（全角引号、尾随逗号、单引号、裸词值）。
    """
    if not content:
        return None
    text = extract_json_text(content)
    if not text:
        return None

    obj: Any = None
    for candidate in (text, repair_json(text)):
        try:
            obj = json.loads(candidate)
            break
        except (json.JSONDecodeError, ValueError):
            continue
    if not isinstance(obj, dict):
        return None

    kind = str(obj.get("kind") or obj.get("action") or "").strip().lower()
    if kind not in ("move", "attack", "defend", "wait"):
        return None
    raw_dir = obj.get("direction")
    if raw_dir is None:
        direction = None
    else:
        direction = str(raw_dir).strip().lower()
        direction = _ZH_DIRECTION.get(direction, direction)
    if kind in ("move", "attack") and direction not in ("up", "down", "left", "right"):
        return None
    if kind in ("defend", "wait"):
        direction = None
    return Intent(
        kind=kind,
        direction=direction,
        reason=str(obj.get("reason", "llm-event"))[:32],
    )
