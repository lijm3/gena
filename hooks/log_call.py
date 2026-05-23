#!/usr/bin/env python3
"""
示例钩子：把每次工具调用追加到 logs/tool_calls.jsonl。

由 HookManager 启动；环境变量入参见 docs/钩子机制方案.md §5。
"""
import datetime
import json
import os
import sys
from pathlib import Path

LOG_PATH = Path("logs") / "tool_calls.jsonl"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def main() -> int:
    record = {
        "ts": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "event": os.environ.get("HOOK_EVENT", ""),
        "agent_role": os.environ.get("HOOK_AGENT_ROLE", ""),
        "tool_name": os.environ.get("HOOK_TOOL_NAME", ""),
        "round": os.environ.get("HOOK_ROUND", ""),
        "call_index": os.environ.get("HOOK_CALL_INDEX", ""),
    }
    if record["event"] == "PostToolUse":
        record["status"] = os.environ.get("HOOK_OUTPUT_STATUS", "")
        record["duration_ms"] = os.environ.get("HOOK_DURATION_MS", "")
        err = os.environ.get("HOOK_ERROR")
        if err:
            record["error"] = err
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
