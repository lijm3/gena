"""
LLM 客户端 - 统一的 LLM API 调用
"""
import json
import time
from typing import Any, Dict, List, Optional

import requests

from config.settings import LLMConfig
from utils.llm_logger import llm_logger

# 多数 LLM 网关返回 UTF-8，但未带 charset；requests 默认按 ISO-8859-1 解码会导致中文乱码。
_DEFAULT_TEXT_ENCODING = "utf-8"


def _ensure_utf8_response(response: requests.Response) -> None:
    response.encoding = _DEFAULT_TEXT_ENCODING


def _merge_partial_json(buf: str) -> Dict[str, Any]:
    if not buf.strip():
        return {}
    try:
        return json.loads(buf)
    except json.JSONDecodeError:
        return {}


def _parse_sse_event(data: Dict[str, Any], state: Dict[str, Any], emit_stream: bool) -> None:
    """累积 Anthropic Messages SSE 事件，更新 state。"""
    et = data.get("type")
    if et == "content_block_start":
        idx = int(data["index"])
        cb = data["content_block"]
        cbt = cb["type"]
        if cbt == "text":
            state["blocks"][idx] = {"type": "text", "text": cb.get("text") or ""}
        elif cbt == "tool_use":
            state["blocks"][idx] = {
                "type": "tool_use",
                "id": cb["id"],
                "name": cb["name"],
                "input": cb.get("input") if isinstance(cb.get("input"), dict) else {},
                "_json_buf": "",
            }
    elif et == "content_block_delta":
        idx = int(data["index"])
        delta = data.get("delta") or {}
        dt = delta.get("type")
        block = state["blocks"].get(idx)
        if not block:
            return
        if dt == "text_delta" and block.get("type") == "text":
            piece = delta.get("text") or ""
            block["text"] = block.get("text", "") + piece
            if emit_stream and piece:
                print(piece, end="", flush=True)
        elif dt == "input_json_delta" and block.get("type") == "tool_use":
            block["_json_buf"] = block.get("_json_buf", "") + (delta.get("partial_json") or "")
    elif et == "content_block_stop":
        idx = int(data["index"])
        block = state["blocks"].get(idx)
        if block and block.get("type") == "tool_use":
            buf = block.pop("_json_buf", "")
            block["input"] = _merge_partial_json(buf) if buf else block.get("input", {})
    elif et == "message_delta":
        md = data.get("delta") or {}
        if "stop_reason" in md:
            state["stop_reason"] = md["stop_reason"]
        usage = md.get("usage")
        if usage:
            state["usage"] = usage
    elif et == "message_start":
        msg = data.get("message") or {}
        if msg.get("id"):
            state["id"] = msg["id"]
        if msg.get("model"):
            state["model"] = msg["model"]


def _consume_stream(response: requests.Response, emit_stream: bool) -> Dict[str, Any]:
    _ensure_utf8_response(response)
    state: Dict[str, Any] = {
        "blocks": {},
        "stop_reason": "end_turn",
        "id": None,
        "model": None,
        "usage": None,
    }
    # 走字节流再手动 decode：requests 的 decode_unicode=True 会按 chunk decode，
    # 多字节字符跨 TCP 包边界时尾部字节会被 replace 成 � 导致丢字。
    # SSE 行尾是 \n（ASCII），按字节切行不会切坏 UTF-8 多字节序列。
    for raw_line in response.iter_lines(decode_unicode=False):
        if not raw_line:
            continue
        line = raw_line.decode("utf-8", errors="replace")
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw == "[DONE]":
            break
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        _parse_sse_event(data, state, emit_stream)

    if emit_stream:
        print()

    blocks_by_idx = sorted(state["blocks"].items(), key=lambda x: x[0])
    content: List[Dict[str, Any]] = [b for _, b in blocks_by_idx]

    out: Dict[str, Any] = {
        "type": "message",
        "role": "assistant",
        "content": content,
        "stop_reason": state.get("stop_reason") or "end_turn",
    }
    if state.get("id"):
        out["id"] = state["id"]
    if state.get("model"):
        out["model"] = state["model"]
    if state.get("usage"):
        out["usage"] = state["usage"]
    return out


class LLMClient:
    """LLM 客户端类"""

    def __init__(self) -> None:
        self.base_url = LLMConfig.get_base_url().rstrip("/")
        self.auth_token = LLMConfig.get_auth_token()
        self.model = LLMConfig.get_model()
        self.max_tokens = LLMConfig.get_max_tokens()
        self.timeout = LLMConfig.get_timeout()
        self.use_stream = LLMConfig.get_stream()
        self.stream_print = LLMConfig.get_stream_print()

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.auth_token}",
            "x-api-key": self.auth_token,
            "anthropic-version": "2023-06-01",
        }

    def call_api(
        self,
        messages: List[Dict[str, Any]],
        system: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: Optional[int] = None,
        *,
        stream: Optional[bool] = None,
        emit_stream_print: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        调用 LLM API（Anthropic Messages 兼容）。

        Returns:
            非流式或与流式等价的 dict，至少包含 content、stop_reason。
        """
        use_stream = self.use_stream if stream is None else stream
        emit = self.stream_print if emit_stream_print is None else emit_stream_print

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "stream": use_stream,
        }

        if system:
            payload["system"] = system

        if tools:
            payload["tools"] = tools

        url = f"{self.base_url}/v1/messages"

        # 日志只记 payload(已剥离 auth header),便于事后复现这一轮 LLM 看到了什么
        started = time.monotonic()

        def _emit(resp: Optional[Dict[str, Any]] = None, err: Optional[str] = None) -> None:
            llm_logger.log_call(
                request=payload,
                response=resp,
                error=err,
                duration_ms=(time.monotonic() - started) * 1000,
            )

        # LLMRequest 钩子：每次 LLM 调用之前触发（observer-only；高频，性能敏感）
        try:
            from managers.hook_manager import fire_hook, HookContext
            fire_hook(
                "LLMRequest",
                HookContext(
                    agent_role="lead",
                    llm_model=self.model,
                    message_count=len(messages),
                ),
            )
        except Exception:
            pass

        try:
            if not use_stream:
                response = requests.post(
                    url,
                    headers=self._headers(),
                    json=payload,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                _ensure_utf8_response(response)
                data = response.json()
                _emit(resp=data)
                self._fire_llm_response(data, started)
                return data

            response = requests.post(
                url,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
                stream=True,
            )
            response.raise_for_status()
            try:
                data = _consume_stream(response, emit_stream=emit)
            finally:
                response.close()
            _emit(resp=data)
            self._fire_llm_response(data, started)
            return data
        except Exception as e:
            _emit(err=f"{type(e).__name__}: {e}")
            raise

    def _fire_llm_response(self, data: Dict[str, Any], started: float) -> None:
        """LLM 调用成功后触发 LLMResponse 钩子。失败静默吞掉。"""
        try:
            from managers.hook_manager import fire_hook, HookContext
            usage = data.get("usage") or {}
            fire_hook(
                "LLMResponse",
                HookContext(
                    agent_role="lead",
                    llm_model=data.get("model") or self.model,
                    llm_stop_reason=str(data.get("stop_reason") or ""),
                    llm_input_tokens=int(usage.get("input_tokens", 0) or 0),
                    llm_output_tokens=int(usage.get("output_tokens", 0) or 0),
                    llm_duration_ms=int((time.monotonic() - started) * 1000),
                ),
            )
        except Exception:
            pass

    def create_message(
        self,
        messages: List[Dict[str, Any]],
        system: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """创建消息（别名）"""
        return self.call_api(messages, system, tools, max_tokens)
