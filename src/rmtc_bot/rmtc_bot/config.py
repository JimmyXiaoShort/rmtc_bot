# -*- coding: utf-8 -*-
"""
接入配置加载。

【设计原则】
- 接入信息优先从 ROS 2 启动参数读取（launch 中传参），环境变量作为兜底，
  便于「换场切换」：换场只需改启动命令中的参数，无需改代码。
- ROS_DOMAIN_ID 必须在 rclpy.init 之前设定，只能通过环境变量 / launch 环境设置。
- 令牌严禁写入源码、仓库或公开日志（协议第 1.2 节、第 5 节）。
- decision_config.json 是现场唯一允许修改的文件，且只允许修改 decision_type
  （规则 §2.3）。本模块读取该文件，并对取值做「已登记枚举」校验。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import constants as C

log = logging.getLogger("rmtc.config")

# 现场参数文件名（规则 §2.3 规定该名称，不得更改）
DECISION_CONFIG_FILENAME = "decision_config.json"

# ROS 2 参数名 → (兜底环境变量, 默认值)
# 主节点按此表声明参数；launch 传入的值会覆盖默认值，环境变量用于兼容
# `ros2 run` / `python3 -m` 及自测脚本。
PARAM_ENV_DEFAULTS: Dict[str, tuple] = {
    "seat": ("RMTC_SEAT", C.SEAT_STAGE1),
    "team_name": ("RMTC_TEAM_NAME", "RMTC 队伍"),
    "gateway": ("RMTC_GATEWAY", ""),
    "team_token": ("RMTC_TEAM_TOKEN", ""),
    "referee_ip": ("RMTC_REFEREE_IP", "127.0.0.1"),
    "ai_port": ("RMTC_AI_PORT", str(C.LLM_DEFAULT_PORT)),
    "model": ("RMTC_MODEL", "deepseek-flash"),
    "max_tokens": ("RMTC_MAX_TOKENS", "128"),
    "llm_quota": ("RMTC_LLM_QUOTA", str(C.LLM_DEFAULT_SOFT_QUOTA)),
    "fort_x0": ("RMTC_FORT_X0", str(C.FORT_X0_DEFAULT)),
    "fort_x1": ("RMTC_FORT_X1", str(C.FORT_X1_DEFAULT)),
    "fort_y0": ("RMTC_FORT_Y0", str(C.FORT_Y0_DEFAULT)),
    "fort_y1": ("RMTC_FORT_Y1", str(C.FORT_Y1_DEFAULT)),
    "fort_auto_center": ("RMTC_FORT_AUTO_CENTER", "0"),
    "fort_require_move": ("RMTC_FORT_REQUIRE_MOVE", "1"),
    "fort_retreat_hp": ("RMTC_FORT_RETREAT_HP", str(C.FORT_RETREAT_HP_DEFAULT)),
    "fort_resume_hp": ("RMTC_FORT_RESUME_HP", str(C.FORT_RESUME_HP_DEFAULT)),
    "fort_enemy_policy": ("RMTC_FORT_ENEMY_POLICY", C.FORT_ENEMY_POLICY_DEFAULT),
}


class ConfigError(Exception):
    """配置缺失或非法时抛出（启动期快速失败，避免带病上场）。"""


# ---------------------------------------------------------------------------
# decision_type 枚举登记表（规则 §2.3）
#
# 规则要求：允许切换的决策类型必须在阶段封存前完成实现和封装，
# 并向裁判登记「类型标识、作用及读取方式」。下表即本队的登记内容，
# 编码时与 decision/registry.py 一一对应。
#
# 现场只允许把 decision_config.json 的 decision_type 改成下列键之一；
# 不得夹带坐标、动作、目标 ID、任意指令或新增算法参数。
# ---------------------------------------------------------------------------
DECISION_TYPE_REGISTRY: Dict[str, Dict[str, str]] = {
    "collect": {
        "stage": "1",
        "summary": "第一阶段独立采集（仅 MOVE / WAIT）",
        "detail": "贪心最近资源搜索 + 已知障碍规避；不发送 attack/defend。",
    },
    "combat": {
        "stage": "2",
        "summary": "第二阶段资源争夺 + 任务 + 对抗 + 事件响应",
        "detail": "资源优先、任务导向、按血量/视野威胁决定攻击或防御，事件经 LLM 理解。",
    },
    "auto": {
        "stage": "1/2",
        "summary": "自动判定阶段（默认）",
        "detail": "按 tick 范围、地图尺寸与视野内实体推断阶段，再分派到 collect / combat。",
    },
    "safe": {
        "stage": "2",
        "summary": "保守/降级模式",
        "detail": "不主动交战，优先脱离威胁、就近采集与保命；用于异常或应急降级。",
    },
}

DEFAULT_DECISION_TYPE = "auto"


def _env(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    """读取环境变量；required 且缺失时抛 ConfigError。"""
    val = os.environ.get(name, default)
    if required and (val is None or val == ""):
        raise ConfigError(f"缺少必需的环境变量 {name}")
    return val


def _pick(params: Optional[Dict[str, object]], key: str, env_name: str,
          default: Optional[str] = None) -> Optional[str]:
    """取配置值：启动参数优先，其次环境变量，最后默认值。"""
    if params is not None:
        val = params.get(key)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return _env(env_name, default=default)


def _to_int(value: object, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


@dataclass
class Config:
    """运行期配置（一次启动内只读）。"""

    seat: str                              # 本场席位 A/B/C/D
    team_name: str                         # 显示名称，1–96 UTF-8 字节
    gateway: str                           # LLM 网关基址，如 http://<裁判机IP>:8000
    token: str                             # 本场席位令牌（严禁写入日志/仓库）
    ros_domain_id: int = 27                # 赛前公布值
    ai_port: int = C.LLM_DEFAULT_PORT      # RMTC_AI_PORT，默认 8000
    decision_type: str = DEFAULT_DECISION_TYPE
    decision_types: List[str] = field(default_factory=list)
    # LLM 输出限制（按赛前公告设置；原文未明确具体默认值）
    llm_model: Optional[str] = None
    llm_max_tokens: Optional[int] = None
    # 本地软配额（用于降级；正式以赛前公告 & 网关 429 为准）
    llm_soft_quota: int = C.LLM_DEFAULT_SOFT_QUOTA

    # ---- 堡垒配置（规则 §8.5「天下无双」）----
    # 默认 [11,11]–[13,13] 共 3×3 格；来源为「选手提供的赛场信息」，
    # 官方规则原文未公布具体坐标（仅称「地图中心」）→ 换场按官方公告核对。
    fort_x0: int = C.FORT_X0_DEFAULT
    fort_x1: int = C.FORT_X1_DEFAULT
    fort_y0: int = C.FORT_Y0_DEFAULT
    fort_y1: int = C.FORT_Y1_DEFAULT
    fort_auto_center: bool = False        # True=按实际地图尺寸自动居中 3×3
    fort_lifetime_ticks: int = C.FORT_LIFETIME_TICKS
    fort_residence_ticks: int = C.FORT_RESIDENCE_TICKS
    fort_require_move: bool = True        # 是否必须成功移动才累计驻留（原文未明确）
    fort_retreat_hp: int = C.FORT_RETREAT_HP_DEFAULT
    fort_resume_hp: int = C.FORT_RESUME_HP_DEFAULT
    fort_enemy_policy: str = C.FORT_ENEMY_POLICY_DEFAULT
    fort_heal_per_tick: Optional[int] = None   # 原文未明确：不假设，None=不推断

    # ---- 话题名（由席位推导） ----
    @property
    def perception_topic(self) -> str:
        return C.TOPIC_PERCEPTION_FMT.format(seat=self.seat)

    @property
    def action_topic(self) -> str:
        return C.TOPIC_ACTION_FMT.format(seat=self.seat)

    @property
    def registration_topic(self) -> str:
        return C.TOPIC_REGISTRATION_FMT.format(seat=self.seat)

    @property
    def chat_url(self) -> str:
        return self.gateway.rstrip("/") + C.LLM_CHAT_PATH

    @property
    def health_url(self) -> str:
        return self.gateway.rstrip("/") + C.LLM_HEALTH_PATH


def _read_decision_type(path: str) -> str:
    """读取 decision_config.json 的 decision_type，并校验是否为已登记枚举。"""
    if not os.path.isfile(path):
        log.warning("未找到 %s，使用默认 decision_type=%s", path, DEFAULT_DECISION_TYPE)
        return DEFAULT_DECISION_TYPE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:  # 文件损坏不得导致崩溃
        log.error("解析 %s 失败：%s，回退默认值", path, exc)
        return DEFAULT_DECISION_TYPE

    dt = data.get("decision_type", DEFAULT_DECISION_TYPE)
    if not isinstance(dt, str):
        log.error("decision_type 非字符串，回退默认值")
        return DEFAULT_DECISION_TYPE
    if dt not in DECISION_TYPE_REGISTRY:
        # 未登记枚举：拒绝，回退到安全的自动判定，避免现场误配置导致违规动作
        log.error("decision_type=%r 不在已登记枚举中，回退 %s", dt, DEFAULT_DECISION_TYPE)
        return DEFAULT_DECISION_TYPE
    return dt


def _resolve_config_path(decision_config_path: Optional[str]) -> Optional[str]:
    """定位 decision_config.json。

    查找顺序：
      1. 调用方显式传入的路径（直接使用）；
      2. 当前工作目录下的 decision_config.json；
      3. 已安装包的 share 目录（ament_index）：<share>/rmtc_bot/config/decision_config.json；
      4. 源码目录（开发态）：本文件所在包的 config/decision_config.json。
    返回首个存在的路径；均不存在返回 None（由调用方回退默认 decision_type）。
    """
    if decision_config_path:
        return decision_config_path if os.path.isfile(decision_config_path) else None

    candidates = [
        os.path.join(os.getcwd(), DECISION_CONFIG_FILENAME),
    ]
    # 已安装包的 share 目录（colcon 构建 / 正式运行场景）
    try:
        from ament_index_python.packages import get_package_share_directory
        share = get_package_share_directory("rmtc_bot")
        candidates.append(os.path.join(share, "config", DECISION_CONFIG_FILENAME))
    except Exception:
        pass
    # 源码目录（开发态：rmtc_bot/config.py -> ../config/decision_config.json）
    candidates.append(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", DECISION_CONFIG_FILENAME,
    ))

    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def load_config(decision_config_path: Optional[str] = None,
                params: Optional[Dict[str, object]] = None) -> Config:
    """
    组装 Config。

    取值顺序：ROS 2 启动参数（params）→ 环境变量 → 默认值。
    推荐用 launch 传参（见 launch/contestant.launch.py）；环境变量保留作兜底，
    便于 `ros2 run`、`python3 -m` 和自测脚本使用。

    主要启动参数 / 环境变量：
        seat / RMTC_SEAT                 本场席位（A/B/C/D）
        team_name / RMTC_TEAM_NAME       显示名称（1–96 UTF-8 字节）
        gateway / RMTC_GATEWAY           LLM 网关基址
        team_token / RMTC_TEAM_TOKEN     本场席位令牌
        model / RMTC_MODEL               LLM 模型名（默认 deepseek-flash）
        max_tokens / RMTC_MAX_TOKENS     输出上限（默认 128）
        llm_quota / RMTC_LLM_QUOTA       本地软配额（0 表示不限制）
        fort_* / RMTC_FORT_*             堡垒参数
        ROS_DOMAIN_ID                    ROS 域编号（只能由环境设置）
    """
    seat = (_pick(params, "seat", "RMTC_SEAT", C.SEAT_STAGE1) or "A").strip().upper()
    if seat not in C.SEATS:
        raise ConfigError(f"非法席位 {seat!r}（应为 A/B/C/D）")

    team_name = _pick(params, "team_name", "RMTC_TEAM_NAME", "RMTC 队伍") or "RMTC 队伍"
    _validate_team_name(team_name)

    # 若没有直接给 gateway，则由裁判机 IP + 端口拼装
    gateway = _pick(params, "gateway", "RMTC_GATEWAY")
    ai_port = _to_int(_pick(params, "ai_port", "RMTC_AI_PORT", str(C.LLM_DEFAULT_PORT)),
                      C.LLM_DEFAULT_PORT)
    if not gateway:
        ref_ip = _pick(params, "referee_ip", "RMTC_REFEREE_IP", "127.0.0.1") or "127.0.0.1"
        gateway = f"http://{ref_ip}:{ai_port}"

    max_tokens_raw = _pick(params, "max_tokens", "RMTC_MAX_TOKENS")

    cfg = Config(
        seat=seat,
        team_name=team_name,
        gateway=gateway,
        token=_pick(params, "team_token", "RMTC_TEAM_TOKEN", "") or "",
        ros_domain_id=_to_int(_env("ROS_DOMAIN_ID", default="27"), 27),
        ai_port=ai_port,
        decision_type=_read_decision_type(_resolve_config_path(decision_config_path) or DECISION_CONFIG_FILENAME),
        decision_types=list(DECISION_TYPE_REGISTRY.keys()),
        llm_model=_pick(params, "model", "RMTC_MODEL"),
        llm_max_tokens=(_to_int(max_tokens_raw, 128) if max_tokens_raw else None),
        llm_soft_quota=_to_int(_pick(params, "llm_quota", "RMTC_LLM_QUOTA",
                                     str(C.LLM_DEFAULT_SOFT_QUOTA)), C.LLM_DEFAULT_SOFT_QUOTA),
        # 堡垒：启动参数优先，环境变量兜底；默认即 [11,11]–[13,13]
        fort_x0=_to_int(_pick(params, "fort_x0", "RMTC_FORT_X0", str(C.FORT_X0_DEFAULT)), C.FORT_X0_DEFAULT),
        fort_x1=_to_int(_pick(params, "fort_x1", "RMTC_FORT_X1", str(C.FORT_X1_DEFAULT)), C.FORT_X1_DEFAULT),
        fort_y0=_to_int(_pick(params, "fort_y0", "RMTC_FORT_Y0", str(C.FORT_Y0_DEFAULT)), C.FORT_Y0_DEFAULT),
        fort_y1=_to_int(_pick(params, "fort_y1", "RMTC_FORT_Y1", str(C.FORT_Y1_DEFAULT)), C.FORT_Y1_DEFAULT),
        fort_auto_center=_to_bool(_pick(params, "fort_auto_center", "RMTC_FORT_AUTO_CENTER", "0"), False),
        fort_require_move=_to_bool(_pick(params, "fort_require_move", "RMTC_FORT_REQUIRE_MOVE", "1"), True),
        fort_retreat_hp=_to_int(_pick(params, "fort_retreat_hp", "RMTC_FORT_RETREAT_HP",
                                      str(C.FORT_RETREAT_HP_DEFAULT)), C.FORT_RETREAT_HP_DEFAULT),
        fort_resume_hp=_to_int(_pick(params, "fort_resume_hp", "RMTC_FORT_RESUME_HP",
                                     str(C.FORT_RESUME_HP_DEFAULT)), C.FORT_RESUME_HP_DEFAULT),
        fort_enemy_policy=(
            (_pick(params, "fort_enemy_policy", "RMTC_FORT_ENEMY_POLICY",
                   C.FORT_ENEMY_POLICY_DEFAULT) or C.FORT_ENEMY_POLICY_DEFAULT).strip().lower()
        ),
    )

    if cfg.fort_enemy_policy not in C.FORT_ENEMY_POLICIES:
        log.error("非法 fort_enemy_policy=%r，回退 %s", cfg.fort_enemy_policy, C.FORT_ENEMY_POLICY_DEFAULT)
        cfg.fort_enemy_policy = C.FORT_ENEMY_POLICY_DEFAULT

    if not cfg.token:
        # 不阻断启动：可能处于「纯感知/动作联调」阶段（不要求 AI）。
        # 但阶段二正式比赛必须有本场令牌，否则 AI 资格不成立。
        log.warning("未提供团队令牌（team_token / RMTC_TEAM_TOKEN）：LLM 调用将不可用（不影响感知/动作联调）")

    log.info(
        "配置就绪 seat=%s domain=%s decision_type=%s gateway=%s ai_port=%s",
        cfg.seat, cfg.ros_domain_id, cfg.decision_type, cfg.gateway, cfg.ai_port,
    )

    # LLM 网关参数字典（供 LLMGateway 读取；enabled 由令牌是否存在决定）
    cfg.llm = {
        "enabled": bool(cfg.token),
        "base_url": cfg.gateway,
        "api_key": cfg.token,
        "model": cfg.llm_model,
        "max_tokens": cfg.llm_max_tokens or 128,
        "timeout_sec": C.LLM_LOCAL_TIMEOUT_S,
        "soft_quota": cfg.llm_soft_quota,
    }
    return cfg


def _validate_team_name(name: str) -> None:
    """队名按 UTF-8 编码为 1–96 字节，不得包含换行或回车（协议第 2 节）。"""
    b = name.encode("utf-8")
    if not (C.TEAM_NAME_MIN_BYTES <= len(b) <= C.TEAM_NAME_MAX_BYTES):
        raise ConfigError(f"队名 UTF-8 长度需在 1–96 字节，当前 {len(b)} 字节")
    if "\n" in name or "\r" in name:
        raise ConfigError("队名不得包含换行或回车")
