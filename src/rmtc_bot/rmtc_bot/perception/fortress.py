# -*- coding: utf-8 -*-
"""
堡垒（Fortress）模型 —— 规则 v1.1 §8.5「天下无双」。

【官方原文（赛事规则 v1.1 §8.5 天下无双，逐字）】
  「地图中心生成 3×3 堡垒，存在 50 Tick。每 Tick 战斗后存活且移动后位于堡垒内才
    累计驻留，连续 10 Tick 达标；离开或死亡清空尚未完成的驻留进度。达标在该 Tick
    战斗后授予增益，之后须主动提交 ATTACK 和合法方向。ATTACK 攻击自身横纵各 ±2 格的
    5×5 方形内全部存活敌人，包含斜向、不受障碍遮挡，方向不限制目标集合。对非防御
    目标伤害为 50 HP，防御目标 10 HP，出生保护目标 0 HP。已获得增益后可离开堡垒，
    死亡复活也保留，但堡垒结束时统一失效。与易伤叠加时按其倍率计算。」

【同 Tick 顺序（§5.2 第 7 步）】「为存活车辆累计堡垒驻留，结算有效回血。」
  → 官方把「累计驻留」与「有效回血」放在战斗之后、死亡扣除（第 8 步）之前。

【本模型职责】
  1) 堡垒几何：矩形范围 → 9 个格；距离/最近格/包含判定；
  2) 驻留累计：仅依据感知可观测事实（存活 + 是否成功移动 + 移动后是否在堡垒内）；
  3) 增益状态：连续驻留达标后标记 buff_granted（增益本身的伤害效果由裁判结算，
     本程序只据 perceived 的 self.last_action / hp 与规则做保守推断）；
  4) 回血观测：不假设每 Tick 回血量（原文未明确），仅记录堡垒内观测到的 HP 增量。

【「原文未明确」清单（均以配置项兜底，不臆造数值）】
  (a) 每 Tick 回血量 / 累计上限        → 不假设，仅观测。
  (b) 原地 WAIT 是否累计驻留           → 默认「需成功移动」(fort_require_move)。
  (c) 50 Tick 生命周期的起算 Tick      → 默认「全程有效」(fort_lifetime_ticks=0 视为全程)。
  (d) 堡垒具体坐标                     → 默认按感知地图中心自动居中（fort_auto_center）。
"""

from __future__ import annotations

import logging
from typing import List, Optional, Set, Tuple

from .. import constants as C
from ..models import Perception, XY

log = logging.getLogger("rmtc.fortress")


def manhattan(a: XY, b: XY) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


