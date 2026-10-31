# -*- coding: utf-8 -*-
"""测试动作 JSON schema（协议第 4 节）。"""

import json

from rmtc_bot.action.builder import ActionBuilder
from rmtc_bot.models import Intent, Perception, SelfState


def _make_perception(stage: int = 2) -> Perception:
    return Perception(
        tick=147,
        session_id="test-session",
        stage=stage,
        vision_radius=4,
        map_w=24,
        map_h=24,
        me=SelfState(team="A", robot_id="A1", position=(10, 10), hp=100, alive=True, respawn_in=0, protected=False),
        robots=[],
        resources=[],
        obstacles=[],
        missions=[],
        score=None,
    )


def test_action_move_has_direction():
    builder = ActionBuilder(cfg=None)
    p = _make_perception(stage=2)
    intent = Intent(kind="move", direction="right")
    msg = builder.build(intent, p)
    obj = json.loads(msg)
    assert obj["action"] == "move"
    assert obj["direction"] == "right"
    assert obj["tick"] == 147
    assert obj["session_id"] == "test-session"


def test_action_defend_no_direction():
    builder = ActionBuilder(cfg=None)
    p = _make_perception(stage=2)
    intent = Intent(kind="defend")
    msg = builder.build(intent, p)
    obj = json.loads(msg)
    assert obj["action"] == "defend"
    assert "direction" not in obj


def test_phase1_rejects_attack():
    """第一阶段仅允许 move/wait，attack 应被丢弃。"""
    builder = ActionBuilder(cfg=None)
    p = _make_perception(stage=1)
    intent = Intent(kind="attack", direction="up")
    assert builder.build(intent, p) is None
