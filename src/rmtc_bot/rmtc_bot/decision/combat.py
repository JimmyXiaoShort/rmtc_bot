# -*- coding: utf-8 -*-
"""
第二阶段策略：对抗 / 任务（Combat）。

约束（规则 §6、§10、协议第 4 节）：
- 第二阶段动作白名单：MOVE / ATTACK / DEFEND / WAIT；
- ATTACK：命中指定方向直线 1–2 格内最近的存活敌车；普通目标 20 HP、防御目标 10 HP、
  出生保护目标 0 HP；无命中概率；出生保护中的攻击者不能 ATTACK；
- DEFEND：当前 Tick 不移动不攻击，普通受击由 20 降为 10 HP（须每 Tick 提交）；
- 有效伤害 D：先累计实际攻击伤害 + 击杀奖励，再按每次死亡扣 60，最低 0；
  D 达到 500 时对抗分满分（v1.1 由 300 调整为 500）；累计值没有 500 上限；
- 击杀奖励：普通攻击/辣椒/反伤致死时，每名被击毁车辆奖励总量 60，贡献者按队伍去重平分；
- 任务（reach/资源/伤害）在事件策略与 FSM 中叠加；此模块专注对抗。
"""

from __future__ import annotations

import logging
from typing import Optional

from ..models import Intent, Perception
from ..perception.world_model import WorldModel

log = logging.getLogger("rmtc.strategy.combat")

# 战斗行为参数（答辩/调参用；与规则数值无关）
_ENGAGE_RANGE = 2      # 攻击射程（直线 1–2 格；已进入射程即开火）
_SCAN_RANGE = 4        # 索敌扫描半径（视野内 ±4 格）
_FLEE_HP = 15          # HP 低于该值强制撤离
_SAFE_MODE_HP = 40     # safe 模式触发撤离的 HP 阈值

# 方向 → 位移（规则 §4.1：x 向右、y 向下）
_DIR_VEC = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}

_DIR_VEC_OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}


def _enemy_near(p: Perception) -> Optional[object]:
    """返回视野内最近的存活敌车（RobotEntity 或 None）。"""
    best = None
    best_d2 = float("inf")
    me = p.me
    if me is None or me.position is None:
        return None
    x, y = me.position
    for r in p.robots:
        if r.position is None:
            continue
        d2 = (r.position[0] - x) ** 2 + (r.position[1] - y) ** 2
        if d2 <= _SCAN_RANGE ** 2 and d2 < best_d2:
            best, best_d2 = r, d2
    return best


def _line_of_fire(p: Perception, enemy) -> bool:
    """敌方是否在我方指定方向的直线 1–2 格内（贴脸攻击判定）。"""
    me = p.me
    if me is None or me.position is None or enemy is None or enemy.position is None:
        return False
    dx = enemy.position[0] - me.position[0]
    dy = enemy.position[1] - me.position[1]
    # 攻击命中「指定方向直线 1–2 格内最近目标」；这里对四方向直接判断
    manhattan_d = abs(dx) + abs(dy)
    if not (1 <= manhattan_d <= _ENGAGE_RANGE):
        return False
    # 必须与某一条坐标轴对齐（直线 1–2 格）
    return dx == 0 or dy == 0


def _attack_direction(p: Perception, enemy) -> Optional[str]:
    """返回指向敌方的方向字符串；不正对返回 None。"""
    me = p.me
    if me is None or me.position is None or enemy is None or enemy.position is None:
        return None
    dx = enemy.position[0] - me.position[0]
    dy = enemy.position[1] - me.position[1]
    if dx == 0 and dy == 0:
        return None
    if abs(dx) > abs(dy):
        return "right" if dx > 0 else "left"
    return "down" if dy > 0 else "up"


def _free_step(wm: WorldModel, src, direction: str) -> bool:
    """该方向下一格是否为本地已知可达格（界内且非已知障碍）。"""
    dx, dy = _DIR_VEC[direction]
    return wm.is_free((src[0] + dx, src[1] + dy))


def _candidates_toward(p: Perception, target) -> list:
    """朝目标的候选方向序列：主轴 → 次轴 → 其余方向（确定性）。"""
    me = p.me
    assert me is not None and me.position is not None
    dx = target[0] - me.position[0]
    dy = target[1] - me.position[1]
    horiz = "right" if dx > 0 else ("left" if dx < 0 else None)
    vert = "down" if dy > 0 else ("up" if dy < 0 else None)
    primary = horiz if abs(dx) >= abs(dy) else vert
    secondary = vert if primary == horiz else horiz
    out = [d for d in (primary, secondary) if d is not None]
    for d in ("up", "down", "left", "right"):
        if d not in out:
            out.append(d)
    return out


