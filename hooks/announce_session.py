#!/usr/bin/env python3
"""
示例钩子：会话开始时打印一行招呼到 stdout（钩子日志里能看到）。

退出码 0；stdout 会被 HookManager 截取一行打到 [hook:SessionStart] 日志。
"""
import os
import sys


def main() -> int:
    role = os.environ.get("HOOK_AGENT_ROLE", "?")
    cwd = os.environ.get("HOOK_CWD", "?")
    print(f"session-start role={role} cwd={cwd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
