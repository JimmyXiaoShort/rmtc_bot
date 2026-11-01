#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地自测脚本（不依赖测试裁判、不依赖 ROS 网络）。

用途：在真实上场/联调之前，用合成感知 JSON 走通
      感知解析 → 世界模型 → 决策状态机 → 动作构造
整条链路，检查：
  1) 第一阶段：能找到资源并朝其移动（仅 move/wait）；
  2) 第二阶段：贴脸且有敌机时能 attack；
  3) 堡垒（§8.5）：低血时朝最近的堡垒格移动；
  4) 动作 JSON 合规：小写 action/direction，且 tick/session_id 原样回填。

说明：本脚本不是 zip 内原有文件，是配套文档《06_测试操作说明》提供的自测工具，
      可另存到 rmtc_bot/scripts/ 下运行；它只导入纯 Python 模块，无需 rclpy。
用法：
    cd contestant_ws/src/rmtc_bot
    python3 scripts/selftest_local.py
"""
from __future__ import annotations

import json
import os
import sys

# 让脚本既能在包根目录、也能在 scripts/ 下被找到地导入 rmtc_bot
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

# 自测用的最小环境（不写死任何真实令牌；仅用于本地跑通配置加载）
os.environ.setdefault("RMTC_SEAT", "A")
os.environ.setdefault("RMTC_TEAM_NAME", "自测队")
os.environ.setdefault("RMTC_GATEWAY", "http://127.0.0.1:8080")
os.environ.setdefault("RMTC_TEAM_TOKEN", "")   # 留空：自测不调用 LLM，走规则策略
os.environ.setdefault("ROS_DOMAIN_ID", "27")

from rmtc_bot.action import ActionBuilder                      # noqa: E402
from rmtc_bot.config import load_config                    # noqa: E402
from rmtc_bot.decision.event_policy import EventPolicy          # noqa: E402
from rmtc_bot.decision.fsm import DecisionFSM               # noqa: E402
from rmtc_bot.decision.registry import DecisionContext      # noqa: E402
from rmtc_bot.llm.gateway import LLMGateway                 # noqa: E402
from rmtc_bot.perception.parser import parse_perception     # noqa: E402
from rmtc_bot.perception.world_model import WorldModel      # noqa: E402


def make_grid(dx: int = 0, dy: int = 0) -> str:
    """生成 9x9 相对位置图：'1' 固定在中心(第5行第5列)，'2' 为物体。

    dx/dy 为物体相对本车的位移（列右为正、行下为正）；dx=dy=0 表示同格，
    此时图中不出现 '2'（按协议：中心保留 '1'）。
    """
    rows = []
    for r in range(9):
        row = ["0"] * 9
        if r == 4 and dx == 0 and dy == 0:
            row[4] = "1"
        else:
            row[4] = "1"
        rows.append(row)
    if not (dx == 0 and dy == 0):
        rows[4 + dy][4 + dx] = "2"
    return "\n".join("".join(r) for r in rows)


def perception(stage=1, tick=5, session="self-1", self_xy=(10, 10), hp=100,
               alive=True, entities=None, map_wh=(20, 20)):
    return json.dumps({
        "tick": tick,
        "session_id": session,
        "vision_radius": 4,
        "map": {"width": map_wh[0], "height": map_wh[1]},
        "self": {
            "team": "A", "robot_id": "A",
            "position": list(self_xy),
            "hp": hp, "alive": alive,
            "defending": False, "protected": False, "respawn_in": 0,
            "last_action": {"tick": tick - 1, "action": "move", "moved": True,
                            "effective_direction": "right"},
        },
        "visible_entities": entities or [],
        "score": {"resource_value": 0, "effective_damage": 0.0,
                  "mission_completed": 0, "mission_completed_ids": [],
                  "invalid_actions": 0},
        "missions": [],
    })


def run_case(title, raw):
    cfg = load_config()
    world = WorldModel(cfg)   # 与 main.py 一致：带 cfg 才会创建堡垒模型
    llm = LLMGateway(cfg)
    event_policy = EventPolicy(world, llm, cfg)
    ctx = DecisionContext(world, None, event_policy, cfg)
    fsm = DecisionFSM(cfg, ctx)
    actions = ActionBuilder(cfg)

    p = parse_perception(raw)
    world.update(p)
    intent = fsm.step(p)
    msg = actions.build(intent, p)

    print(f"\n=== {title} ===")
    print(f"stage={p.stage} tick={p.tick} hp={p.me.hp} pos={p.me.position}")
    print(f"intent = {intent}")
    print(f"action_json = {msg}")
    return p, intent, msg


def main() -> int:
    ok = True

    # 情况 1：第一阶段，右侧 1 格有普通资源 → 期望 move right
    raw1 = perception(
        stage=1, tick=5, session="self-1", self_xy=(10, 10),
        entities=[{"type": "resource_normal", "id": "r1",
                   "position": make_grid(1, 0), "value": 10}],
        map_wh=(20, 20),
    )
    p1, i1, m1 = run_case("情况1 第一阶段·追资源", raw1)
    if not (i1.kind == "move" and i1.direction == "right"):
        ok = False
        print("!! 期望 move right，实际", i1)
    if m1 is not None:
        d = json.loads(m1)
        if d["tick"] != p1.tick or d["session_id"] != p1.session_id:
            ok = False
            print("!! tick/session_id 未原样回填")

    # 情况 2：第二阶段，左侧 1 格有敌机 → 期望 attack left
    raw2 = perception(
        stage=2, tick=60, session="self-2", self_xy=(10, 10), hp=100,
        entities=[{"type": "robot", "id": "B", "team": "B", "robot_id": "B",
                   "position": make_grid(-1, 0), "hp": 60,
                   "defending": False, "protected": False}],
        map_wh=(24, 24),
    )
    p2, i2, m2 = run_case("情况2 第二阶段·贴脸攻击", raw2)
    if i2.kind != "attack":
        ok = False
        print("!! 期望 attack，实际", i2)

    # 情况 3：第二阶段低血(-堡垒)，本车 (8,12)、堡垒 [11..13]x[11..13] → 期望朝右移动
    # tick 取 120（≥100）以确保按第二阶段解析：地图尺寸不能区分阶段。
    raw3 = perception(
        stage=2, tick=120, session="self-3", self_xy=(8, 12), hp=20,
        entities=[], map_wh=(24, 24),
    )
    p3, i3, m3 = run_case("情况3 第二阶段·低血回撤堡垒", raw3)
    if not (i3.kind == "move" and i3.direction == "right"):
        ok = False
        print("!! 期望 move right(朝堡垒)，实际", i3)
    if not str(i3.reason).startswith("fort-"):
        ok = False
        print("!! 期望走堡垒策略(reason 以 fort- 开头)，实际", i3.reason)

    print("\n" + ("全部自测通过 ✔" if ok else "存在未通过项 ✘"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
