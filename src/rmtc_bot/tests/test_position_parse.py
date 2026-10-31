# -*- coding: utf-8 -*-
"""测试 9×9 相对位置图解析（协议第 3 节）。"""

from rmtc_bot.perception.parser import parse_relative_position


def test_relative_position_right_up():
    """物体在自身右一格、上两格 → 位移 [1, -2]。"""
    pos_str = (
        "000000000\n"
        "000000000\n"
        "000002000\n"
        "000000000\n"
        "000010000\n"
        "000000000\n"
        "000000000\n"
        "000000000\n"
        "000000000"
    )
    abs_xy, rel = parse_relative_position(pos_str, (10, 10))
    assert rel == (1, -2), rel
    assert abs_xy == (11, 8), abs_xy


def test_relative_position_same_cell():
    """物体与自身同格 → 位移 [0, 0]，整图无 '2'。"""
    pos_str = "\n".join(["000000000"] * 4 + ["000010000"] + ["000000000"] * 4)
    abs_xy, rel = parse_relative_position(pos_str, (5, 5))
    assert rel == (0, 0), rel
    assert abs_xy == (5, 5), abs_xy


def test_relative_position_invalid():
    """非法位置串 → 返回 (None, None)。"""
    abs_xy, rel = parse_relative_position("not-a-grid", (0, 0))
    assert abs_xy is None and rel is None
