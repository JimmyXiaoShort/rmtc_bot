# -*- coding: utf-8 -*-
"""
决策类型登记与分派（规则 §2.3「代码封存与重启」）。

现场只允许修改 decision_config.json 的 decision_type 字段，
其取值只能来自「本队已登记的枚举」。本模块即登记内容的代码实现：
每个 decision_type 标识 → 一个可封存的策略入口（stage / 作用 / 说明）。

【合规】现场切换只能落到下列枚举之一，不得修改源码、Prompt、算法参数，
也不得借 decision_type 逐 Tick 人工控制。
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

from ..config import DECISION_TYPE_REGISTRY
from ..models import Intent, Perception
from .collector import decide_collect
from .combat import decide_combat

log = logging.getLogger("rmtc.registry")


def infer_stage(p: Perception) -> int:
    """
    阶段推断（不依赖缓存）。

    优先使用 parser 已推断的 p.stage（按 tick / 视野内机器人）；
    兜底：第二阶段动作 Tick 为 0–499，第一阶段 0–99；tick ≥ 100 时必然是第二阶段。
    注意：地图尺寸不能区分阶段（本站 stage1/stage2 地图都是 24×24），不得用于判阶段。
    """
    if p.stage in (1, 2):
        return p.stage
    if p.tick >= 100:
        return 2
    if p.robots:
        return 2
    return 1


def _strategy_collect(p: Perception, ctx: "DecisionContext") -> Intent:
    """decision_type=collect：强制第一阶段采集策略。"""
    return decide_collect(p, ctx)


def _strategy_combat(p: Perception, ctx: "DecisionContext") -> Intent:
    """decision_type=combat：第一阶段先采集（无对手可见），第二阶段强制对抗。"""
    if infer_stage(p) == 1:
        return decide_collect(p, ctx)
    return decide_combat(p, ctx)


def _strategy_auto(p: Perception, ctx: "DecisionContext") -> Intent:
    """decision_type=auto：按 tick / 视野自动判定阶段。"""
    stage = infer_stage(p)
    if stage == 1:
        return decide_collect(p, ctx)
    return decide_combat(p, ctx)


def _strategy_safe(p: Perception, ctx: "DecisionContext") -> Intent:
    """
    decision_type=safe：保守/降级模式。

    第一阶段无对手可见 → 就近采集；第二阶段不主动交战：
    有视野内敌人则优先脱离，残血时优先撤离（仍受动作合法性与阶段白名单约束）。
    """
    # 延迟导入避免循环依赖
    from .combat import decide_combat

    if infer_stage(p) == 1:
        return decide_collect(p, ctx)
    return decide_combat(p, ctx, safe_mode=True)


# decision_type → 策略入口（与 config.DECISION_TYPE_REGISTRY 一一对应）
STRATEGY_TABLE: Dict[str, Callable[[Perception, "DecisionContext"], Intent]] = {
    "collect": _strategy_collect,
    "combat": _strategy_combat,
    "auto": _strategy_auto,
    "safe": _strategy_safe,
}


def registered_types() -> Dict[str, dict]:
    """返回登记表（供日志/答辩展示）。"""
    return DECISION_TYPE_REGISTRY


def dispatch(decision_type: str, p: Perception, ctx: "DecisionContext") -> Intent:
    """按已登记枚举分派策略；未登记则回退 auto（保守）。"""
    fn = STRATEGY_TABLE.get(decision_type)
    if fn is None:
        log.error("未登记的 decision_type=%r，回退 auto", decision_type)
        fn = _strategy_auto
    return fn(p, ctx)


__all__ = ["infer_stage", "dispatch", "registered_types", "STRATEGY_TABLE", "DecisionContext"]


class DecisionContext:
    """
    决策上下文：把世界模型、LLM 信箱、事件策略聚合，注入各策略函数。
    （用轻量容器而非全局变量，便于单测与解耦。）
    """

    def __init__(self, world, llm_inbox, event_policy, cfg, stage_fn=None) -> None:
        self.world = world
        self.llm_inbox = llm_inbox
        self.event_policy = event_policy
        self.cfg = cfg
        self._stage_fn = stage_fn or infer_stage

    def stage(self, p: Perception) -> int:
        return self._stage_fn(p)
