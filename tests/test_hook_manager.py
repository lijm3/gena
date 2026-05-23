"""
HookManager 单元测试 - 覆盖 docs/钩子机制方案.md §10 所列场景。

设计：每个测试用 tmp_path 构造独立的工作目录，通过写入 .claude/hooks.json
与 .claude/.claude_trusted 控制钩子的开启状态；钩子脚本用 shebang-less Python
通过 sys.executable 调用，避免依赖系统 PATH 上的 python。
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

# 让测试能 import 仓库根的模块
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from managers.hook_manager import (
    HookManager, HookContext, HookResult, HOOK_EVENTS,
)


@pytest.fixture
def trusted_workdir(tmp_path: Path) -> Path:
    """构造一个工作区：.claude/ + 信任标记。"""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".claude_trusted").write_text("")
    return tmp_path


def _write_hook_script(workdir: Path, name: str, source: str) -> str:
    """把一段 Python 脚本写入 hooks/<name>.py 并返回执行命令。"""
    hooks_dir = workdir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    script = hooks_dir / f"{name}.py"
    script.write_text(textwrap.dedent(source), encoding="utf-8")
    # 用 sys.executable 跑，避免 PATH 上没有 python。
    return f'"{sys.executable}" "{script}"'


def _write_config(workdir: Path, hooks_cfg: dict) -> None:
    (workdir / ".claude" / "hooks.json").write_text(
        json.dumps({"hooks": hooks_cfg}), encoding="utf-8"
    )


# ---------------- 信任门 ----------------

def test_no_config_inactive(tmp_path):
    mgr = HookManager(workdir=tmp_path)
    assert mgr.is_active() is False
    # 不抛错，返回空 result
    result = mgr.run_hooks("PreToolUse", HookContext(tool_name="bash"))
    assert isinstance(result, HookResult)
    assert result.blocked is False and result.messages == []


def test_config_but_no_trust_marker(tmp_path):
    """有配置但没信任标记 -> 不执行任何钩子。"""
    (tmp_path / ".claude").mkdir()
    cmd = _write_hook_script(tmp_path, "boom", "import sys; sys.exit(1)")
    (tmp_path / ".claude" / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"command": cmd}]}}), encoding="utf-8"
    )
    mgr = HookManager(workdir=tmp_path)
    assert mgr.is_active() is False
    result = mgr.run_hooks("PreToolUse", HookContext(tool_name="bash"))
    assert result.blocked is False


def test_sdk_mode_bypasses_trust(tmp_path):
    cmd = _write_hook_script(tmp_path, "ok", "print('ok')")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"command": cmd}]}}), encoding="utf-8"
    )
    mgr = HookManager(workdir=tmp_path, sdk_mode=True)
    assert mgr.is_active() is True


# ---------------- matcher ----------------

def test_matcher_exact_match(trusted_workdir):
    cmd = _write_hook_script(trusted_workdir, "mark", "import sys; sys.stderr.write('hit'); sys.exit(2)")
    _write_config(trusted_workdir, {"PreToolUse": [{"matcher": "bash", "command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    # 匹配
    r = mgr.run_hooks("PreToolUse", HookContext(tool_name="bash"))
    assert r.messages == ["hit"]
    # 不匹配
    r = mgr.run_hooks("PreToolUse", HookContext(tool_name="read_file"))
    assert r.messages == []


def test_matcher_wildcard(trusted_workdir):
    cmd = _write_hook_script(trusted_workdir, "mark", "import sys; sys.stderr.write('hit'); sys.exit(2)")
    _write_config(trusted_workdir, {"PreToolUse": [{"matcher": "*", "command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    r = mgr.run_hooks("PreToolUse", HookContext(tool_name="anything"))
    assert r.messages == ["hit"]


# ---------------- exit code 0 ----------------

def test_exit0_silent(trusted_workdir):
    cmd = _write_hook_script(trusted_workdir, "ok", "")
    _write_config(trusted_workdir, {"PreToolUse": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    r = mgr.run_hooks("PreToolUse", HookContext(tool_name="bash"))
    assert r.blocked is False and r.messages == [] and r.updated_input is None


def test_exit0_structured_updated_input(trusted_workdir):
    src = """
        import json, sys
        sys.stdout.write(json.dumps({"updatedInput": {"command": "ls"}}))
    """
    cmd = _write_hook_script(trusted_workdir, "rewrite", src)
    _write_config(trusted_workdir, {"PreToolUse": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    r = mgr.run_hooks("PreToolUse", HookContext(tool_name="bash", tool_input={"command": "rm -rf /"}))
    assert r.updated_input == {"command": "ls"}


def test_exit0_additional_context(trusted_workdir):
    src = """
        import json, sys
        sys.stdout.write(json.dumps({"additionalContext": "ctx"}))
    """
    cmd = _write_hook_script(trusted_workdir, "ctx", src)
    _write_config(trusted_workdir, {"PreToolUse": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    r = mgr.run_hooks("PreToolUse", HookContext(tool_name="bash"))
    assert r.messages == ["ctx"]


def test_exit0_permission_deny(trusted_workdir):
    src = """
        import json, sys
        sys.stdout.write(json.dumps({"permissionDecision": "deny"}))
    """
    cmd = _write_hook_script(trusted_workdir, "perm", src)
    _write_config(trusted_workdir, {"PreToolUse": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    r = mgr.run_hooks("PreToolUse", HookContext(tool_name="bash"))
    assert r.blocked is True
    assert r.permission_override == "deny"


# ---------------- exit 1 / 2 ----------------

def test_exit1_blocks_pretooluse(trusted_workdir):
    src = """
        import sys
        sys.stderr.write("blocked-reason")
        sys.exit(1)
    """
    cmd = _write_hook_script(trusted_workdir, "block", src)
    _write_config(trusted_workdir, {"PreToolUse": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    r = mgr.run_hooks("PreToolUse", HookContext(tool_name="bash"))
    assert r.blocked is True
    assert "blocked-reason" in r.block_reason


def test_exit1_on_posttooluse_degrades_to_inject(trusted_workdir):
    src = """
        import sys
        sys.stderr.write("post-blocked")
        sys.exit(1)
    """
    cmd = _write_hook_script(trusted_workdir, "post_block", src)
    _write_config(trusted_workdir, {"PostToolUse": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    r = mgr.run_hooks("PostToolUse", HookContext(tool_name="bash"))
    # PostToolUse 的 exit 1 退化为 inject（消息进 messages）
    assert r.blocked is False
    assert "post-blocked" in (r.messages[0] if r.messages else "")


def test_exit2_injects(trusted_workdir):
    src = """
        import sys
        sys.stderr.write("inject-msg")
        sys.exit(2)
    """
    cmd = _write_hook_script(trusted_workdir, "inject", src)
    _write_config(trusted_workdir, {"PreToolUse": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    r = mgr.run_hooks("PreToolUse", HookContext(tool_name="bash"))
    assert r.blocked is False
    assert r.messages == ["inject-msg"]


# ---------------- 串行 + 阻断早退 ----------------

def test_multiple_hooks_block_stops_chain(trusted_workdir):
    """阻断的钩子之后，后续钩子不再执行。"""
    block_cmd = _write_hook_script(
        trusted_workdir, "block_first",
        "import sys; sys.stderr.write('first-block'); sys.exit(1)",
    )
    # 第二个钩子写一个标记文件，用于检测是否被跑到
    marker = trusted_workdir / "second_ran.txt"
    second_cmd = _write_hook_script(
        trusted_workdir, "second",
        f"open(r'{marker}', 'w').write('x')",
    )
    _write_config(
        trusted_workdir,
        {"PreToolUse": [{"command": block_cmd}, {"command": second_cmd}]},
    )
    mgr = HookManager(workdir=trusted_workdir)
    r = mgr.run_hooks("PreToolUse", HookContext(tool_name="bash"))
    assert r.blocked is True
    assert not marker.exists(), "第二个钩子不应被执行"


# ---------------- 异常隔离 ----------------

def test_unexpected_exit_code_treated_as_zero(trusted_workdir):
    cmd = _write_hook_script(trusted_workdir, "weird", "import sys; sys.exit(99)")
    _write_config(trusted_workdir, {"PreToolUse": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    r = mgr.run_hooks("PreToolUse", HookContext(tool_name="bash"))
    assert r.blocked is False and r.messages == []


def test_python_exception_in_hook_does_not_propagate(trusted_workdir):
    """钩子脚本抛 Python 异常 → 进程退出码 1 → 按协议视为 block，但不向上抛错。"""
    cmd = _write_hook_script(trusted_workdir, "boom", "raise RuntimeError('boom')")
    _write_config(trusted_workdir, {"PreToolUse": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    # 不应抛错；返回正常的 HookResult（exit 1 在 PreToolUse 下意味着 blocked=True）
    r = mgr.run_hooks("PreToolUse", HookContext(tool_name="bash"))
    assert isinstance(r, HookResult)
    assert r.blocked is True  # exit 1 → block 是协议规定行为


# ---------------- 环境变量 ----------------

def test_env_vars_passed_to_hook(trusted_workdir):
    """HOOK_TOOL_NAME / HOOK_AGENT_ROLE / HOOK_ROUND / HOOK_TOOL_INPUT 等都应该传入。"""
    out_file = trusted_workdir / "env.json"
    src = f"""
        import json, os
        record = {{
            "tool": os.environ.get("HOOK_TOOL_NAME"),
            "role": os.environ.get("HOOK_AGENT_ROLE"),
            "round": os.environ.get("HOOK_ROUND"),
            "input": os.environ.get("HOOK_TOOL_INPUT"),
        }}
        with open(r"{out_file}", "w", encoding="utf-8") as f:
            json.dump(record, f)
    """
    cmd = _write_hook_script(trusted_workdir, "dump_env", src)
    _write_config(trusted_workdir, {"PreToolUse": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    mgr.run_hooks(
        "PreToolUse",
        HookContext(
            agent_role="subagent",
            tool_name="bash",
            tool_input={"command": "ls"},
            round=7,
        ),
    )
    record = json.loads(out_file.read_text(encoding="utf-8"))
    assert record["tool"] == "bash"
    assert record["role"] == "subagent"
    assert record["round"] == "7"
    assert json.loads(record["input"]) == {"command": "ls"}


def test_posttooluse_carries_status_and_duration(trusted_workdir):
    out_file = trusted_workdir / "post_env.json"
    src = f"""
        import json, os
        record = {{
            "status": os.environ.get("HOOK_OUTPUT_STATUS"),
            "duration": os.environ.get("HOOK_DURATION_MS"),
            "output": os.environ.get("HOOK_TOOL_OUTPUT"),
        }}
        with open(r"{out_file}", "w", encoding="utf-8") as f:
            json.dump(record, f)
    """
    cmd = _write_hook_script(trusted_workdir, "dump_post", src)
    _write_config(trusted_workdir, {"PostToolUse": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    mgr.run_hooks(
        "PostToolUse",
        HookContext(
            tool_name="bash",
            tool_output_text="hello world",
            tool_output_status="CHANGED",
            duration_ms=42,
        ),
    )
    record = json.loads(out_file.read_text(encoding="utf-8"))
    assert record["status"] == "CHANGED"
    assert record["duration"] == "42"
    assert record["output"] == "hello world"


# ---------------- 不支持的事件 ----------------

def test_unknown_event_returns_empty(trusted_workdir):
    cmd = _write_hook_script(trusted_workdir, "ok", "")
    _write_config(trusted_workdir, {"PreToolUse": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    r = mgr.run_hooks("NonExistent", HookContext(tool_name="bash"))
    assert r.blocked is False and r.messages == []


# ---------------- extract_tool_status / stringify ----------------

def test_extract_tool_status_from_toolresult():
    from managers.hook_manager import extract_tool_status, stringify_tool_output
    from utils.loop_control import ToolResult

    assert extract_tool_status("plain string") == "UNKNOWN"
    assert extract_tool_status(ToolResult(content="x", done=True)) == "DONE"
    assert extract_tool_status(ToolResult(content="x", error=True)) == "ERROR"
    assert extract_tool_status(ToolResult(content="x", changed=True)) == "CHANGED"
    assert extract_tool_status(ToolResult(content="x", changed=False)) == "NO_CHANGE"

    # stringify
    assert stringify_tool_output("plain") == "plain"
    assert stringify_tool_output(ToolResult(content="abc", done=True)) == "abc"
    tr = ToolResult(content=[{"type": "text", "text": "hi"}, {"type": "image", "source": {}}])
    assert "hi" in stringify_tool_output(tr)
