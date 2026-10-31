# -*- coding: utf-8 -*-
"""
动作构造器：Intent → /team_X/action JSON 文本。

职责（协议第 4 节）：
- 动作 JSON 必须小写：{"tick": <int>, "session_id": <str>, "action": "move|attack|defend|wait"}；
- move / attack 必须携带 direction ∈ {up, down, left, right}；defend / wait 不携带；
- tick 与 session_id 必须原样回填所依据感知的值（缺失/不符即非法请求）；
- 阶段动作白名单：第一阶段仅 MOVE/WAIT，第二阶段 MOVE/ATTACK/DEFEND/WAIT；
- 每 Tick 最多执行一个动作（主循环保证单发）。
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Optional

from .. import constants as C
from ..models import Intent, Perception

# 阶段动作白名单（常量集中定义，规则 §6、§9、§10）
ALLOWED_PHASE1 = C.ALLOWED_PHASE1
ALLOWED_PHASE2 = C.ALLOWED_PHASE2

log = logging.getLogger("rmtc.action")

# 方向常量（协议第 4 节；动作字段必须小写）
DIRECTIONS = ("up", "down", "left", "right")


class ActionBuilder:
    """把决策意图翻译为协议要求的 action JSON 文本。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    # ------------------------------------------------------------------
    def build(self, intent: Intent, p: Perception) -> Optional[str]:
        """返回待发送的 action JSON 文本；不合法返回 None（不发送）。"""
        action = intent.kind
        if action not in ("move", "attack", "defend", "wait"):
            log.error("未知动作意图 %s", action)
            return None

        # 阶段白名单
        allowed = ALLOWED_PHASE2 if p.stage == 2 else ALLOWED_PHASE1
        if action not in allowed:
            log.warning("[无效动作防护] 阶段%d 禁止 %s，丢弃", p.stage, action)
            return None

        msg: Dict = {
            "tick": p.tick,
            "session_id": p.session_id,
            "action": action,
        }
        if action in ("move", "attack"):
            direction = intent.direction
            if direction not in DIRECTIONS:
                log.warning("[无效动作防护] %s 缺少合法 direction(%r)，丢弃", action, direction)
                return None
            msg["direction"] = direction
        return json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