class FortressModel:
    """堡垒几何 + 驻留/回血观测状态机（每个场次重建）。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        # ---- 几何 ----
        self.x0 = int(getattr(cfg, "fort_x0", C.FORT_X0_DEFAULT))
        self.x1 = int(getattr(cfg, "fort_x1", C.FORT_X1_DEFAULT))
        self.y0 = int(getattr(cfg, "fort_y0", C.FORT_Y0_DEFAULT))
        self.y1 = int(getattr(cfg, "fort_y1", C.FORT_Y1_DEFAULT))
        self.auto_center = bool(getattr(cfg, "fort_auto_center", False))
        self._map_dims: Tuple[int, int] = (0, 0)   # 已标定的地图尺寸（避免每 Tick 重算/刷日志）
        # 固定 3×3（选手提供赛场信息：[11,11]–[13,13] 共 9 格）
        self.cells: List[XY] = C.fortress_cells(self.x0, self.x1, self.y0, self.y1)
        self.cell_set: Set[XY] = set(self.cells)

        # ---- 规则参数 ----
        self.need = int(getattr(cfg, "fort_residence_ticks", C.FORT_RESIDENCE_TICKS))
        self.lifetime = int(getattr(cfg, "fort_lifetime_ticks", C.FORT_LIFETIME_TICKS))
        self.require_move = bool(getattr(cfg, "fort_require_move", True))

        # ---- 运行状态 ----
        self.streak: int = 0              # 当前连续驻留 Tick 数
        self.buff_granted: bool = False   # 是否已达标获得增益
        self.inside: bool = False         # 本帧（更新后）是否位于堡垒内
        self.start_tick: Optional[int] = None   # 首次侦测到堡垒的 Tick（原文未明确生成时刻）
        self.last_hp: Optional[int] = None       # 用于观测回血
        self.observed_heals: List[int] = []      # 堡垒内观测到的正 HP 增量（答辩/调参用）
        self.log: List[str] = []                  # 关键事件留痕

    # ------------------------------------------------------------------
    # 场次 / 地图标定
    # ------------------------------------------------------------------
    def rebind_map(self, map_w: int, map_h: int) -> None:
        """按实际地图尺寸重新标定（仅当开启自动居中）。

        不硬编码 20×20 / 24×24：尺寸来自本队感知的 map 字段。
        仅在尺寸变化时重算，避免每 Tick 重复计算与日志刷屏。
        """
        if not self.auto_center or map_w <= 0 or map_h <= 0:
            return
        if (map_w, map_h) == self._map_dims:
            return
        self._map_dims = (map_w, map_h)
        cells = C.center_3x3_cells(map_w, map_h)
        self.cells = cells
        self.cell_set = set(cells)
        log.info("堡垒按地图 %dx%d 自动居中：%s..%s", map_w, map_h, cells[0], cells[-1])

    def reset_for_session(self) -> None:
        self.streak = 0
        self.buff_granted = False
        self.inside = False
        self.start_tick = None
        self.last_hp = None
        self.observed_heals = []
        self.log = []

    # ------------------------------------------------------------------
    # 几何查询
    # ------------------------------------------------------------------
    def contains(self, xy: Optional[XY]) -> bool:
        return xy is not None and tuple(xy) in self.cell_set

    def nearest_cell(self, xy: XY) -> XY:
        """返回堡垒 9 格中距 xy 曼哈顿最近的格（并列取序号最小，保证确定性）。"""
        return min(self.cells, key=lambda c: (manhattan(xy, c), c[1], c[0]))

    def distance(self, xy: XY) -> int:
        return manhattan(xy, self.nearest_cell(xy))

    def is_active(self, tick: int) -> bool:
        """堡垒是否仍在有效期。

        原文未明确 50 Tick 的起算点。默认 lifetime<=0 或 start_tick 未知 → 视为全程有效，
        以避免因无法观测生成时刻而误判失效（保守、可解释）。
        """
        if self.lifetime <= 0 or self.start_tick is None:
            return True
        return (tick - self.start_tick) < self.lifetime

    # ------------------------------------------------------------------
    # 每帧更新（由 main/世界模型在「新帧」时调用）
    # ------------------------------------------------------------------
    def update(self, p: Perception, moved: bool, tick: int) -> None:
        """
        moved：上一 Tick 动作是否成功移动（来自 p.me.last_action.moved）。

        驻留累计（严格对齐 §8.5）：
          条件 = 存活（alive 且 respawn_in==0）且（成功移动）且（移动后位于堡垒内）；
          不满足 → 清空未完成进度（离开/死亡/未成功移动）。
        """
        me = p.me
        if p.map_w and p.map_h:
            self.rebind_map(p.map_w, p.map_h)

        if me is None:
            self.inside = False
            self.streak = 0
            return

        alive = bool(me.alive) and me.respawn_in == 0
        self.inside = alive and self.contains(me.position)

        if self.inside and self.start_tick is None:
            self.start_tick = tick        # 首次进入视为可观测起点（原文未明确）

        # ---- 回血观测（不假设数值） ----
        if self.last_hp is not None and self.inside and me.hp > self.last_hp:
            self.observed_heals.append(me.hp - self.last_hp)
        self.last_hp = me.hp

        # ---- 驻留累计 ----
        effective_move = moved or (not self.require_move)
        if self.inside and effective_move and self.is_active(tick):
            self.streak += 1
            if self.streak == self.need:
                self.buff_granted = True
                msg = f"tick={tick} 堡垒驻留达标({self.need}) → 获得增益（天下无双）"
                log.info(msg)
                self.log.append(msg)
        else:
            if self.streak > 0 and not self.inside:
                msg = f"tick={tick} 离开堡垒，驻留进度清空（原 {self.streak}）"
                log.info(msg)
                self.log.append(msg)
            self.streak = 0

        # 堡垒结束 → 增益统一失效（§8.5）
        if self.buff_granted and not self.is_active(tick):
            self.buff_granted = False
            msg = f"tick={tick} 堡垒结束 → 增益统一失效"
            log.info(msg)
            self.log.append(msg)

    # ------------------------------------------------------------------
    # 决策辅助
    # ------------------------------------------------------------------
    @property
    def residence_ready(self) -> bool:
        return self.buff_granted or self.streak >= self.need

    def retreat_hp(self) -> int:
        return int(getattr(self.cfg, "fort_retreat_hp", C.FORT_RETREAT_HP_DEFAULT))

    def resume_hp(self) -> int:
        return int(getattr(self.cfg, "fort_resume_hp", C.FORT_RESUME_HP_DEFAULT))

    def next_cell(self, from_xy: XY) -> XY:
        """在当前格之外挑一个堡垒格作为下一步目标（用于「在堡垒内巡航累计驻留」）。

        确定性策略：按索引循环选择，保证不原地停留、可覆盖 9 格。
        """
        if from_xy in self.cell_set:
            i = self.cells.index(from_xy)
            return self.cells[(i + 1) % len(self.cells)]
        return self.nearest_cell(from_xy)

    def status(self) -> str:
        return (f"fort cells=[{self.x0},{self.y0}]-[{self.x1},{self.y1}] "
                f"n={len(self.cells)} inside={self.inside} streak={self.streak}/{self.need} "
                f"buff={self.buff_granted} active={self.is_active(self.start_tick or 0)}")
