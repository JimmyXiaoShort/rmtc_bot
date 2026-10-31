# -*- coding: utf-8 -*-
"""
动作构造器：Intent → /team_X/action JSON 文本。

职责（协议第 4 节）：
- 动作 JSON 必须小写：{"tick": <int>, "session_id": <str>, "action": "move|attack|defend|wait"}；
- move / attack 必须携带 direction ∈ {up, down, left, right}；defend / wait 不携带；
- tick 与 session_id 必须原样回填所依据感知的值（缺失/不符即非法请求）；
- 每 Tick 最多执行一个动作（主循环保证单发）。

校验逻辑（阶段白名单、direction 合法性）在 guard.py 中，本模块只负责构造。
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Optional

from ..models import Intent, Perception
from .guard import validate_intent

log = logging.getLogger("rmtc.action.builder")


class ActionBuilder:
    """把决策意图翻译为协议要求的 action JSON 文本。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    # ------------------------------------------------------------------
    def build(self, intent: Intent, p: Perception) -> Optional[str]:
        """返回待发送的 action JSON 文本；不合法返回 None（不发送）。"""
        if not validate_intent(intent, p):
            return None

        action = intent.kind
        msg: Dict = {
            "tick": p.tick,
            "session_id": p.session_id,
            "action": action,
        }
        if action in ("move", "attack"):
            msg["direction"] = intent.direction
        return json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
