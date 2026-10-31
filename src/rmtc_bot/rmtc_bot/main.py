# -*- coding: utf-8 -*-
"""
主 ROS 2 节点（rclpy）。

职责：
- 订阅 /team_X/perception、/referee/event_text、/referee/mission；
- 发布 /team_X/action、/team_X/registration（注册发布者须保持运行，协议第 2 节）；
- 每帧：世界模型更新 → 事件消费 → FSM 决策 → 动作构造 → 发布；
- 双向验证：核对感知中 self.last_action（协议第 1.2 节），本地留痕；
- 场次切换（session_id 变化）自动重置（WorldModel / FSM / 事件栈）。
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional

import rclpy
from rclpy.node import Node

from .action import ActionBuilder
from .comm.ros_bridge import RosBridge
from .config import Config, PARAM_ENV_DEFAULTS, load_config
from .decision.fsm import DecisionFSM
from .decision.event_policy import EventPolicy
from .llm.gateway import LLMGateway
from .models import Perception
from .perception.world_model import WorldModel
from .decision.registry import DecisionContext
from .perception.parser import PerceptionParseError, parse_perception

log = logging.getLogger("rmtc.main")


def _configure_logging() -> None:
    """把 rmtc.* 日志输出到 stderr（联调排查依赖这些日志）。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(args=None) -> int:
    _configure_logging()
    rclpy.init(args=args)
    node = RmtcBotNode()          # 配置在节点内从启动参数读取
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


class RmtcBotNode(Node):
    """RMTC 参赛机器人主节点。

    以 10 ms 定时器轮询 RosBridge 的缓存（回调线程只保存最新，
    主循环取走即清空），保证同 Tick 重发不重复推进策略。
    """

    def __init__(self, cfg: Optional[Config] = None) -> None:
        super().__init__("rmtc_bot_node")
        if cfg is None:
            cfg = load_config(params=self._declare_params())
        self.cfg = cfg
        self.bridge = RosBridge(self, cfg)   # 复用本节点创建话题（只 spin 本节点）
        self.world = WorldModel(cfg)   # 必须传入 cfg：否则堡垒模型不创建（规则 §8.5）
        self.llm = LLMGateway(cfg)
        self.event_policy = EventPolicy(self.world, self.llm, cfg)
        self.ctx = DecisionContext(self.world, None, self.event_policy, cfg)
        self.fsm = DecisionFSM(cfg, self.ctx)
        self.actions = ActionBuilder(cfg)
        self._last_tick = -1

        # 10 ms 轮询：感知/事件/任务
        self.create_timer(0.01, self._poll)
        log.info("RMTC bot 节点就绪，seat=%s decision_type=%s",
                 cfg.seat, cfg.decision_type)

    # ------------------------------------------------------------------
    def _declare_params(self) -> Dict[str, object]:
        """声明全部接入参数并返回当前取值。

        默认值取环境变量（有则用之）否则取内置默认；`ros2 launch` / `ros2 run`
        通过 `-p name:=value` 传入的值会覆盖默认值。
        """
        for name, (env_name, default) in PARAM_ENV_DEFAULTS.items():
            self.declare_parameter(name, os.environ.get(env_name, default))
        return {name: self.get_parameter(name).value for name in PARAM_ENV_DEFAULTS}

    # ------------------------------------------------------------------
    def _poll(self) -> None:
        """定时器主循环：处理待收感知、事件、任务文本。"""
        # 1) 事件公告（先于策略消费，内部规则/LLM 分支）
        for text in self.bridge.drain_events():
            self.event_policy.on_event_text(text, tick=self.world.tick)

        # 2) 任务公告（纯文本；结构化任务由感知 missions 提供）
        mtext = self.bridge.take_mission_text()
        if mtext:
            self.event_policy.on_mission_text(mtext, tick=self.world.tick)

        # 3) 感知 → 决策 → 动作
        raw = self.bridge.take_perception()
        if raw is None:
            return
        try:
            p = parse_perception(raw)
        except PerceptionParseError as exc:
            log.error("感知解析失败：%s（原文已丢弃）", exc)
            return

        prev_session = self.world.session_id

        # 场次切换（session_id 变化）→ 重置 tick 去重基准，避免跨场次 tick 碰撞
        # （规则 §5.1：同一场次中早于当前 Tick 的感知应丢弃）
        if prev_session is not None and prev_session != p.session_id:
            self._last_tick = -1

        # 同 Tick 重发 或 早于当前 Tick 的感知 → 丢弃（规则 §5.1）
        if p.tick <= self._last_tick:
            return
        self._last_tick = p.tick

        self.world.update(p)          # 内部已在 session 变化时重建世界模型（含堡垒）

        # 场次切换 → 重置事件栈（避免上一场公告残留影响决策）
        if prev_session is not None and prev_session != p.session_id:
            log.info("session 切换 %s -> %s，重置事件栈", prev_session, p.session_id)
            self.event_policy.reset()
        self.world.session_id = p.session_id

        # 双向验证留痕：核对上一 Tick 动作回执
        self._check_last_action(p)

        intent = self.fsm.step(p)
        msg = self.actions.build(intent, p)
        if msg is not None:
            self.bridge.publish_action(msg)

    def _check_last_action(self, p: Perception) -> None:
        """协议第 1.2 节：核对 self.last_action，确认动作被裁判处理。"""
        la = p.me.last_action if p.me is not None else None
        if la is None:
            return
        if la.action is not None and la.moved:
            log.debug("动作回执 ok：tick=%s action=%s dir=%s",
                      la.tick, la.action, la.effective_direction)
        elif la.error:
            log.warning("动作回执 error：tick=%s action=%s err=%s",
                        la.tick, la.action, la.error)

if __name__ == "__main__":
    raise SystemExit(main())
