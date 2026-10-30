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
_DEFEND_HP = 30        # HP 低于该值且被贴脸时优先 DEFEND
_FLEE_HP = 15          # HP 低于该值强制撤离
_SAFE_MODE_HP = 40     # safe 模式触发撤离的 HP 阈值

# 方向 → 位移（规则 §4.1：x 向右、y 向下）
_DIR_VEC = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}


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


def _move_toward(p: Perception, target) -> str:
    """朝目标走一格（返回方向）；优先长轴。"""
    me = p.me
    assert me is not None and me.position is not None
    dx = target[0] - me.position[0]
    dy = target[1] - me.position[1]
    if abs(dx) >= abs(dy):
        return "right" if dx > 0 else ("left" if dx < 0 else ("down" if dy > 0 else "up"))
    return "down" if dy > 0 else ("up" if dy < 0 else ("right" if dx > 0 else "left"))


def _flee_direction(p: Perception) -> str:
    """撤离方向：远离最近敌人；无敌人返回 'up'（保守原地靠边）。"""
    me = p.me
    if me is None or me.position is None:
        return "up"
    enemy = _enemy_near(p)
    if enemy is None or enemy.position is None:
        return "up"
    dx = me.position[0] - enemy.position[0]
    dy = me.position[1] - enemy.position[1]
    if abs(dx) >= abs(dy):
        return "right" if dx > 0 else "left"
    return "down" if dy > 0 else "up"


def decide_combat(p: Perception, ctx, safe_mode: bool = False) -> Intent:
    """阶段二决策入口：MOVE / ATTACK / DEFEND / WAIT。"""
    wm: WorldModel = ctx.world
    me = p.me
    if me is None or me.position is None:
        return Intent(kind="wait", reason="no-self")

    enemy = _enemy_near(p)
    hp = me.hp

    # ---- 1) 残血 / safe 模式 → 优先 DEFEND 或撤离 ----
    if (not safe_mode and hp <= _FLEE_HP) or (safe_mode and hp <= _SAFE_MODE_HP):
        if enemy is not None and _line_of_fire(p, enemy):
            return Intent(kind="defend", reason=f"flee-hp({hp})")
        return Intent(kind="move", direction=_flee_direction(p), reason=f"flee-hp({hp})")

    # ---- 2) 交战逻辑 ----
    if enemy is not None and _line_of_fire(p, enemy):
        # 出生保护中的攻击者不能 ATTACK（规则 §6.2）
        if me.protected:
            return Intent(kind="move", direction=_flee_direction(p), reason="protected-no-attack")
        return Intent(kind="attack", direction=_attack_direction(p, enemy), reason="engage")

    # ---- 3) 追敌 / 游走 ----
    if enemy is not None:
        return Intent(kind="move", direction=_move_toward(p, enemy.position), reason="chase")

    # ---- 4) 无敌人 → 探索 / 任务点 / 游走 ----
    return _patrol(p, wm)


def _patrol(p: Perception, wm: WorldModel) -> Intent:
    """无敌人时的游走：向最近未完成任务点（若有）或地图中心扫荡。"""
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
        target = (p.map_w // 2, p.map_h // 2)
    return Intent(kind="move", direction=_move_toward(p, target), reason="patrol")
