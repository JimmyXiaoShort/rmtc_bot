# -*- coding: utf-8 -*-
"""
堡垒策略（规则 v1.1 §8.5「天下无双」）—— 决策层的堡垒行为叠加。

【职责】
在第二阶段（及任何允许 MOVE/ATTACK/DEFEND/WAIT 的场合）把堡垒纳入决策优先级：
  1) 低血（HP ≤ fort_retreat_hp）且尚未拿到增益 → 回撤至堡垒，并在堡垒内持续移动以
     推进连续驻留（§8.5 要求「移动后位于堡垒内」才算累计）；
  2) 已获增益 → 立刻交回常规对抗/任务策略（增益持续到堡垒结束，无需留守）；
  3) 敌车占据堡垒时的应对：strike / contest / avoid（本地可配置策略）。

【与官方原文的边界】
- 官方规则只说「地图中心生成 3×3 堡垒」，未公布具体坐标 → 坐标由配置提供，
  默认 [11,11]–[13,13]（来源：选手提供的赛场信息）。
- 【重要】堡垒只授予「天下无双」增益，**不提供回血**（回血来自治疗点事件 §8.4 与复活）；
  原文 §5.2 第 7 步的「有效回血」指治疗点效果，不能据此在堡垒里等待恢复。
  因此本模块不做任何「驻留回血」等待，避免长时间原地停滞。
- 增益达成后「须主动提交 ATTACK 和合法方向」（§8.5）→ 达标后立即交回 combat 开火。
"""

from __future__ import annotations

import logging
from typing import Optional

from .. import constants as C
from ..models import Intent, Perception
from ..perception.world_model import WorldModel

log = logging.getLogger("rmtc.strategy.fortress")

_DIR_VEC = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


def _step_dir(src, dst) -> Optional[str]:
    """从 src 朝 dst 走一格的确定性方向（先水平后垂直，与动作枚举一致）。"""
    dx, dy = dst[0] - src[0], dst[1] - src[1]
    if abs(dx) >= abs(dy):
        if dx > 0:
            return "right"
        if dx < 0:
            return "left"
        if dy > 0:
            return "down"
        if dy < 0:
            return "up"
        return None
    if dy > 0:
        return "down"
    if dy < 0:
        return "up"
    if dx > 0:
        return "right"
    return "left"


def _line_attack_dir(me_xy, enemy_xy) -> Optional[str]:
    """敌车是否在本方指定方向直线 1–2 格内；是则返回攻击方向（普通攻击规则）。"""
    dx = enemy_xy[0] - me_xy[0]
    dy = enemy_xy[1] - me_xy[1]
    if dx != 0 and dy != 0:
        return None
    d = abs(dx) + abs(dy)
    if not (1 <= d <= C.ATTACK_RANGE_MAX):
        return None
    if dx == 0:
        return "down" if dy > 0 else "up"
    return "right" if dx > 0 else "left"


def _nearest_enemy_on_fort(p: Perception, wm: WorldModel):
    """返回视野内、位于堡垒格上的最近敌车（无则 None）。"""
    fort = wm.fortress
    if fort is None or p.me is None:
        return None
    best, best_d = None, float("inf")
    for r in p.robots:
        if r.position is None or not fort.contains(r.position):
            continue
        d = abs(r.position[0] - p.me.position[0]) + abs(r.position[1] - p.me.position[1])
        if d < best_d:
            best, best_d = r, d
    return best


def _cruise_inside(p: Perception, wm: WorldModel, fort, me) -> Optional[Intent]:
    """在堡垒内推进「移动后位于堡垒内」的连续驻留（朝向下一格匀速巡航）。

    下一格被已知障碍占用时返回 None（交回常规策略绕行），不硬撞。
    """
    nxt = fort.next_cell(me.position)
    if nxt == me.position:
        return None
    if not wm.is_obstacle(nxt):
        d = _step_dir(me.position, nxt)
        if d is not None:
            return Intent(kind="move", direction=d,
                          reason=f"fort-reside({fort.streak}/{fort.need})", target=nxt)
    return None


def decide_fortress(p: Perception, ctx) -> Optional[Intent]:
    """
    堡垒叠加决策：返回 Intent 表示「本 Tick 采用堡垒行为」；返回 None 表示
    「不干预，交回常规对抗/任务策略」。
    """
    wm: WorldModel = ctx.world
    fort = wm.fortress
    me = p.me
    if fort is None or me is None or me.position is None or not me.alive or me.respawn_in > 0:
        return None
    if not fort.is_active(p.tick):
        return None
    # 已获增益：堡垒不再提供任何额外收益（也不回血）→ 立即交回常规策略
    if fort.buff_granted:
        return None

    hp = me.hp
    inside = fort.contains(me.position)
    enemy_on_fort = _nearest_enemy_on_fort(p, wm)
    policy = str(getattr(ctx.cfg, "fort_enemy_policy", C.FORT_ENEMY_POLICY_DEFAULT))
    retreat_hp = fort.retreat_hp()

    # 敌人贴脸且在攻击线内 → 优先开火（出生保护者不能攻击，规则 §6.2）
    atk_dir = None
    if enemy_on_fort is not None and enemy_on_fort.position is not None:
        atk_dir = _line_attack_dir(me.position, enemy_on_fort.position)

    # ---------- 1) 低血：回撤至堡垒并推进驻留（堡垒不回血，只给增益） ----------
    if hp <= retreat_hp:
        if atk_dir is not None and not me.protected:
            return Intent(kind="attack", direction=atk_dir,
                          reason=f"fort-defend-low-hp({hp})", target=enemy_on_fort.position)
        if inside:
            intent = _cruise_inside(p, wm, fort, me)
            return intent   # 巡航失败则交回常规策略，绝不原地等待回血
        tgt = fort.nearest_cell(me.position)
        d = _step_dir(me.position, tgt)
        if d is None:
            return None
        nx, ny = me.position[0] + _DIR_VEC[d][0], me.position[1] + _DIR_VEC[d][1]
        if not wm.is_free((nx, ny)):
            return None   # 直线被已知障碍挡住 → 交回常规策略绕行
        return Intent(kind="move", direction=d, reason=f"fort-retreat({hp})", target=tgt)

    # ---------- 2) 敌车占据堡垒 ----------
    if enemy_on_fort is not None:
        if policy == "avoid":
            return None  # 交给常规逻辑（通常会追击/撤离）
        if policy == "strike" and atk_dir is not None and not me.protected:
            return Intent(kind="attack", direction=atk_dir,
                          reason="fort-strike", target=enemy_on_fort.position)
        # contest / strike 且在堡垒外 → 争夺（朝最近堡垒格推进）
        if not inside:
            tgt = fort.nearest_cell(me.position)
            d = _step_dir(me.position, tgt)
            if d is not None:
                nx, ny = me.position[0] + _DIR_VEC[d][0], me.position[1] + _DIR_VEC[d][1]
                if wm.is_free((nx, ny)):
                    return Intent(kind="move", direction=d, reason="fort-contest", target=tgt)
        return None

    # ---------- 3) 驻留累计中（未达标）→ 在堡垒内巡航 ----------
    if inside:
        return _cruise_inside(p, wm, fort, me)

    # ---------- 4) 其余情况交回常规对抗/任务策略 ----------
    return None
