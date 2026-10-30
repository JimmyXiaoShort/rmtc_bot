# -*- coding: utf-8 -*-
"""
随机事件 / 任务处理器（自然语言事件，协议第 7 节 / 3.1 任务类型）。

职责：
- 监听 /referee/event_text 与 /referee/mission（均为纯文本公告，协议第 2 节）；
- 事件文本交给 LLM 网关解析（openai 兼容），得到结构化动作建议；
- 纯任务提示（reach 型任务由感知 missions 结构化给出坐标）不进 LLM（省配额）；
- 事件窗口内（有建议且未消费）由 FSM 优先叠加执行。
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Optional

from ..models import Intent, Perception
from ..perception.world_model import WorldModel
from ..decision.registry import DecisionContext  # noqa: F401  (类型标注)

log = logging.getLogger("rmtc.event")

# 纯规则可识别事件（无需 LLM）：任务提示
_TASK_KEYWORDS = ("任务", "mission", "目标", "坐标", "计分")
_ACTION_KEYWORDS = ("攻击", "撤离", "防守", "defend", "attack", "flee", "move", "wait")


@dataclass
class EventState:
    """事件解析结果：LLM 建议 + 有效窗口。"""

    tick_seen: int = -1          # 看到事件文本的 tick
    text: str = ""               # 原始事件文本
    intent: Optional[Intent] = None   # 结构化建议
    expire_at: int = -1          # 窗口到期 tick（含）
    consumed: bool = False

    def valid(self, tick: int) -> bool:
        return (not self.consumed) and self.intent is not None and tick <= self.expire_at


class EventPolicy:
    """事件策略：任务提示规则分支 + LLM 事件解析 + 窗口管理。"""

    def __init__(self, wm: WorldModel, llm_gateway, cfg) -> None:
        self.wm = wm
        self.llm = llm_gateway
        self.cfg = cfg
        self._ev: Optional[EventState] = None
        self._lock = threading.Lock()      # 后台解析线程与主循环共享 _ev
        self._last_mission_text = ""       # 任务公告去重（裁判每 Tick 重发同一文本）

    # ------------------------------------------------------------------
    def on_mission_text(self, text: str, tick: int) -> None:
        """处理 /referee/mission（纯文本任务公告）。

        任务坐标的权威来源是感知中的 missions 字段（结构化），本方法只做
        规则提示记录；涉及指令性语义（如"前往 (x,y)"）由 LLM 分支处理。
        裁判会每 Tick 重发同一任务文本，故按内容去重，避免重复消耗 LLM 配额。
        """
        t = text.strip()
        if not t or t == self._last_mission_text:
            return
        self._last_mission_text = t
        log.info("任务公告[tick=%s]: %s", tick, t)
        if any(k in t for k in _TASK_KEYWORDS):
            self._llm_parse_async(t, tick)   # 让 LLM 提取可执行意图（到达/守卫等）
        else:
            log.info("任务公告无可执行意图，仅记录")

    def on_event_text(self, text: str, tick: int) -> None:
        """处理 /referee/event_text（纯文本随机事件公告）。"""
        t = text.strip()
        if not t:
            return
        # 按内容判重（world.event_log 为本场已见公告集合）：
        # 裁判重发/启动重放的历史公告不再重复调用 LLM，节省配额。
        if not self.wm.record_event_text(t):
            return
        log.info("事件公告[tick=%s]: %s", tick, t)
        # 事件文本一般含指令语义 → 交给 LLM 解析（有配额与降级保护）
        self._llm_parse_async(t, tick)

    def _llm_parse_async(self, text: str, tick: int) -> None:
        """在后台线程调用 LLM：等待模型期间不阻塞感知处理与逐 Tick 动作。"""
        threading.Thread(
            target=self._llm_parse, args=(text, tick),
            name="rmtc-llm-parse", daemon=True,
        ).start()

    def _llm_parse(self, text: str, tick: int) -> None:
        """调 LLM 网关解析文本，写入 EventState。失败则保守忽略。"""
        intent = self.llm.parse_event(text)
        if intent is None:
            log.warning("LLM 事件解析失败/降级，本事件忽略")
            return
        # 窗口：本 tick 起 3 个动作周期内有效（协议未定义窗口，取保守值）
        with self._lock:
            self._ev = EventState(
                tick_seen=tick, text=text, intent=intent, expire_at=tick + 3,
            )
        log.info("事件建议已入队：kind=%s direction=%s", intent.kind, intent.direction)

    # ------------------------------------------------------------------
    def step(self, p: Perception) -> Optional[Intent]:
        """FSM 每帧调用：事件窗口内返回建议；否则 None。"""
        with self._lock:
            ev = self._ev
            if ev is None:
                return None
            if not ev.valid(p.tick):
                if ev.consumed:
                    self._ev = None
                return None
            # 消费（单发）
            intent, ev.consumed = ev.intent, True
        if intent.kind not in ("move", "attack", "defend", "wait"):
            log.warning("事件建议动作非法，忽略: %s", intent.kind)
            return None
        return intent

    def reset(self) -> None:
        with self._lock:
            self._ev = None
        self._last_mission_text = ""
