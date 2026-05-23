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


# =========================================================================
# Phase 2 测试
# =========================================================================

# ---------------- UserPromptSubmit ----------------

def test_user_prompt_submit_passes_prompt(trusted_workdir):
    out_file = trusted_workdir / "ups.txt"
    src = f"""
        import os
        with open(r"{out_file}", "w", encoding="utf-8") as f:
            f.write(os.environ.get("HOOK_USER_PROMPT", ""))
    """
    cmd = _write_hook_script(trusted_workdir, "dump_ups", src)
    _write_config(trusted_workdir, {"UserPromptSubmit": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    mgr.run_hooks("UserPromptSubmit", HookContext(user_prompt="hello world"))
    assert out_file.read_text(encoding="utf-8") == "hello world"


def test_user_prompt_submit_is_blockable(trusted_workdir):
    """UserPromptSubmit 也属于可阻断事件。"""
    src = """
        import sys
        sys.stderr.write("not allowed")
        sys.exit(1)
    """
    cmd = _write_hook_script(trusted_workdir, "block_prompt", src)
    _write_config(trusted_workdir, {"UserPromptSubmit": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    r = mgr.run_hooks("UserPromptSubmit", HookContext(user_prompt="anything"))
    assert r.blocked is True
    assert "not allowed" in r.block_reason


# ---------------- Stop / SubagentStop ----------------

def test_stop_carries_outcome(trusted_workdir):
    out_file = trusted_workdir / "stop.txt"
    src = f"""
        import os
        with open(r"{out_file}", "w", encoding="utf-8") as f:
            f.write(os.environ.get("HOOK_OUTCOME", ""))
    """
    cmd = _write_hook_script(trusted_workdir, "stop", src)
    _write_config(trusted_workdir, {"Stop": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    mgr.run_hooks("Stop", HookContext(outcome="final answer"))
    assert out_file.read_text(encoding="utf-8") == "final answer"


def test_subagent_stop_carries_agent_type(trusted_workdir):
    out_file = trusted_workdir / "subagent_stop.json"
    src = f"""
        import json, os
        record = {{
            "type": os.environ.get("HOOK_AGENT_TYPE"),
            "outcome": os.environ.get("HOOK_OUTCOME"),
            "role": os.environ.get("HOOK_AGENT_ROLE"),
        }}
        with open(r"{out_file}", "w", encoding="utf-8") as f:
            json.dump(record, f)
    """
    cmd = _write_hook_script(trusted_workdir, "subagent_stop", src)
    _write_config(trusted_workdir, {"SubagentStop": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    mgr.run_hooks(
        "SubagentStop",
        HookContext(agent_role="subagent", agent_type="Explore", outcome="done"),
    )
    record = json.loads(out_file.read_text(encoding="utf-8"))
    assert record == {"type": "Explore", "outcome": "done", "role": "subagent"}


def test_stop_exit_1_degrades_to_inject(trusted_workdir):
    """Stop 不是可阻断事件，exit 1 应退化为 inject。"""
    src = """
        import sys
        sys.stderr.write("non-blockable")
        sys.exit(1)
    """
    cmd = _write_hook_script(trusted_workdir, "stop_block", src)
    _write_config(trusted_workdir, {"Stop": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    r = mgr.run_hooks("Stop", HookContext())
    assert r.blocked is False
    assert r.messages == ["non-blockable"]


# ---------------- SessionEnd（1.5s 超时） ----------------

def test_session_end_has_short_timeout(trusted_workdir, caplog):
    """SessionEnd 用 1.5s 硬超时；超时后不抛错、不注入消息。

    注意：subprocess.run 在 Windows 上 shell=True 的情况下，timeout 杀的是 cmd.exe，
    子 python.exe 会成为孤儿继续跑直到自然退出，因此实际 wall-clock 可能比 1.5s 长。
    这里只断言 "timeout 被 HookManager 识别 + 钩子没成功注入"，不卡精确 wall-clock。
    """
    import logging
    src = """
        import time, sys
        time.sleep(3)
        sys.stderr.write("late-message")
        sys.exit(2)
    """
    cmd = _write_hook_script(trusted_workdir, "slow_end", src)
    _write_config(trusted_workdir, {"SessionEnd": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    with caplog.at_level(logging.WARNING, logger="managers.hook_manager"):
        r = mgr.run_hooks("SessionEnd", HookContext())
    # 必须：
    # 1) 没把超时异常抛上来
    # 2) 没收到 inject 消息（钩子被 1.5s 超时切断，stderr 永远没机会被读到）
    # 3) 日志里能看到 "timeout (1.5s)"
    assert r.messages == []
    assert any("timeout (1.5s)" in rec.getMessage() for rec in caplog.records)


# ---------------- PreCompact ----------------

def test_pre_compact_carries_counts(trusted_workdir):
    out_file = trusted_workdir / "compact.json"
    src = f"""
        import json, os
        record = {{
            "count": os.environ.get("HOOK_MESSAGE_COUNT"),
            "tokens": os.environ.get("HOOK_TOKEN_ESTIMATE"),
        }}
        with open(r"{out_file}", "w", encoding="utf-8") as f:
            json.dump(record, f)
    """
    cmd = _write_hook_script(trusted_workdir, "pc", src)
    _write_config(trusted_workdir, {"PreCompact": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    mgr.run_hooks(
        "PreCompact",
        HookContext(message_count=42, token_estimate=12345),
    )
    record = json.loads(out_file.read_text(encoding="utf-8"))
    assert record == {"count": "42", "tokens": "12345"}


# ---------------- LLMRequest / LLMResponse ----------------

def test_llm_request_response_env(trusted_workdir):
    req_file = trusted_workdir / "req.json"
    res_file = trusted_workdir / "res.json"
    req_src = f"""
        import json, os
        with open(r"{req_file}", "w", encoding="utf-8") as f:
            json.dump({{"model": os.environ.get("HOOK_LLM_MODEL"),
                        "msgs": os.environ.get("HOOK_MESSAGE_COUNT")}}, f)
    """
    res_src = f"""
        import json, os
        with open(r"{res_file}", "w", encoding="utf-8") as f:
            json.dump({{
                "model": os.environ.get("HOOK_LLM_MODEL"),
                "stop": os.environ.get("HOOK_LLM_STOP_REASON"),
                "in": os.environ.get("HOOK_LLM_INPUT_TOKENS"),
                "out": os.environ.get("HOOK_LLM_OUTPUT_TOKENS"),
                "duration": os.environ.get("HOOK_LLM_DURATION_MS"),
            }}, f)
    """
    req_cmd = _write_hook_script(trusted_workdir, "llmreq", req_src)
    res_cmd = _write_hook_script(trusted_workdir, "llmres", res_src)
    _write_config(trusted_workdir, {
        "LLMRequest": [{"command": req_cmd}],
        "LLMResponse": [{"command": res_cmd}],
    })
    mgr = HookManager(workdir=trusted_workdir)
    mgr.run_hooks("LLMRequest", HookContext(llm_model="gpt-5.4", message_count=3))
    mgr.run_hooks(
        "LLMResponse",
        HookContext(
            llm_model="gpt-5.4",
            llm_stop_reason="end_turn",
            llm_input_tokens=120,
            llm_output_tokens=80,
            llm_duration_ms=512,
        ),
    )
    assert json.loads(req_file.read_text(encoding="utf-8")) == {"model": "gpt-5.4", "msgs": "3"}
    assert json.loads(res_file.read_text(encoding="utf-8")) == {
        "model": "gpt-5.4", "stop": "end_turn",
        "in": "120", "out": "80", "duration": "512",
    }


# ---------------- GuardTriggered ----------------

def test_guard_triggered_carries_reason_and_metric(trusted_workdir):
    out_file = trusted_workdir / "guard.json"
    src = f"""
        import json, os
        with open(r"{out_file}", "w", encoding="utf-8") as f:
            json.dump({{
                "reason": os.environ.get("HOOK_GUARD_REASON"),
                "metric": os.environ.get("HOOK_GUARD_METRIC"),
            }}, f)
    """
    cmd = _write_hook_script(trusted_workdir, "g", src)
    _write_config(trusted_workdir, {"GuardTriggered": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)
    mgr.run_hooks(
        "GuardTriggered",
        HookContext(guard_reason="Loop detected: bash", guard_metric="loop_detected"),
    )
    record = json.loads(out_file.read_text(encoding="utf-8"))
    assert record == {"reason": "Loop detected: bash", "metric": "loop_detected"}


# ---------------- 全局 HookManager 注册 ----------------

def test_fire_hook_without_manager_returns_empty():
    """未注册全局 HookManager 时，fire_hook 应返回空 HookResult 而不抛错。"""
    from managers.hook_manager import fire_hook, get_current_hook_manager, set_current_hook_manager
    # 先确保当前是 None
    prev = get_current_hook_manager()
    try:
        set_current_hook_manager(None)
        r = fire_hook("PreCompact", HookContext(message_count=1))
        assert r.blocked is False and r.messages == []
    finally:
        set_current_hook_manager(prev)


def test_fire_hook_uses_registered_manager(trusted_workdir):
    """注册后 fire_hook 走全局实例。"""
    out_file = trusted_workdir / "fired.txt"
    src = f"""
        with open(r"{out_file}", "w", encoding="utf-8") as f:
            f.write("ok")
    """
    cmd = _write_hook_script(trusted_workdir, "fired", src)
    _write_config(trusted_workdir, {"PreCompact": [{"command": cmd}]})
    mgr = HookManager(workdir=trusted_workdir)

    from managers.hook_manager import fire_hook, set_current_hook_manager, get_current_hook_manager
    prev = get_current_hook_manager()
    try:
        set_current_hook_manager(mgr)
        fire_hook("PreCompact", HookContext(message_count=1))
        assert out_file.read_text(encoding="utf-8") == "ok"
    finally:
        set_current_hook_manager(prev)


# ---------------- classify_guard_reason ----------------

def test_classify_guard_reason():
    from agents.main_agent import _classify_guard_reason
    assert _classify_guard_reason("Maximum rounds reached (12)") == "max_rounds"
    assert _classify_guard_reason("Maximum tool calls reached (30)") == "max_tool_calls"
    assert _classify_guard_reason("Too many tool calls in one round (6 > 5)") == "tool_calls_per_round"
    assert _classify_guard_reason("Wall-clock timeout (61.0s > 60s)") == "wall_clock"
    assert _classify_guard_reason("Token budget exceeded (200000 > 150000)") == "token_budget"
    assert _classify_guard_reason("Loop detected: bash") == "loop_detected"
    assert _classify_guard_reason("No progress detected in recent rounds") == "no_progress"
    assert _classify_guard_reason("Something else") == "other"
