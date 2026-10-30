# -*- coding: utf-8 -*-
"""
决策状态机（规则基）。

状态：开局定位 → 采集 → 对抗/任务 → 事件响应（临时叠加态）→ 等待（阵亡/复活）。
- 纯本地、确定性；LLM 只通过 ResultInbox 注入「事件理解 / 战术建议」，
  绝不进入逐 Tick 热路径。
- 场次切换（session_id 变化）→ 清空全部缓存并重新定位。
- 同 Tick 重发不重复推进（由 main 通过 WorldModel.is_new_frame 保证）。
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

from ..models import Intent, Perception
from .registry import DecisionContext, dispatch
from .fortress_policy import decide_fortress

log = logging.getLogger("rmtc.fsm")


class State(str, Enum):
    INIT_LOCATE = "init_locate"   # 开局定位
    COLLECT = "collect"           # 采集（两阶段均适用，阶段一主战场）
    COMBAT = "combat"             # 对抗 / 任务（阶段二）
    FORT = "fortress"             # 堡垒行为（阶段二叠加态，规则 §8.5）
    EVENT = "event"               # 事件响应（临时叠加态）
    WAIT = "wait"                 # 阵亡 / 复活 / 强制等待


class DecisionFSM:
    """规则基决策状态机。"""

    def __init__(self, cfg, ctx: DecisionContext) -> None:
        self.cfg = cfg
        self.ctx = ctx
        self.state: State = State.INIT_LOCATE
        self._last_tick: int = -1
        self._last_session: Optional[str] = None

    # ------------------------------------------------------------------
    def step(self, p: Perception) -> Intent:
        """用一帧（新）感知推进一步，返回动作意图。"""
        # ---- 场次切换：清空缓存 ----
        if p.session_id != self._last_session:
            log.info("场次切换 %s -> %s，重置状态机", self._last_session, p.session_id)
            self._last_session = p.session_id
            self.state = State.INIT_LOCATE

        self._last_tick = p.tick
        me = p.me

        # ---- 1) 阵亡 / 复活等待 → 只 WAIT（阵亡时 WAIT 合法，不增加无效动作） ----
        if me is None:
            return Intent(kind="wait", reason="no-self")
        if (not me.alive) or me.respawn_in > 0:
            self.state = State.WAIT
            return Intent(kind="wait", reason=f"dead/respawn_in={me.respawn_in}")

        # ---- 2) 开局 / 重生定位 ----
        if me.position_reason in ("initial", "respawn"):
            log.info("定位：pos=%s reason=%s tick=%s",
                     me.position, me.position_reason, p.tick)
            self.state = State.INIT_LOCATE

        # ---- 3) 事件响应优先（消费 LLM 结构化结果） ----
        ev_intent = self.ctx.event_policy.step(p)
        if ev_intent is not None:
            self.state = State.EVENT
            return ev_intent
        if self.state == State.EVENT:
            # 事件窗口结束，回到常规状态
            self.state = State.COLLECT if self.ctx.stage(p) == 1 else State.COMBAT

        # ---- 3.5) 堡垒叠加（规则 §8.5；仅阶段二适用）----
        # 优先级：事件 > 堡垒（低血回撤/驻留回血/敌占堡垒）> 常规对抗/任务。
        if self.ctx.stage(p) == 2:
            fort_intent = decide_fortress(p, self.ctx)
            if fort_intent is not None:
                self.state = State.FORT
                return fort_intent

        # ---- 4) 常规策略分派（按已登记 decision_type） ----
        intent = dispatch(self.cfg.decision_type, p, self.ctx)
        self.state = State.COLLECT if self.ctx.stage(p) == 1 else State.COMBAT
        return intent
