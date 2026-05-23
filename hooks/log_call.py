#!/usr/bin/env python3
"""
示例钩子：把每次事件追加到 logs/tool_calls.jsonl。

由 HookManager 启动；环境变量入参见 docs/钩子机制方案.md §5。
所有事件共用一份记录器；按 HOOK_EVENT 字段分流。
"""
import datetime
import json
import os
import sys
from pathlib import Path

LOG_PATH = Path("logs") / "tool_calls.jsonl"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def main() -> int:
    event = os.environ.get("HOOK_EVENT", "")
    record = {
        "ts": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "event": event,
        "agent_role": os.environ.get("HOOK_AGENT_ROLE", ""),
    }
    # 按事件类型挑相关字段，避免每条记录都塞一堆空字符串
    if event in ("PreToolUse", "PostToolUse"):
        record["tool"] = os.environ.get("HOOK_TOOL_NAME", "")
        record["round"] = os.environ.get("HOOK_ROUND", "")
        record["call_index"] = os.environ.get("HOOK_CALL_INDEX", "")
        if event == "PostToolUse":
            record["status"] = os.environ.get("HOOK_OUTPUT_STATUS", "")
            record["duration_ms"] = os.environ.get("HOOK_DURATION_MS", "")
            err = os.environ.get("HOOK_ERROR")
            if err:
                record["error"] = err
    elif event == "UserPromptSubmit":
        record["prompt"] = (os.environ.get("HOOK_USER_PROMPT") or "")[:200]
    elif event in ("Stop", "SubagentStop"):
        record["outcome_preview"] = (os.environ.get("HOOK_OUTCOME") or "")[:200]
        if event == "SubagentStop":
            record["agent_type"] = os.environ.get("HOOK_AGENT_TYPE", "")
    elif event == "PreCompact":
        record["message_count"] = os.environ.get("HOOK_MESSAGE_COUNT", "")
        record["token_estimate"] = os.environ.get("HOOK_TOKEN_ESTIMATE", "")
    elif event in ("LLMRequest", "LLMResponse"):
        record["model"] = os.environ.get("HOOK_LLM_MODEL", "")
        record["message_count"] = os.environ.get("HOOK_MESSAGE_COUNT", "")
        if event == "LLMResponse":
            record["stop_reason"] = os.environ.get("HOOK_LLM_STOP_REASON", "")
            record["in_tokens"] = os.environ.get("HOOK_LLM_INPUT_TOKENS", "")
            record["out_tokens"] = os.environ.get("HOOK_LLM_OUTPUT_TOKENS", "")
            record["duration_ms"] = os.environ.get("HOOK_LLM_DURATION_MS", "")
    elif event == "GuardTriggered":
        record["reason"] = os.environ.get("HOOK_GUARD_REASON", "")
        record["metric"] = os.environ.get("HOOK_GUARD_METRIC", "")

    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
