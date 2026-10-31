# -*- coding: utf-8 -*-
"""感知解析子包。"""

from .parser import (  # noqa: F401
    PerceptionParseError,
    parse_perception,
)

__all__ = [
    "PerceptionParseError",
    "parse_perception",
]
