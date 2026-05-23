#!/usr/bin/env python3
"""
示例钩子：审计 bash 工具调用，命中黑名单时阻断（PreToolUse exit 1）。

环境变量入参：HOOK_TOOL_NAME / HOOK_TOOL_INPUT（JSON 串）
退出码：0 放行；1 阻断（stderr 即阻断原因）。
"""
import json
import os
import sys

BLACKLIST = (
    "rm -rf /",
    "mkfs",
    ":(){ :|:& };:",
    "shutdown",
    "reboot",
)


def main() -> int:
    raw = os.environ.get("HOOK_TOOL_INPUT", "{}")
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {}
    command = str(payload.get("command", ""))
    for pattern in BLACKLIST:
        if pattern in command:
            sys.stderr.write(f"audit_bash: blocked dangerous command (matched '{pattern}')")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
