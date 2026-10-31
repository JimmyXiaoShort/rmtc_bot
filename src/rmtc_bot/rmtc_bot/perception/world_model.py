# -*- coding: utf-8 -*-
"""
世界模型（World Model）。

职责：把「单帧感知」与「本场历史记忆」融合，供决策层查询。
合规边界（规则 §4.1、协议第 3 节、第 8 节）：
- 允许记忆和推断此前「合法获得」的信息（本队视野 + 公开公告）；
- 但不得把推断当作裁判提供的实时真值：所有带 TTL 的推断都要过期；
- 不得把测试端网页全图、地图真值等当作输入。

地图表示采用稀疏结构（本地已知障碍 / 本地已知资源），而非整幅真值地图——
因为感知只提供视野内实体，全局地图真值不公开。
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Dict, Optional, Set, Tuple

from .. import constants as C
from ..models import (
    Obstacle,
    Perception,
    ResourceEntity,
    RobotEntity,
    XY,
)
from .fortress import FortressModel

log = logging.getLogger("rmtc.world")

# 记忆 TTL（本地设计参数：资源一旦收集即移除且本场不刷新，
# 故资源记忆可较长；敌人位置推断必须短 TTL，避免当作实时真值）
ROBOT_MEMORY_TTL = 8        # Tick：视野外敌人推断保留时长
RESOURCE_MEMORY_TTL = 400   # Tick：资源记忆保留时长（本场不刷新）
OBSTACLE_MEMORY_TTL = 100_000  # 静态障碍长期保留（临时障碍需按事件到期清理）


class WorldModel:
    """单调更新的世界模型；每个场次重建一次（session_id 变化时 reset）。"""

    def __init__(self, cfg=None) -> None:
        self.cfg = cfg
        self.session_id: Optional[str] = None
        self.tick: int = -1
        self.map_w: int = 0
        self.map_h: int = 0
        self.self_pos: XY = (0, 0)
        self.self_hp: int = C.HP_MAX
        self.alive: bool = True
        self.vision_radius: int = C.VISION_RADIUS_MAX

        # 记忆体
        self.obstacles: Dict[XY, Obstacle] = {}
        self.resources: Dict[XY, ResourceEntity] = {}
        self.enemies: Dict[str, RobotEntity] = {}      # 以 robot_id 为键
        self.visited: Set[XY] = set()                  # 本车已到过的格（用于探索）
        self.mission_points: Set[XY] = set()           # 任务坐标（来自感知 missions 的 reach 型）
        self.mission_done: Dict[XY, bool] = {}         # 任务坐标是否已完成
        self.event_log: deque = deque(maxlen=200)      # 本场事件公告原文（含去重）

        # 堡垒模型（规则 §8.5）
        self.fortress = FortressModel(cfg) if cfg is not None else None

        # 裁判下发的无效动作计数（仅用于自检告警）
        self.last_invalid_actions: int = 0

    # ------------------------------------------------------------------
    # 场次管理
    # ------------------------------------------------------------------
    def reset_for_session(self, session_id: str) -> None:
        """换场清空：收到新的 session_id 时清空上一场的规划、定位与公告缓存。

        采用「就地重置」而非调用 __init__：避免重建 fortress 等子对象，
        否则外部持有的 world.fortress 引用会指向已失效的旧实例。
        """
        log.info("世界模型重置：session %s -> %s", self.session_id, session_id)
        self.session_id = session_id
        self.tick = -1
        self.map_w = 0
        self.map_h = 0
        self.self_pos = (0, 0)
        self.self_hp = C.HP_MAX
        self.alive = True
        self.vision_radius = C.VISION_RADIUS_MAX

        # 容器就地清空（保留对象身份，外部引用可见清空后的状态）
        self.obstacles.clear()
        self.resources.clear()
        self.enemies.clear()
        self.visited.clear()
        self.mission_points.clear()
        self.mission_done.clear()
        self.event_log.clear()

        # 堡垒模型就地重置（保留同一实例）
        if self.fortress is not None:
            self.fortress.reset_for_session()

        self.last_invalid_actions = 0

    # ------------------------------------------------------------------
    # 更新
    # ------------------------------------------------------------------
    def update(self, p: Perception) -> None:
        """用一帧感知更新世界模型。"""
        if self.session_id != p.session_id:
            self.reset_for_session(p.session_id)

        self.tick = p.tick

        if p.map_w and p.map_h:
            self.map_w, self.map_h = p.map_w, p.map_h
        self.vision_radius = p.vision_radius

        if p.me is not None:
            self.self_pos = p.me.position
            self.self_hp = p.me.hp
            self.alive = p.me.alive
            self.visited.add(p.me.position)

        # 无效动作自检：裁判计 invalid_actions 递增说明本队在发非法动作，必须告警
        if p.score is not None and p.score.invalid_actions > self.last_invalid_actions:
            log.warning("无效动作计数上升：%d -> %d（tick=%s），请检查动作合法性",
                        self.last_invalid_actions, p.score.invalid_actions, p.tick)
            self.last_invalid_actions = p.score.invalid_actions

        # 障碍：静态记忆，需按 TTL 清理
        for ob in p.obstacles:
            self.obstacles[ob.position] = Obstacle(
                position=ob.position, last_seen_tick=p.tick
            )

        # 资源：以 id 维度去重（同 id 位置不变）；收集后本场不刷新
        for res in p.resources:
            self.resources[res.position] = ResourceEntity(
                id=res.id, position=res.position, value=res.value, is_high=res.is_high,
                last_seen_tick=p.tick,
            )

        # 敌人：以 robot_id 为键更新；离开视野靠 TTL 过期
        for rb in p.robots:
            self.enemies[rb.robot_id or rb.id] = RobotEntity(
                id=rb.id, team=rb.team, robot_id=rb.robot_id, position=rb.position,
                hp=rb.hp, protected=rb.protected,
                last_seen_tick=p.tick,
            )

        # 任务（来自感知 missions）：reach 型记录坐标；done 状态跟随感知更新
        for m in p.missions:
            if m.type == C.MISSION_TYPE_REACH and m.position is not None:
                self.mission_points.add(m.position)
                self.mission_done[m.position] = bool(m.target and m.target <= 1)

        # ---- 堡垒驻留 / 回血观测（规则 §8.5）----
        # moved 来自上一 Tick 动作回执（self.last_action.moved）；
        # 官方措辞「移动后位于堡垒内」→ 用该回执作为「成功移动」的唯一合法依据。
        if self.fortress is not None:
            la = p.me.last_action if p.me is not None else None
            moved = bool(la.moved) if la is not None else False
            self.fortress.update(p, moved=moved, tick=p.tick)

        self._expire(p.tick)

    def _expire(self, now: int) -> None:
        """按 TTL 清理推断性记忆，避免把过时推断当实时真值。"""
        stale_enemies = [
            k for k, e in self.enemies.items()
            if now - e.last_seen_tick > ROBOT_MEMORY_TTL
        ]
        for k in stale_enemies:
            del self.enemies[k]

        stale_res = [
            k for k, r in self.resources.items()
            if now - r.last_seen_tick > RESOURCE_MEMORY_TTL
        ]
        for k in stale_res:
            del self.resources[k]

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def in_bounds(self, xy: XY) -> bool:
        """越界判断（依赖感知提供的 map 尺寸）。"""
        if self.map_w <= 0 or self.map_h <= 0:
            return True  # 尺寸未知时不拦截，交由裁判 E_OOB 判定
        return 0 <= xy[0] < self.map_w and 0 <= xy[1] < self.map_h

    def is_obstacle(self, xy: XY) -> bool:
        return xy in self.obstacles

    def is_free(self, xy: XY) -> bool:
        """本地碰撞预判：在界内且非已知障碍。"""
        return self.in_bounds(xy) and not self.is_obstacle(xy)

    def nearest_resource(self, from_xy: XY, high_first: bool = True) -> Optional[ResourceEntity]:
        """返回「价值 / 距离」最优的资源；高价值优先由其价值体现。"""
        best: Optional[ResourceEntity] = None
        best_key = None
        for res in self.resources.values():
            d = manhattan(from_xy, res.position)
            # 单位距离价值：高价值资源天然更优；距离越近越优
            score = res.value / max(1, d)
            key = (score, res.value)
            if best_key is None or key > best_key:
                best_key = key
                best = res
        if best is not None and not high_first:
            # 仅按距离选择
            return min(self.resources.values(), key=lambda r: manhattan(from_xy, r.position))
        return best

    def mark_resource_collected(self, xy: XY) -> None:
        """资源被收集后从记忆移除（本场不刷新）。"""
        self.resources.pop(xy, None)

    def record_event_text(self, text: str) -> bool:
        """
        记录事件公告原文并去重。

        协议第 3 节：公告话题逐条发送，感知中的 event_text 可重复包含近期公告；
        重复收到公告不表示事件重复触发；同一 Tick 也可能有多条不同公告，
        不得仅按 Tick 去除不同内容。故此处按「文本内容」去重。
        返回 True 表示这是一条新的（未见过）公告。
        """
        text = (text or "").strip()
        if not text:
            return False
        if text in self.event_log:
            return False
        self.event_log.append(text)
        return True


def manhattan(a: XY, b: XY) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])
