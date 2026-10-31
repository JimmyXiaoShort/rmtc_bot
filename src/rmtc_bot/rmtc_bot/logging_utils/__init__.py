# -*- coding: utf-8 -*-
"""
结构化日志、指标与脱敏（协议第 5 节、规则 §11）。

职责：
- 统一日志配置（rmtc.* → stderr）；
- 令牌脱敏：日志中不得出现本场席位令牌明文；
- 关键指标留痕：无效动作计数上升、LLM 降级、场次切换等。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

log = logging.getLogger("rmtc.monitor")

# 令牌脱敏正则：匹配 Bearer 后的长字符串
_TOKEN_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]+", re.I)


def mask_token(text: str) -> str:
    """把日志中的 Bearer 令牌替换为 ***，避免泄露本场席位令牌。"""
    if not text:
        return text
    return _TOKEN_RE.sub(r"\1***", text)


def configure_logging(level: int = logging.INFO) -> None:
    """配置 rmtc.* 日志输出到 stderr。"""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def warn_invalid_actions(prev: int, curr: int, tick: int) -> None:
    """无效动作计数上升时告警（裁判结算记录，不按收到坏消息累加）。"""
    log.warning("无效动作计数上升：%d -> %d（tick=%s），请检查动作合法性", prev, curr, tick)


def info_session_switch(old: Optional[str], new: str) -> None:
    log.info("场次切换 %s -> %s", old, new)
