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
import threading
import time
from typing import Any, Dict, Optional

import requests

from ..models import Intent
from .prompts import SYSTEM_PROMPT, parse_response
from .quota import TokenBucket

log = logging.getLogger("rmtc.llm")

_DEFAULT_MODEL = "deepseek-flash"
_RETRY_BACKOFF_S = 0.15   # 429(rate_limit/concurrency_limit) 单次退避重试间隔


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
            log.info("LLM 网关启用: %s → %s model=%s max_tokens=%s",
                     self.base_url, self._endpoint(), self.model or "<default>", self.max_tokens)
        else:
            log.warning("LLM 网关未启用（未提供 team_token / RMTC_TEAM_TOKEN），"
                        "事件将只做本地保守处理")

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
        if content:
            intent = parse_response(content)
            if intent is None:
                # 调用成功但回复不可用（如练习环境的固定模拟响应）→ 保持本地决策
                log.info("LLM 已返回但内容不可用，忽略: %s", content[:80].replace("\n", " "))
            return intent
        return None

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
                {"role": "system", "content": SYSTEM_PROMPT},
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

    def _endpoint(self) -> str:
        """拼接请求地址：gateway 可写基址，也可直接写完整端点。

        协议第 5 节的写法是 `<GATEWAY>/v1/chat/completions`，而本地练习环境
        （rmtc_practice/测试使用教程）给出的就是完整地址
        `http://127.0.0.1:8080/v1/chat/completions`。两种写法都要能用，
        否则完整地址会被再拼一次路径 → 404，LLM 永远调不通。

        另外允许省略协议头（现场手敲参数常写成 `127.0.0.1:8080`）：
        requests 对无协议的地址会抛 MissingSchema，等于一次调用都没发出去。
        """
        base = self.base_url.strip().rstrip("/")
        if "://" not in base:
            base = "http://" + base.lstrip("/")
        if base.endswith("/v1/chat/completions"):
            return base
        return base + "/v1/chat/completions"

    @staticmethod
    def _error_code(body: str) -> str:
        try:
            obj = json.loads(body)
            return (obj.get("error") or {}).get("code", "") or ""
        except Exception:  # noqa: BLE001
            return ""

    def _chat(self, user_text: str) -> Optional[str]:
        """调用 <GATEWAY>/v1/chat/completions（OpenAI 兼容，非流式）。"""
        url = self._endpoint()
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
        elif status == 404:
            log.error("LLM 请求地址 404：%s —— 请确认 gateway 是基址（如 http://<IP>:<端口>）"
                      "或完整端点（.../v1/chat/completions），不要带多余路径", self._endpoint())
        else:
            log.warning("LLM HTTP %s code=%s body=%s", status, code, body[:120])
