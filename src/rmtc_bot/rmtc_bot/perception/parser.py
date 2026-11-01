# -*- coding: utf-8 -*-
"""
感知解析：JSON → 内部数据结构。

严格对照协议第 3 节：
- self.position 为「绝对坐标数组 [x, y]」；
- visible_entities[].position 为「九行相对位置字符串」（89 字符 = 81 格 + 8 换行，无末尾换行），
  1 固定在 (5,5)（行列从 1 开始），2 为该物体，0 为占位；
- 每个可见实体各有一张 position 图，不是合并后的视野总图；
- 方向始终沿原地图坐标轴（x 向右、y 向下），不随机器人移动方向旋转；
- 物体与自身同格时中心保留 1、整图不出现 2，表示相对位移 [0, 0]。

【健壮性要求】接收端须检查字段类型并处理解析异常，不得因字段缺失或类型错误
导致进程退出（协议 §3「感知解析要求」）。本模块保证：任何单字段异常只丢弃该字段，
不抛出到主循环（顶层仍保留 PerceptionParseError 供调用方记录）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional, Tuple

from .. import constants as C
from ..models import (
    LastAction,
    Mission,
    Obstacle,
    Perception,
    ResourceEntity,
    RobotEntity,
    Score,
    SelfState,
    XY,
)

log = logging.getLogger("rmtc.perception")


class PerceptionParseError(Exception):
    """感知整体不可用（非 JSON / 非对象 / 缺 tick 或 session_id）时抛出。"""


# ---------------------------------------------------------------------------
# 9×9 相对位置图解析
# ---------------------------------------------------------------------------
def parse_relative_position(pos_str: Any, self_xy: XY) -> Tuple[Optional[XY], Optional[XY]]:
    """
    解析相对位置字符串，返回 (绝对坐标, 相对位移)。

    返回规则：
    - 找到 '2'：相对位移 = (列-5, 行-5)（行列从 1 开始），绝对坐标 = self_xy + 位移；
    - 整图无 '2'（物体与自身同格）：相对位移 = (0, 0)，绝对坐标 = self_xy；
    - 字符串非法（类型错 / 行数或列数不符）：返回 (None, None)，由调用方丢弃该实体。

    注意：JSON 文本中换行写作 \\n，json.loads 后为真实换行符；
    不得把换行误读为「反斜杠 + 字母 n」两个字符。
    """
    if not isinstance(pos_str, str):
        log.debug("position 非字符串，丢弃：%r", type(pos_str))
        return None, None

    rows = pos_str.split("\n")
    if len(rows) != C.REL_POS_ROWS or any(len(r) != C.REL_POS_COLS for r in rows):
        # 兼容个别环境把换行转义为字面 "\\n" 的情况（防御性处理）
        if "\\n" in pos_str:
            rows = pos_str.split("\\n")
        if len(rows) != C.REL_POS_ROWS or any(len(r) != C.REL_POS_COLS for r in rows):
            log.warning(
                "非法 9x9 位置串（行数=%d，长度=%d），丢弃该实体",
                len(rows), len(pos_str),
            )
            return None, None

    for r_idx, row in enumerate(rows):
        c_idx = row.find(C.REL_POS_TARGET_CHAR)
        if c_idx != -1:
            dx = c_idx - C.REL_POS_CENTER_INDEX
            dy = r_idx - C.REL_POS_CENTER_INDEX
            return (self_xy[0] + dx, self_xy[1] + dy), (dx, dy)

    # 未找到 '2'：与自身重叠，表示相对位移 [0, 0]
    return (self_xy[0], self_xy[1]), (0, 0)


def _as_int(v: Any, default: Optional[int] = None) -> Optional[int]:
    """宽松整数转换：仅接受 int / float / 数字字符串。"""
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return default
    return default


def _as_float(v: Any, default: float = 0.0) -> float:
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return default
    return default


def _as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    return default


def _as_xy(v: Any) -> Optional[XY]:
    """把 [x, y] 解析为整数坐标；非法返回 None。"""
    if isinstance(v, (list, tuple)) and len(v) == 2:
        x = _as_int(v[0])
        y = _as_int(v[1])
        if x is None or y is None:
            return None
        return (x, y)
    return None


# ---------------------------------------------------------------------------
# 顶层感知解析
# ---------------------------------------------------------------------------
def parse_perception(raw: str) -> Perception:
    """
    解析感知 JSON 文本为 Perception。

    抛出 PerceptionParseError 的情形：
    - 非合法 JSON；
    - 顶层不是对象；
    - 缺少或非法 tick / session_id / self.position。
    其余字段一律尽力解析，异常字段逐个丢弃。
    """
    try:
        obj = json.loads(raw)
    except Exception as exc:
        raise PerceptionParseError(f"JSON 解析失败：{exc}") from exc

    if not isinstance(obj, dict):
        raise PerceptionParseError("感知顶层不是 JSON 对象")

    tick = _as_int(obj.get("tick"))
    session_id = obj.get("session_id")
    if tick is None:
        raise PerceptionParseError("缺少或非法 tick")
    if not isinstance(session_id, str):
        raise PerceptionParseError("缺少或非法 session_id（必须为字符串）")

    s = obj.get("self")
    if not isinstance(s, dict):
        raise PerceptionParseError("缺少 self 对象")
    self_xy = _as_xy(s.get("position"))
    if self_xy is None:
        raise PerceptionParseError("self.position 必须为 [x, y] 整数数组")

    # ---- self ----
    la_raw = s.get("last_action")
    last_action: Optional[LastAction] = None
    if isinstance(la_raw, dict):
        last_action = LastAction(
            tick=_as_int(la_raw.get("tick")),
            action=la_raw.get("action") if isinstance(la_raw.get("action"), str) else None,
            moved=_as_bool(la_raw.get("moved"), False),
            effective_direction=(
                la_raw.get("effective_direction")
                if isinstance(la_raw.get("effective_direction"), str) else None
            ),
            error=la_raw.get("error") if isinstance(la_raw.get("error"), str) else None,
        )

    me = SelfState(
        team=s.get("team") if isinstance(s.get("team"), str) else "",
        robot_id=s.get("robot_id") if isinstance(s.get("robot_id"), str) else "",
        position=self_xy,
        hp=_as_int(s.get("hp"), 0) or 0,
        alive=_as_bool(s.get("alive"), True),
        protected=_as_bool(s.get("protected"), False),
        respawn_in=_as_int(s.get("respawn_in"), 0) or 0,
        position_reason=(
            s.get("position_reason")
            if isinstance(s.get("position_reason"), str) else None
        ),
        last_action=last_action,
    )

    # ---- visible_entities（逐条尽力解析） ----
    robots: List[RobotEntity] = []
    resources: List[ResourceEntity] = []
    obstacles: List[Obstacle] = []

    entities = obj.get("visible_entities")
    if not isinstance(entities, list):
        entities = []

    for ent in entities:
        if not isinstance(ent, dict):
            continue
        etype = ent.get("type")
        if etype == C.ENTITY_ROBOT:
            abs_xy, _ = parse_relative_position(ent.get("position"), self_xy)
            if abs_xy is None:
                continue
            robots.append(RobotEntity(
                id=ent.get("id") if isinstance(ent.get("id"), str) else "",
                team=ent.get("team") if isinstance(ent.get("team"), str) else "",
                robot_id=ent.get("robot_id") if isinstance(ent.get("robot_id"), str) else "",
                position=abs_xy,
                hp=_as_int(ent.get("hp"), 0) or 0,
                protected=_as_bool(ent.get("protected"), False),
                last_seen_tick=tick,
            ))
        elif etype in (C.ENTITY_RESOURCE_NORMAL, C.ENTITY_RESOURCE_HIGH):
            abs_xy, _ = parse_relative_position(ent.get("position"), self_xy)
            if abs_xy is None:
                continue
            value = _as_int(ent.get("value"), 0) or 0
            resources.append(ResourceEntity(
                id=ent.get("id") if isinstance(ent.get("id"), str) else "",
                position=abs_xy,
                value=value,
                is_high=value >= C.RESOURCE_HIGH_THRESHOLD,
                last_seen_tick=tick,
            ))
        elif etype == C.ENTITY_OBSTACLE:
            abs_xy, _ = parse_relative_position(ent.get("position"), self_xy)
            if abs_xy is None:
                continue
            obstacles.append(Obstacle(position=abs_xy, last_seen_tick=tick))
        else:
            # 未知实体类型：忽略，不报错（保持前向兼容）
            continue

    # ---- score（裁判结算值：态势判断用；invalid_actions 作自检信号）----
    sc = obj.get("score") if isinstance(obj.get("score"), dict) else {}
    score = Score(
        resource_value=_as_int(sc.get("resource_value"), 0) or 0,
        effective_damage=_as_float(sc.get("effective_damage"), 0.0) or 0.0,
        mission_completed=_as_int(sc.get("mission_completed"), 0) or 0,
        invalid_actions=_as_int(sc.get("invalid_actions"), 0) or 0,
    )

    # ---- missions（结构化任务；任务数据与随机事件文本分别处理） ----
    missions: List[Mission] = []
    mraw = obj.get("missions")
    if isinstance(mraw, list):
        for m in mraw:
            if not isinstance(m, dict):
                continue
            mtype = m.get("type")
            mid = m.get("id")
            if not isinstance(mid, str):
                continue
            pos = _as_xy(m.get("position")) if "position" in m else None
            missions.append(Mission(
                id=mid,
                type=mtype if isinstance(mtype, str) else "",
                target=_as_float(m.get("target")) if m.get("target") is not None else None,
                position=pos,
            ))

    # ---- map ----
    mp = obj.get("map") if isinstance(obj.get("map"), dict) else {}
    map_w = _as_int(mp.get("width"), 0) or 0
    map_h = _as_int(mp.get("height"), 0) or 0

    # ---- 阶段推断（规则第 3 节） ----
    # 第一阶段动作 Tick 0–99，第二阶段 500 Tick。
    # 注意：地图尺寸不能区分阶段（本站 stage1/stage2 地图都是 24×24）；
    # 视野内出现机器人可判定第二阶段；tick ≥ 100 时第一阶段已结束，必为第二阶段。
    # 保守取舍：tick < 100 且未见机器人时按第一阶段处理（只发 MOVE/WAIT），
    # 避免在真正的第一阶段误发 ATTACK/DEFEND 造成无效动作扣分。
    if tick >= 100 or robots:
        stage = 2
    else:
        stage = 1

    return Perception(
        tick=tick,
        session_id=session_id,
        vision_radius=_as_int(obj.get("vision_radius"), 0) or 0,
        stage=stage,
        me=me,
        robots=robots,
        resources=resources,
        obstacles=obstacles,
        score=score,
        missions=missions,
        map_w=map_w,
        map_h=map_h,
    )
