# -*- coding: utf-8 -*-
"""测试场次切换（session_id 变化）时的重置逻辑。"""

from rmtc_bot.models import Perception, SelfState
from rmtc_bot.perception.world_model import WorldModel


def _make_perception(session_id: str, tick: int) -> Perception:
    return Perception(
        tick=tick,
        session_id=session_id,
        stage=2,
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


def test_session_change_resets_world():
    """session_id 变化时，世界模型应重置（记忆清空、tick 归零）。"""
    wm = WorldModel(cfg=None)
    p1 = _make_perception("session-A", tick=50)
    wm.update(p1)
    assert wm.session_id == "session-A"
    assert wm.tick == 50

    # 模拟记忆写入
    wm.visited.add((10, 10))

    p2 = _make_perception("session-B", tick=0)
    wm.update(p2)
    assert wm.session_id == "session-B"
    assert wm.tick == 0
    # 场次切换后记忆清空：visited 仅包含 update 时记录的当前位置
    assert wm.visited == {(10, 10)}
