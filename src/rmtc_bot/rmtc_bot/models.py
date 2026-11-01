# -*- coding: utf-8 -*-
"""
内部数据结构定义。

感知 JSON（协议第 3 节）→ 下列 dataclass；决策层只依赖这些结构，
不直接接触原始 JSON。所有坐标均为「地图绝对坐标 (x, y)」，原点 [0,0] 在左上角。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

XY = Tuple[int, int]


# ---------------------------------------------------------------------------
# 感知子结构
# ---------------------------------------------------------------------------
@dataclass
class LastAction:
    """self.last_action：上一 Tick 的无坐标回执（开局为 None/null）。

    字段：tick、action、moved、effective_direction（成功移动时给出）、error（失败可带）。
    回执不含起点、终点或累计位移；不得据此推算位置。
    """

    tick: Optional[int] = None
    action: Optional[str] = None
    moved: bool = False
    effective_direction: Optional[str] = None
    error: Optional[str] = None


@dataclass
class SelfState:
    """本车状态（感知 self 字段）。"""

    team: str
    robot_id: str
    position: XY
    hp: int = 0
    alive: bool = True
    protected: bool = False          # 是否处于出生保护（保护期内不能攻击）
    respawn_in: int = 0              # 距离复活还有多少 Tick（存活时为 0）
    position_reason: Optional[str] = None   # 仅开局/重生首帧：initial / respawn
    last_action: Optional[LastAction] = None


@dataclass
class RobotEntity:
    """type=robot：视野内其他存活机器人。position 已换算为绝对坐标。"""

    id: str
    team: str
    robot_id: str
    position: XY
    hp: int = 0
    protected: bool = False
    last_seen_tick: int = 0


@dataclass
class ResourceEntity:
    """type=resource_normal / resource_high。value >= 20 记为高价值。"""

    id: str
    position: XY
    value: int = 0
    is_high: bool = False
    last_seen_tick: int = 0


@dataclass
class Obstacle:
    """type=obstacle：障碍格，不可进入、不可攻击或破坏。"""

    position: XY
    last_seen_tick: int = 0


@dataclass
class Score:
    """score.*：仅保留裁判下发的无效动作计数，作为最后一道自检信号。

    其余累计分（resource_value / effective_damage / mission_completed）不参与决策：
    资源、伤害、任务进度均由视野内实体直接感知，无需裁判汇总分。
    """

    invalid_actions: int = 0


@dataclass
class Mission:
    """missions[] 单条结构化任务（协议 §3.1）。"""

    id: str
    type: str
    target: Optional[float] = None
    position: Optional[XY] = None


@dataclass
class Perception:
    """一次完整感知（协议第 3 节）。"""

    tick: int
    session_id: str
    vision_radius: int = 0
    stage: int = 1                    # 1=第一阶段，2=第二阶段（由 parser 按 tick/视野内机器人推断）
    me: Optional[SelfState] = None
    robots: List[RobotEntity] = field(default_factory=list)
    resources: List[ResourceEntity] = field(default_factory=list)
    obstacles: List[Obstacle] = field(default_factory=list)
    score: Score = field(default_factory=Score)
    missions: List[Mission] = field(default_factory=list)
    map_w: int = 0
    map_h: int = 0


# ---------------------------------------------------------------------------
# 决策层结构
# ---------------------------------------------------------------------------
@dataclass
class Intent:
    """状态机输出的「意图」，由 action.builder 转为协议动作 JSON。"""

    kind: str                                  # move / attack / defend / wait
    direction: Optional[str] = None            # move/attack 必需：up/down/left/right
    reason: str = ""                           # 决策理由（写入日志，便于答辩）
    target: Optional[XY] = None                # 目标坐标（日志用）
