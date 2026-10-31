# -*- coding: utf-8 -*-
"""
OpenAI 兼容 LLM 网关（协议第 5 节「LLM 网关」）。

要点：
- 公开端点仅 <GATEWAY>/healthz 与 <GATEWAY>/v1/chat/completions；
- 请求头：Content-Type: application/json、Authorization: Bearer <本队令牌>（必填）；
- 请求体与 OpenAI 兼容：messages 非空数组；model 统一 deepseek-flash；
- 【关键】正式网关不主动关闭思考且除 model 外原样转发：DeepSeek 默认开启思考，
  必须由选手在顶层显式传 "thinking": {"type": "disabled"}，否则响应耗时远超动作
  窗口（并浪费配额）；thinking/temperature/max_tokens/response_format 由选手填写；
- response_format: {"type": "json_object"} 强制结构化输出；若上游 400 不支持，
  自动去掉该字段重试一次并记住（后续不再发送）；
- 响应以完整内容返回（不流式）；须同时处理 HTTP 状态码、错误码、正文解析失败、连接异常；
- 频率/并发：每来源 IP 10 秒最多 100 次 POST；每席位令牌最多 2 个在途调用；
- 429 + quota_exceeded → 降级；rate_limit/concurrency_limit → 单次短退避重试；
- 本地硬超时 1.9s（参考官方 Python 示例），显著小于动作窗口（阶段二 500 ms），
  不能让模型等待阻塞动作窗口（协议明确要求异步/降级）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Dict, Optional

import requests

from ..models import Intent

log = logging.getLogger("rmtc.llm")

_DEFAULT_MODEL = "deepseek-flash"
_RETRY_BACKOFF_S = 0.15   # 429(rate_limit/concurrency_limit) 单次退避重试间隔

_FENCE_RE = re.compile(r"```[ \t]*(?:json)?[ \t]*\r?\n?(.*?)```", re.S | re.I)
_INVISIBLE = dict.fromkeys(map(ord, "\ufeff\u200b\u200c\u200d"), None)
_ZH_DIRECTION = {
    "上": "up", "向上": "up", "下": "down", "向下": "down",
    "左": "left", "向左": "left", "右": "right", "向右": "right",
}


def _extract_json_text(content: str) -> str:
    """从模型回复中抽取 JSON 候选文本。

    兼容三类常见输出：
      1) 纯 JSON；
      2) ```json ... ``` / ```JSON ... ``` 围栏（围栏前后可带解释文字）；
      3) 解释文字包裹 JSON（截取首个 '{' 到最后一个 '}'）。
    """
    text = content.translate(_INVISIBLE).strip()
    m = _FENCE_RE.search(text)
    if m is not None:
        text = m.group(1).strip()
    else:
        i, j = text.find("{"), text.rfind("}")
        if i != -1 and j > i:
            text = text[i:j + 1]
        elif text.startswith("`"):
            text = text.strip("`").strip()
    return text.strip()


def _repair_json(text: str) -> str:
    """对模型 JSON 的常见瑕疵做轻量修复（尽力而为，失败仍走降级）。

    - 全角/中文引号 → 半角；
    - 尾随逗号（`,` 紧跟 `}` / `]`）；
    - 单引号包裹的键与值；
    - 未加引号且非数字的裸词值（如 "kind": move）。
    """
    t = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    t = re.sub(r",\s*([}\]])", r"\1", t)
    t = re.sub(r"'([^'\"{}:,]*?)'\s*:", r'"\1":', t)
    t = re.sub(r":\s*'([^']*)'", r': "\1"', t)
    t = re.sub(r":\s*([A-Za-z_][A-Za-z0-9_]*)\s*([,}])", r': "\1"\2', t)
    return t


_SYSTEM_PROMPT = (
    "你是 RMTC 机甲对战机器人的事件解析器。裁判会给你一段中文事件公告，"
    "你要判断它此刻最合适的立即动作。\n"
    "输出要求：只输出一个 json 对象，不要 Markdown 代码块，不要任何解释文字。\n"
    "字段（仅这三个）：\n"
    '- kind: "move" | "attack" | "defend" | "wait"\n'
    '- direction: "up" | "down" | "left" | "right"（move/attack 必填；'
    "move 表示下一步移动方向，attack 表示目标所在方向；up 为 y 减小）\n"
    "- reason: 8 字以内中文短语\n"
    "判定规则：\n"
    "1) 要求撤离/躲避/远离某处 → move，方向为远离该威胁的方向；\n"
    "2) 给出敌人方位或要求集火 → attack，方向为敌人所在方向；\n"
    "3) 要求防守/固守/回防 → defend；\n"
    "4) 只是通知、无明确战术指令 → wait。\n"
    "示例：\n"
    '输入「陨石将落在你的左侧，请向右躲避」→ {"kind":"move","direction":"right","reason":"躲避陨石"}\n'
    '输入「右侧出现敌方机甲，集中火力」→ {"kind":"attack","direction":"right","reason":"集火右侧"}\n'
    '输入「堡垒已被占领，回防」→ {"kind":"defend","reason":"回防"}\n'
)


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


class LLMGateway:
    """OpenAI 兼容网关：事件解析 + 鉴权 + 配额/频控/降级。"""

    def __init__(self, cfg) -> None:
        c = cfg.llm
        self.enabled = bool(c.get("enabled", False))
        self.base_url = c.get("base_url", "http://127.0.0.1:8000")
        self.api_key = c.get("api_key", cfg.token)   # 本场席位令牌（Bearer）
        # 规则更新：模型统一 deepseek-flash（未显式配置时也用它，避免落到思考模型默认值）
        self.model = c.get("model") or _DEFAULT_MODEL
        self.max_tokens = c.get("max_tokens", 128)
        self.timeout = float(c.get("timeout_sec", 1.9))
        # 结构化输出能力：首次 400 后自动降级并记住（避免每 Tick 重复撞 400）
        self._json_mode = True
        # 频控/并发（协议 §5）
        self._lock = threading.Lock()
        self._inflight = 0
        self._bucket = TokenBucket(
            rate=float(c.get("qps", 8.0)),          # 本地远低于每 IP 100/10s
            capacity=float(c.get("burst", 16)),
        )
        # 本地软配额（赛前公告为准；0 不限制）。按 HTTP POST 次数计数，
        # 重试与失败请求同样占用额度，保证总调用不超过公告上限。
        self.soft_quota = int(c.get("soft_quota", 0))
        self._used = 0
        if self.enabled:
            log.info("LLM 网关启用: %s model=%s max_tokens=%s",
                     self.base_url, self.model or "<default>", self.max_tokens)

    # ------------------------------------------------------------------
    def parse_event(self, text: str) -> Optional[Intent]:
        """解析事件文本 → Intent；不可用/降级返回 None（调用方忽略）。"""
        if not self.enabled:
            return None
        if not self._bucket.acquire():
            log.warning("LLM 本地限频，跳过本次解析")
            return None
        with self._lock:
            if self._inflight >= 2:   # 每令牌最多 2 个在途
                log.warning("LLM 在途调用已达上限(2)，跳过")
                return None
            self._inflight += 1
        try:
            content = self._chat(text)
        finally:
            with self._lock:
                self._inflight -= 1
        return self._parse_response(content)

    # ------------------------------------------------------------------
    def _build_payload(self, user_text: str, json_mode: bool) -> Dict[str, Any]:
        """构造请求体。

        - model 固定为 deepseek-flash（规则更新：模型统一）；
        - thinking 顶层显式关闭：正式网关不主动关闭思考、其余字段原样转发，
          不传则上游默认开启思考 → 响应超出动作窗口并浪费配额；
        - response_format=json_object 强制结构化输出（上游不支持时自动降级）。
        """
        payload: Dict[str, Any] = {
            "model": self.model,
            "thinking": {"type": "disabled"},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_text[:500]},
            ],
            "max_tokens": self.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _reserve_call(self) -> bool:
        """预占一次调用额度；达到上限返回 False（含重试在内统一计数）。"""
        with self._lock:
            if self.soft_quota > 0 and self._used >= self.soft_quota:
                log.warning("LLM 调用已达上限(%s)，降级", self.soft_quota)
                return False
            self._used += 1
            return True

    def _post(self, url: str, headers: Dict[str, str], payload: Dict[str, Any],
              timeout: float) -> Any:
        """发送一次请求：返回 requests.Response；连接/读取异常返回 None。"""
        if not self._reserve_call():
            return None
        try:
            return requests.post(url, json=payload, headers=headers, timeout=timeout)
        except Exception as exc:  # 连接/读取异常：降级，不重试
            log.warning("LLM 连接异常: %r", exc)
            return None

    @staticmethod
    def _error_code(body: str) -> str:
        try:
            obj = json.loads(body)
            return (obj.get("error") or {}).get("code", "") or ""
        except Exception:  # noqa: BLE001
            return ""

    def _chat(self, user_text: str) -> Optional[str]:
        """调用 <GATEWAY>/v1/chat/completions（OpenAI 兼容，非流式）。"""
        url = self.base_url.rstrip("/") + "/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        r = self._post(url, headers, self._build_payload(user_text, self._json_mode), self.timeout)
        if r is None:
            return None

        # 上游不支持 response_format（400）→ 去掉该字段重试一次，并记住后续不再发送
        if r.status_code == 400 and self._json_mode:
            log.info("上游不支持 response_format，改为提示词约束 JSON（后续不再发送）")
            self._json_mode = False
            r = self._post(url, headers, self._build_payload(user_text, False), self.timeout)
            if r is None:
                return None

        # 429 且非额度耗尽（限频/并发）→ 单次短退避重试；quota_exceeded 直接降级
        if r.status_code == 429:
            code = self._error_code(r.text)
            if code != "quota_exceeded":
                time.sleep(_RETRY_BACKOFF_S)
                r2 = self._post(url, headers, self._build_payload(user_text, self._json_mode),
                                self.timeout)
                if r2 is not None and r2.status_code == 200:
                    r = r2
                else:
                    self._handle_http_error(r.status_code, r.text)
                    return None
            else:
                self._handle_http_error(r.status_code, r.text)
                return None

        if r.status_code != 200:
            self._handle_http_error(r.status_code, r.text)
            return None
        try:
            data = r.json()
            return data["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM 响应解析异常: %r", exc)
            return None

    def _handle_http_error(self, status: int, body: str) -> None:
        """按协议 §5 错误码表处理；错误提示文字不作为程序判断依据。"""
        code = self._error_code(body)
        if status == 429:
            if code == "quota_exceeded":
                log.warning("LLM 额度耗尽(quota_exceeded)，降级（不重试）")
            else:
                log.warning("LLM 限频/并发(%s)，可退避后重试", code or "rate_limit")
        elif status in (401, 403):
            log.error("LLM 鉴权失败 status=%s code=%s：请检查本场席位令牌", status, code)
        elif status == 503:
            log.warning("LLM audit_unavailable(503)：保持本地决策并联系裁判")
        else:
            log.warning("LLM HTTP %s code=%s body=%s", status, code, body[:120])

    @staticmethod
    def _parse_response(content: Optional[str]) -> Optional[Intent]:
        """从回复提取 JSON 并构造 Intent；解析失败返回 None。

        流程：先抽取（兼容 Markdown 围栏/前后解释文字）→ 原样解析 →
        失败则轻量修复后再解析（全角引号、尾随逗号、单引号、裸词值）。
        """
        if not content:
            return None
        text = _extract_json_text(content)
        if not text:
            log.warning("LLM 回复不含 JSON: %r", content[:80])
            return None

        obj: Any = None
        for candidate in (text, _repair_json(text)):
            try:
                obj = json.loads(candidate)
                break
            except (json.JSONDecodeError, ValueError):
                continue
        if not isinstance(obj, dict):
            log.warning("LLM 回复无法解析为 JSON 对象: %r", content[:80])
            return None

        kind = str(obj.get("kind") or obj.get("action") or "").strip().lower()
        if kind not in ("move", "attack", "defend", "wait"):
            return None
        raw_dir = obj.get("direction")
        if raw_dir is None:
            direction = None
        else:
            direction = str(raw_dir).strip().lower()
            direction = _ZH_DIRECTION.get(direction, direction)
        if kind in ("move", "attack") and direction not in ("up", "down", "left", "right"):
            return None
        if kind in ("defend", "wait"):
            direction = None
        return Intent(
            kind=kind,
            direction=direction,
            reason=str(obj.get("reason", "llm-event"))[:32],
        )

