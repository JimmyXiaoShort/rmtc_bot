# -*- coding: utf-8 -*-
"""
通信层：ROS 2 节点封装（连接 / 订阅 / 发布 / QoS / 去重 / 断线重连）。

【重要事实】本赛事使用 ROS 2 话题（std_msgs/msg/String），
而非 MQTT。任务描述中的「MQTT 客户端」在本赛事中对应实现为
ROS 2 rclpy 的订阅者 / 发布者。若团队本地另用 MQTT 做调试旁路，
只能作为内部通道，不得接入比赛接口。

QoS 严格对齐协议第 2 节：
    感知、动作       RELIABLE / VOLATILE        / KEEP_LAST(1)
    注册、任务       RELIABLE / TRANSIENT_LOCAL / KEEP_LAST(1)
    事件公告         RELIABLE / TRANSIENT_LOCAL / KEEP_LAST(128)

设计要点：
- 订阅回调只做「保存最新」，不做业务处理，避免阻塞 DDS 回调线程；
- 感知用「取走即清空」保证同 Tick 重发不重复推进策略；
- 注册发布定期重发（注册发布者须保持运行，裁判接入时接收保留消息）。
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from typing import Deque, List, Optional

try:  # ROS 2 运行期导入；单元测试环境可无 rclpy
    import rclpy
    from rclpy.qos import (
        DurabilityPolicy,
        HistoryPolicy,
        QoSProfile,
        ReliabilityPolicy,
    )
    from std_msgs.msg import String

    _ROS_AVAILABLE = True
except Exception:  # pragma: no cover - 无 ROS 环境
    rclpy = None  # type: ignore
    String = None  # type: ignore
    _ROS_AVAILABLE = False

    class QoSProfile:  # type: ignore
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _Policy:  # type: ignore
        def __init__(self, name):
            self.name = name

    ReliabilityPolicy = _Policy("reliability")  # type: ignore
    DurabilityPolicy = _Policy("durability")  # type: ignore
    HistoryPolicy = _Policy("history")  # type: ignore

from .. import constants as C

log = logging.getLogger("rmtc.comm")


def build_qos_perception_action() -> QoSProfile:
    """感知 / 动作 QoS：RELIABLE、VOLATILE、KEEP_LAST(1)。"""
    return QoSProfile(
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
        history=HistoryPolicy.KEEP_LAST,
        depth=C.QOS_DEPTH_PERCEPTION_ACTION,
    )


def build_qos_registry_mission() -> QoSProfile:
    """注册 / 任务 QoS：RELIABLE、TRANSIENT_LOCAL、KEEP_LAST(1)。"""
    return QoSProfile(
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
        history=HistoryPolicy.KEEP_LAST,
        depth=C.QOS_DEPTH_REGISTRY_MISSION,
    )


def build_qos_event() -> QoSProfile:
    """事件公告 QoS：RELIABLE、TRANSIENT_LOCAL、KEEP_LAST(128)。"""
    return QoSProfile(
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
        history=HistoryPolicy.KEEP_LAST,
        depth=C.QOS_DEPTH_EVENT,
    )


class RosBridge:
    """ROS 2 通信桥。管理与裁判机的全部公开话题。

    不是独立节点：复用调用方传入的 Node 创建发布者/订阅者/定时器，
    保证「只有一个节点需要 spin」，避免订阅回调永不执行。
    """

    def __init__(self, node, cfg) -> None:
        if not _ROS_AVAILABLE:
            raise RuntimeError("未检测到 rclpy：请在 ROS 2 Humble 环境中运行")

        self.node = node
        self.cfg = cfg

        # ---- 状态缓存（回调线程写、主循环读；用锁保护） ----
        self._lock = threading.Lock()
        self._latest_perception: Optional[str] = None
        self._event_queue: Deque[str] = deque(maxlen=512)
        self._mission_text: str = ""

        # ---- 发布者 ----
        self.pub_action = self.node.create_publisher(
            String, cfg.action_topic, build_qos_perception_action()
        )
        self.pub_registration = self.node.create_publisher(
            String, cfg.registration_topic, build_qos_registry_mission()
        )

        # ---- 订阅者 ----
        self.node.create_subscription(
            String, cfg.perception_topic, self._on_perception, build_qos_perception_action()
        )
        self.node.create_subscription(
            String, C.TOPIC_EVENT_TEXT, self._on_event, build_qos_event()
        )
        self.node.create_subscription(
            String, C.TOPIC_MISSION, self._on_mission, build_qos_registry_mission()
        )

        # 注册消息定期重发（协议第 2 节：注册发布者须保持运行）
        self._reg_timer = self.node.create_timer(5.0, self.publish_registration)

        # 注册消息先发一次，避免裁判接入后 5 秒内看不到队名
        self.publish_registration()

        log.info(
            "RosBridge 就绪 seat=%s 订阅=%s 发布=%s",
            cfg.seat, cfg.perception_topic, cfg.action_topic,
        )

    # ------------------------------------------------------------------
    # 订阅回调：只保存，不处理
    # ------------------------------------------------------------------
    def _on_perception(self, msg) -> None:
        with self._lock:
            self._latest_perception = msg.data

    def _on_event(self, msg) -> None:
        with self._lock:
            self._event_queue.append(msg.data)

    def _on_mission(self, msg) -> None:
        with self._lock:
            # /referee/mission 为纯文本任务公告（协议第 2 节：任务与随机事件公告 data 为纯文本）
            self._mission_text = msg.data

    # ------------------------------------------------------------------
    # 供主循环调用的访问接口
    # ------------------------------------------------------------------
    def take_perception(self) -> Optional[str]:
        """原子取走最新感知（取走即清空），保证同 Tick 重发不重复推进策略。"""
        with self._lock:
            data, self._latest_perception = self._latest_perception, None
            return data

    def drain_events(self) -> List[str]:
        """取走全部待处理事件公告原文。"""
        with self._lock:
            out = list(self._event_queue)
            self._event_queue.clear()
            return out

    def take_mission_text(self) -> str:
        """取走任务公告文本（纯文本；结构化任务由感知 missions 提供）。"""
        with self._lock:
            data, self._mission_text = self._mission_text, ""
            return data

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------
    def publish_registration(self) -> None:
        """发布队伍注册消息：{"team": "<席位>", "name": "<显示名称>"}。"""
        try:
            payload = json.dumps(
                {"team": self.cfg.seat, "name": self.cfg.team_name},
                ensure_ascii=False, separators=(",", ":"),
            )
            m = String()
            m.data = payload
            self.pub_registration.publish(m)
            log.debug("已发布注册：%s", payload)
        except Exception as exc:  # 发布失败不得中断程序
            log.warning("发布注册失败：%s", exc)

    def publish_action(self, action_json: str) -> bool:
        """发布动作 JSON（调用方负责已完成 schema 校验）。"""
        try:
            m = String()
            m.data = action_json
            self.pub_action.publish(m)
            return True
        except Exception as exc:
            log.error("发布动作失败：%s", exc)
            return False
