# -*- coding: utf-8 -*-
"""
动作校验（协议第 4 节、规则 §6）。

职责：
- 阶段动作白名单：第一阶段仅 MOVE/WAIT，第二阶段 MOVE/ATTACK/DEFEND/WAIT；
- direction 合法性校验：move/attack 必须携带 up/down/left/right；
- 动作类型白名单：仅 move/attack/defend/wait。

不合法时返回 False，由调用方决定丢弃或降级（不发送非法动作）。
"""

from __future__ import annotations

import logging
from typing import Optional

from .. import constants as C
from ..models import Intent, Perception

log = logging.getLogger("rmtc.action.guard")

# 方向常量（协议第 4 节；动作字段必须小写）
DIRECTIONS = ("up", "down", "left", "right")

# 阶段动作白名单（常量集中定义，规则 §6、§9、§10）
ALLOWED_PHASE1 = C.ALLOWED_PHASE1
ALLOWED_PHASE2 = C.ALLOWED_PHASE2


def validate_intent(intent: Intent, p: Perception) -> bool:
    """校验动作意图是否合法；不合法返回 False（调用方应丢弃）。"""
    action = intent.kind

    # 动作类型白名单
    if action not in ("move", "attack", "defend", "wait"):
        log.error("未知动作意图 %s", action)
        return False

    # 阶段白名单
    allowed = ALLOWED_PHASE2 if p.stage == 2 else ALLOWED_PHASE1
    if action not in allowed:
        log.warning("[无效动作防护] 阶段%d 禁止 %s，丢弃", p.stage, action)
        return False

    # move/attack 必须携带合法 direction
    if action in ("move", "attack"):
        direction = intent.direction
        if direction not in DIRECTIONS:
            log.warning("[无效动作防护] %s 缺少合法 direction(%r)，丢弃", action, direction)
            return False

    return True
