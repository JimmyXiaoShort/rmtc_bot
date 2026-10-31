# -*- coding: utf-8 -*-
"""
第一阶段策略：采集（Harvest）。

约束（规则 §9、协议第 4 节）：
- 阶段一仅 MOVE / WAIT 合法（ATTACK / DEFEND 非法 → 无效动作扣分）；
- 无攻击、无任务、无随机事件；地图 20×20；对手之间不可见；
- 目标：100 Tick 内尽可能多地收集资源（普通 10 分 × 12，高价值 20 分 × 4，总值 200）。
- 移动：MOVE 需带 direction（up/down/left/right），沿指定方向移动一格。
"""

from __future__ import annotations

import logging

from ..models import Intent, Perception
from ..perception.world_model import WorldModel

log = logging.getLogger("rmtc.strategy.collect")

# 移动目标候选的偏好权重（收集行为参数，便于答辩解释）
_COST_AVOID_ROBOT = 500.0   # 视距内机器人规避（阶段一对手不可见，通常不触发）
_COST_CELL_COUNT = 5.0      # 已走过格数惩罚系数
_COST_REVERSE = 30.0        # 反向（回到上一格）惩罚：避免相邻两格无限往返

_DIR_VEC = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}

_OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}


def _best_direction(p: Perception, wm: WorldModel) -> str:
    """
    用「可访问区域评估」选移动方向。

    对四方向分别评估（越小越好）：
      - 已知障碍/越界        → 直接跳过（避免把动作浪费在撞墙上）
      - 贴近敌人             → +_COST_AVOID_ROBOT
      - 方向 1~2 格内已走过的格数 → +_COST_CELL_COUNT * 格数（倾向于未探索区域）
      - 与上一 Tick 来向相反   → +_COST_REVERSE（避免两格间往返震荡）
    并列时按 `_DIR_VEC` 定义顺序取第一个（确定性）。
    """
    me = p.me
    assert me is not None and me.position is not None
    x, y = me.position

    # 上一 Tick 的成功移动方向（来向），用于反向惩罚
    last_dir = None
    la = me.last_action
    if la is not None and la.moved and la.effective_direction in _DIR_VEC:
        last_dir = la.effective_direction

    best_dir, best_cost = None, float("inf")
    for d, (dx, dy) in _DIR_VEC.items():
        nx, ny = x + dx, y + dy
        # 越界方向不选（靠近边界时自然转向）
        if p.map_w > 0 and p.map_h > 0:
            if not (0 <= nx < p.map_w and 0 <= ny < p.map_h):
                continue
        # 已知障碍不选（本地预判，避免无效动作）
        if wm.is_obstacle((nx, ny)):
            continue

        cost = 0.0
        # 无已知资源时的探索偏好：越靠近地图中心代价越低。
        # 第一阶段高价值资源通常位于中心区域；该偏好只影响「扫描」，
        # 一旦发现资源仍由上方 nearest_resource 贪心追击接管。
        if p.map_w > 0 and p.map_h > 0:
            cx, cy = p.map_w // 2, p.map_h // 2
            if (cx, cy) not in wm.visited:
                cost += 0.6 * (abs(nx - cx) + abs(ny - cy))

        for r in p.robots:
            if r.position is None:
                continue
            if abs(r.position[0] - nx) <= 1 and abs(r.position[1] - ny) <= 1:
                cost += _COST_AVOID_ROBOT

        # 1~2 格前瞻：统计该走廊内已走过的格数，倾向于走向未探索区域
        cnt = 0
        for step in (1, 2):
            cx, cy = x + dx * step, y + dy * step
            if p.map_w > 0 and p.map_h > 0:
                if not (0 <= cx < p.map_w and 0 <= cy < p.map_h):
                    break
            if (cx, cy) in wm.visited:
                cnt += 1
        cost += _COST_CELL_COUNT * cnt

        if last_dir is not None and d == _OPPOSITE[last_dir]:
            cost += _COST_REVERSE

        if cost < best_cost:
            best_dir, best_cost = d, cost

    if best_dir is None:
        # 四方向都被障碍/边界封死：保守 WAIT（不产生无效动作）
        return "wait"
    return best_dir


def decide_collect(p: Perception, ctx) -> Intent:
    """阶段一决策入口：输出 MOVE 或 WAIT。"""
    wm: WorldModel = ctx.world
    me = p.me
    if me is None or me.position is None:
        return Intent(kind="wait", reason="no-self")

    # 站上资源格即视为已采集：从资源记忆移除，否则 nearest_resource 会一直
    # 返回脚下这个 d=0（score 最高）的已空资源，导致此后不再追任何新资源。
    if me.position in wm.resources:
        wm.mark_resource_collected(me.position)

    # 全图都走过 → WAIT（停留；如规则允许原地重复也可继续 MOVE，此处选保守 WAIT）
    # 注意：感知缺少 map 尺寸时（联调早期）不得误判为全图已覆盖，跳过该检查。
    if p.map_w > 0 and p.map_h > 0 and len(wm.visited) >= p.map_w * p.map_h:
        return Intent(kind="wait", reason="all-visited")

    # 优先：追已知最近资源（贪心，价值/距离权衡）；路径用 BFS 求，避免在凹槽/死角绕圈
    target = wm.nearest_resource(me.position, high_first=True)
    if target is not None and target.position is not None:
        d = wm.next_step(me.position, target.position)
        # 已知障碍下不可达 → 交给扫描逻辑继续探索（不产生撞墙动作）
        if d is not None:
            return Intent(kind="move", direction=d, reason="chase-resource")

    # 兜底：无已知资源 → 覆盖式扫描
    d = _best_direction(p, wm)
    if d == "wait":
        return Intent(kind="wait", reason="no-move")
    return Intent(kind="move", direction=d, reason="collect-scan")
