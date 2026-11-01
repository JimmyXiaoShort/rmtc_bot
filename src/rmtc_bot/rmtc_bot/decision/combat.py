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
from collections import deque
from typing import Optional

from ..models import Intent, Perception
from ..perception.world_model import WorldModel, manhattan

log = logging.getLogger("rmtc.strategy.combat")

# 战斗行为参数（答辩/调参用；与规则数值无关）
_ENGAGE_RANGE = 2      # 攻击射程（直线 1–2 格；已进入射程即开火）
_SCAN_RANGE = 4        # 索敌扫描半径（视野内 ±4 格）
_FLEE_HP = 15          # HP 低于该值强制撤离
_SAFE_MODE_HP = 40     # safe 模式触发撤离的 HP 阈值
_RETREAT_RADIUS = 8    # 撤离目标搜索半径（只找附近安全格，避免跨全图逃跑）

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


def _free_step(wm: WorldModel, src, direction: str) -> bool:
    """该方向下一格是否为本地已知可达格（界内且非已知障碍）。"""
    dx, dy = _DIR_VEC[direction]
    return wm.is_free((src[0] + dx, src[1] + dy))


def _candidates_toward(p: Perception, target, axis_pref: Optional[str] = None) -> list:
    """朝目标的候选方向序列：主轴 → 次轴 → 其余方向（确定性）。

    axis_pref 仅用于“正对角线”平局时打破对称：两台车同时追击且 |dx|==|dy| 时，
    若都按同一确定性规则选水平轴，会互相镜像绕圈（永不进入攻击线）。调用方
    按 robot_id 比较给出不同 axis_pref，使双方选不同轴，从而逼近到攻击线。
    """
    me = p.me
    assert me is not None and me.position is not None
    dx = target[0] - me.position[0]
    dy = target[1] - me.position[1]
    horiz = "right" if dx > 0 else ("left" if dx < 0 else None)
    vert = "down" if dy > 0 else ("up" if dy < 0 else None)
    if abs(dx) == abs(dy) and dx != 0 and dy != 0 and axis_pref == "vertical":
        primary = vert
    else:
        primary = horiz if abs(dx) >= abs(dy) else vert
    secondary = vert if primary == horiz else horiz
    out = [d for d in (primary, secondary) if d is not None]
    for d in ("up", "down", "left", "right"):
        if d not in out:
            out.append(d)
    return out


def _step_toward(p: Perception, wm: WorldModel, target,
                 axis_pref: Optional[str] = None) -> Optional[str]:
    """
    朝目标走一格：在已知栅格上取 BFS 最短路的第一步。

    axis_pref 只影响「多条等长最短路」时的枚举顺序（用于打破斜对角镜像追击）。
    返回 None 表示已在目标或当前已知信息下不可达（调用方退化为 WAIT / DEFEND）。
    """
    me = p.me
    assert me is not None and me.position is not None
    order = _candidates_toward(p, target, axis_pref)
    return wm.next_step(me.position, tuple(target), order)


def _nearest_known_enemy(p: Perception, wm: WorldModel):
    """返回世界模型记忆中的最近敌车（含已离开视野但未过 TTL 的）。

    safe 模式用它而不是当前感知，利用记忆 TTL 形成「迟滞」：敌车在 4 格视野
    边缘忽隐忽现时，若只按当前感知判断，会出现「撤离/任务」逐 Tick 交替的
    2 格振荡（本地仿真 B 席曾卡 345 Tick）。
    """
    me = p.me
    if me is None or me.position is None:
        return None
    best, best_d = None, float("inf")
    for e in wm.enemies.values():
        if e.position is None:
            continue
        d = manhattan(me.position, e.position)
        if d < best_d:
            best, best_d = e, d
    return best


