# -*- coding: utf-8 -*-
"""
LLM 配额与在途限制（协议第 5 节）。

- 每来源 IP 10 秒最多 100 次 POST；
- 每席位令牌最多 2 个在途调用；
- 本地软配额（赛前公告为准；0 表示不限制）。
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """令牌桶限频（本地软限，配合协议每 IP 10 秒 100 次 POST）。"""

    def __init__(self, rate: float, capacity: float) -> None:
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self._lock = threading.Lock()
        self._last = time.monotonic()

    def acquire(self) -> bool:
        with self._lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
            self._last = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False