def _step_toward(p: Perception, wm: WorldModel, target) -> Optional[str]:
    """
    朝目标走一格，避开已知障碍与边界，并尽量不原路折返。

    返回 None 表示四方向均被封死（调用方退化为 WAIT / DEFEND）。
    """
    me = p.me
    assert me is not None and me.position is not None
    cands = _candidates_toward(p, target)
    la = me.last_action
    last = la.effective_direction if (la is not None and la.moved) else None
    if last is not None:
        rev = _DIR_VEC_OPPOSITE.get(last)
        if rev in cands:
            cands.remove(rev)
            cands.append(rev)
    for d in cands:
        if _free_step(wm, me.position, d):
            return d
    return None


def _flee_direction(p: Perception, wm: WorldModel) -> Optional[str]:
    """撤离方向：远离最近敌人；无敌人时选任一可达方向。全被封死返回 None。"""
    me = p.me
    if me is None or me.position is None:
        return None
    enemy = _enemy_near(p)
    cands = []
    if enemy is not None and enemy.position is not None:
        dx = me.position[0] - enemy.position[0]
        dy = me.position[1] - enemy.position[1]
        horiz = "right" if dx > 0 else ("left" if dx < 0 else None)
        vert = "down" if dy > 0 else ("up" if dy < 0 else None)
        primary = horiz if abs(dx) >= abs(dy) else vert
        secondary = vert if primary == horiz else horiz
        cands = [d for d in (primary, secondary) if d is not None]
    for d in ("up", "down", "left", "right"):
        if d not in cands:
            cands.append(d)
    for d in cands:
        if _free_step(wm, me.position, d):
            return d
    return None


def decide_combat(p: Perception, ctx, safe_mode: bool = False) -> Intent:
    """阶段二决策入口：MOVE / ATTACK / DEFEND / WAIT。"""
    wm: WorldModel = ctx.world
    me = p.me
    if me is None or me.position is None:
        return Intent(kind="wait", reason="no-self")

    enemy = _enemy_near(p)
    hp = me.hp

    # 站上资源格即视为已采集：从记忆移除，避免 patrol 反复瞄准脚下已空资源
    if me.position in wm.resources:
        wm.mark_resource_collected(me.position)

    # ---- 1) 残血 / safe 模式 → 优先撤离或 DEFEND ----
    # safe 模式语义是「优先脱离威胁」（见 registry._strategy_safe 文档）：
    # 先尝试撤离；仅当四方向都被已知障碍/边界封死时才 DEFEND。
    # 若像普通模式那样「敌在射程线内就先 DEFEND」，两台残血 safe 车贴脸会互相
    # 防御、永不移动，形成长期僵持（本地两阶段仿真中曾出现 180 Tick 冻结）。
    if safe_mode and hp <= _SAFE_MODE_HP:
        d = _flee_direction(p, wm)
        if d is not None:
            return Intent(kind="move", direction=d, reason=f"safe-flee({hp})")
        return Intent(kind="defend", reason=f"safe-corned({hp})")

    # 普通模式残血：贴脸（敌在射程线内）先 DEFEND 抵消伤害，否则撤离
    if hp <= _FLEE_HP:
        if enemy is not None and _line_of_fire(p, enemy):
            return Intent(kind="defend", reason=f"flee-hp({hp})")
        d = _flee_direction(p, wm)
        if d is not None:
            return Intent(kind="move", direction=d, reason=f"flee-hp({hp})")
        return Intent(kind="defend", reason=f"flee-blocked({hp})")

    # ---- 2) 交战逻辑 ----
    if enemy is not None and _line_of_fire(p, enemy):
        # 出生保护中的攻击者不能 ATTACK（规则 §6.2）
        if me.protected:
            d = _flee_direction(p, wm)
            if d is not None:
                return Intent(kind="move", direction=d, reason="protected-no-attack")
            return Intent(kind="defend", reason="protected-no-attack")
        return Intent(kind="attack", direction=_attack_direction(p, enemy), reason="engage")

    # ---- 3) 追敌 / 游走 ----
    if enemy is not None:
        d = _step_toward(p, wm, enemy.position)
        if d is not None:
            return Intent(kind="move", direction=d, reason="chase")
        return Intent(kind="wait", reason="chase-blocked")

    # ---- 4) 无敌人 → 探索 / 任务点 / 游走 ----
    return _patrol(p, wm)


def _patrol(p: Perception, wm: WorldModel) -> Intent:
    """无敌人时的游走：未完成任务点 → 已知资源 → 地图中心。"""
    me = p.me
    assert me is not None and me.position is not None
    target = None
    for tx, ty in wm.mission_points:
        if wm.mission_done.get((tx, ty), False):
            continue
        if target is None or (abs(tx - me.position[0]) + abs(ty - me.position[1])) < \
                (abs(target[0] - me.position[0]) + abs(target[1] - me.position[1])):
            target = (tx, ty)
    if target is None:
        res = wm.nearest_resource(me.position, high_first=True)
        if res is not None and res.position is not None:
            target = res.position
    if target is None:
        target = (p.map_w // 2, p.map_h // 2)
    d = _step_toward(p, wm, target)
    if d is None:
        return Intent(kind="wait", reason="patrol-blocked")
    return Intent(kind="move", direction=d, reason="patrol")