def _retreat_step(p: Perception, wm: WorldModel, enemy) -> Optional[str]:
    """朝「离敌更远」的可达格撤离一格（BFS 评估，避免贪心在凹槽里来回）。

    做法：从自身做一次 BFS 得到所有可达格及其路径长度，选出「比当前位置离敌
    至少远 2 格」的目标格（同等安全时取路径最短者），再沿最短路走第一步。

    为什么不能继续用「一步贪心远离」：被障碍夹住时，贪心会选一个方向后退，
    下一 Tick 又因障碍被挤回来，形成永久 2 格振荡（本地仿真 safe 模式曾各席
    卡约 365 Tick）。BFS 保证每一步都确实把「离敌距离」变大，或明确不可撤。
    """
    me = p.me
    if me is None or me.position is None or enemy is None or enemy.position is None:
        return None
    src = me.position
    if wm.map_w <= 0 or wm.map_h <= 0:
        return _flee_direction(p, wm)   # 地图尺寸未知时退回旧的保守逻辑
    exy = enemy.position
    cur_safety = abs(src[0] - exy[0]) + abs(src[1] - exy[1])

    prev = {}
    dist = {src: 0}
    queue = deque([src])
    while queue:
        cur = queue.popleft()
        if dist[cur] >= _RETREAT_RADIUS:
            continue
        for d, (dx, dy) in _DIR_VEC.items():
            nx = (cur[0] + dx, cur[1] + dy)
            if nx in dist or not wm.is_free(nx):
                continue
            dist[nx] = dist[cur] + 1
            prev[nx] = (cur, d)
            queue.append(nx)

    best, best_key = None, None
    # 远离方向（相对于敌车的轴向符号）：用于在等距安全格之间优先「沿远离轴」
    ax = (src[0] > exy[0]) - (src[0] < exy[0])
    ay = (src[1] > exy[1]) - (src[1] < exy[1])
    for cell in dist:
        if cell == src:
            continue
        safety = abs(cell[0] - exy[0]) + abs(cell[1] - exy[1])
        if safety < cur_safety + 2:
            continue
        # 先选最近的格，再优先沿「远离敌人」的轴向移动，最后看安全度。
        # 只按安全度选会让两台互逃的车上演「同速平行逃跑 / 贴墙上下摆动」。
        proj = ax * (cell[0] - src[0]) + ay * (cell[1] - src[1])
        key = (-dist[cell], proj, safety)
        if best_key is None or key > best_key:
            best, best_key = cell, key
    if best is None:
        return None     # 已退到当前可达范围内的最安全位置 → 交由调用方 DEFEND

    cur = best
    while prev[cur][0] != src:
        cur = prev[cur][0]
    return prev[cur][1]


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


def _diag_axis_preference(p: Perception, enemy) -> Optional[str]:
    """正对角线追击时的分轴偏好：按 robot_id 字典序，保证两台车选不同轴。

    若两台车都按同一规则选水平轴，会在斜对角上互相镜像绕圈（永不进入攻击线）。
    """
    me = p.me
    if me is None or enemy is None:
        return None
    my_id = str(me.robot_id or me.team or "")
    en_id = str(getattr(enemy, "robot_id", None) or getattr(enemy, "id", "") or "")
    if not my_id or not en_id or my_id == en_id:
        return None
    return "vertical" if my_id < en_id else "horizontal"


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
    # 视野内有敌车就撤离，绝不主动追击；脱开视野后继续做任务/巡逻。
    # 注意不要「只在近身时逃、稍远就追」——两台低血车会陷入
    # 「拉开 3 格→追回去」的往复（本地仿真中 chase+safe-flee 占满 500 Tick）。
    if safe_mode and hp <= _SAFE_MODE_HP:
        threat = enemy if enemy is not None else _nearest_known_enemy(p, wm)
        if threat is not None:
            d = _retreat_step(p, wm, threat)
            if d is not None:
                return Intent(kind="move", direction=d, reason=f"safe-flee({hp})")
            # 退无可退时若双方都在 DEFEND 且血量不变 → 互相僵持，转为开火打破
            la = me.last_action
            if (enemy is not None and _line_of_fire(p, enemy) and la is not None
                    and la.action == "defend" and wm.prev_hp == hp):
                return Intent(kind="attack", direction=_attack_direction(p, enemy),
                              reason=f"engage-break({hp})")
            return Intent(kind="defend", reason=f"safe-corned({hp})")

    # 普通模式残血：贴脸（敌在射程线内）先 DEFEND 抵消伤害，否则撤离
    if not safe_mode and hp <= _FLEE_HP:
        if enemy is not None and _line_of_fire(p, enemy):
            la = me.last_action
            # 上一 Tick 已 DEFEND 且血量未变 → 对手大概率也在防御（互相僵持），
            # 继续 DEFEND 没有任何进展。此时撤离风险低（对方未开火），尝试脱离；
            # 若已退无可退，则转为攻击：对防御目标能造成 10 HP 伤害。
            if (la is not None and la.action == "defend"
                    and wm.prev_hp == hp):
                d = _retreat_step(p, wm, enemy)
                if d is not None:
                    return Intent(kind="move", direction=d, reason=f"flee-break({hp})")
                return Intent(kind="attack", direction=_attack_direction(p, enemy),
                              reason=f"engage-break({hp})")
            return Intent(kind="defend", reason=f"flee-hp({hp})")
        if enemy is not None:
            d = _retreat_step(p, wm, enemy)
            if d is not None:
                return Intent(kind="move", direction=d, reason=f"flee-hp({hp})")
            return Intent(kind="defend", reason=f"flee-blocked({hp})")
        # 无近敌 → 继续常规任务/巡逻（不为了看不见的敌人原地打转）

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
        d = _step_toward(p, wm, enemy.position,
                         axis_pref=_diag_axis_preference(p, enemy))
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
    # 已经站在游走目标上：原地等待，避免「离开一格又回来」的 2 格永久振荡
    if target == me.position:
        return Intent(kind="wait", reason="at-target")
    d = _step_toward(p, wm, target)
    if d is None:
        return Intent(kind="wait", reason="patrol-blocked")
    return Intent(kind="move", direction=d, reason="patrol")
