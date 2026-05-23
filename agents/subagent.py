"""
子 Agent - s04: 派生独立的子 Agent

防护层：
  - LoopDetector / ProgressTracker：阻断重复调用 / 无进展循环
  - 30 轮硬上限作为最后兜底
  - 工具调用 try/except，异常作为 tool_result 反馈给 LLM
  - system prompt 明确身份（Explore 只读 / general-purpose 读写）和工具边界

注意：subagent 是"用完即弃"的短任务，不需要 BudgetController（wall-clock 那种）——
30 轮硬上限 + LoopDetector 已经足够避免烧太多 token。
"""
from typing import List, Dict, Any, Optional
import time

from core.llm_client import LLMClient
from config.settings import (
    LLM_DEBUG_PRINT,
    LOOP_DETECTION_WINDOW,
    LOOP_DETECTION_THRESHOLD,
    PROGRESS_WINDOW,
    PROGRESS_SIMILARITY_THRESHOLD,
)
from utils.loop_control import ToolResult, LoopDetector, ProgressTracker, format_tool_calls
from utils.log_sanitize import sanitize_for_log
from utils.logging_setup import get_logger
from managers.hook_manager import (
    HookManager, HookContext, extract_tool_status, stringify_tool_output,
)

log = get_logger(__name__)

# 进程级累计：本次会话总共起过多少个 subagent。仅做诊断用，不需要持久化。
_SUBAGENT_RUNS_TOTAL = 0

# 子 Agent 硬上限：30 轮工具调用循环
SUB_MAX_ROUNDS = 30

# 不同 agent_type 的 system prompt
_SYS_PROMPT_EXPLORE = (
    "You are a subagent in read-only Explore mode. "
    "You have ONLY read tools (bash, read_file, read_image, ssh_exec, ssh_read, ssh_list_hosts). "
    "DO NOT attempt to write or edit files. "
    "Be concise: gather facts, then summarize findings in the final message. "
    "Stop calling tools once you have enough information to answer. "
    "For remote hosts: call ssh_list_hosts first to see registered names."
)

_SYS_PROMPT_GENERAL = (
    "You are a subagent in general-purpose mode with read AND write tools "
    "(bash, read_file, read_image, write_file, edit_file, ssh_exec, ssh_read, "
    "ssh_write, ssh_upload, ssh_download, ssh_list_hosts). "
    "Complete the task efficiently and summarize what you did in the final message. "
    "Stop calling tools once the task is done. "
    "For remote hosts: call ssh_list_hosts first to see registered names."
)


def run_subagent(prompt: str, agent_type: str = "Explore", hook_manager: Optional[HookManager] = None) -> str:
    """
    派生子 Agent 执行隔离任务

    Args:
        prompt: 子 Agent 的任务描述
        agent_type: Agent 类型
            - "Explore": 只读模式
            - "general-purpose": 完整模式
        hook_manager: 共享的钩子管理器（None 时构造一个空的）

    Returns:
        子 Agent 的工作摘要
    """
    global _SUBAGENT_RUNS_TOTAL
    _SUBAGENT_RUNS_TOTAL += 1
    log.info(
        "subagent started: type=%s session_total=%d prompt=%s",
        agent_type, _SUBAGENT_RUNS_TOTAL, prompt[:80].replace("\n", " "),
    )

    client = LLMClient()
    hooks = hook_manager or HookManager()
    hooks_active = hooks.is_active()
    sys_prompt = _SYS_PROMPT_GENERAL if agent_type != "Explore" else _SYS_PROMPT_EXPLORE

    # 配置子 Agent 的工具集
    sub_tools = [
        {"name": "bash", "description": "Run command.",
         "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "read_file", "description": "Read file.",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
        {"name": "read_image", "description": "Load a local image (PNG/JPEG/WEBP/GIF) so the model can see it.",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
        # SSH 只读三件套:Explore / general 都给
        {"name": "ssh_exec", "description": "Run command on remote host.",
         "input_schema": {"type": "object",
           "properties": {"host": {"type": "string"}, "command": {"type": "string"}},
           "required": ["host", "command"]}},
        {"name": "ssh_read", "description": "Read remote file.",
         "input_schema": {"type": "object",
           "properties": {"host": {"type": "string"}, "path": {"type": "string"}},
           "required": ["host", "path"]}},
        {"name": "ssh_list_hosts", "description": "List registered remote hosts.",
         "input_schema": {"type": "object", "properties": {}}},
    ]

    if agent_type != "Explore":
        sub_tools += [
            {"name": "write_file", "description": "Write file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Edit file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            # SSH 写操作三件套:仅 general-purpose
            {"name": "ssh_write", "description": "Write remote file.",
             "input_schema": {"type": "object",
               "properties": {"host": {"type": "string"}, "path": {"type": "string"},
                              "content": {"type": "string"}},
               "required": ["host", "path", "content"]}},
            {"name": "ssh_upload", "description": "Upload local -> remote.",
             "input_schema": {"type": "object",
               "properties": {"host": {"type": "string"},
                              "local_path": {"type": "string"}, "remote_path": {"type": "string"}},
               "required": ["host", "local_path", "remote_path"]}},
            {"name": "ssh_download", "description": "Download remote -> local.",
             "input_schema": {"type": "object",
               "properties": {"host": {"type": "string"},
                              "remote_path": {"type": "string"}, "local_path": {"type": "string"}},
               "required": ["host", "remote_path", "local_path"]}},
        ]

    # 工具处理函数映射(ssh_add_host / ssh_remove_host 不同步——凭据只能从 Lead-用户对话进入)
    from tools.ssh_tools import (
        ssh_exec as _ssh_exec, ssh_read as _ssh_read, ssh_write as _ssh_write,
        ssh_upload as _ssh_upload, ssh_download as _ssh_download,
        ssh_list_hosts as _ssh_list_hosts,
    )
    sub_handlers = {
        "bash": lambda **kw: run_bash(kw["command"]),
        "read_file": lambda **kw: run_read(kw["path"]),
        "read_image": lambda **kw: run_read_image(kw["path"]),
        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
        "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
        # SSH
        "ssh_exec":       lambda **kw: _ssh_exec(kw["host"], kw["command"]),
        "ssh_read":       lambda **kw: _ssh_read(kw["host"], kw["path"]),
        "ssh_write":      lambda **kw: _ssh_write(kw["host"], kw["path"], kw["content"]),
        "ssh_upload":     lambda **kw: _ssh_upload(kw["host"], kw["local_path"], kw["remote_path"]),
        "ssh_download":   lambda **kw: _ssh_download(kw["host"], kw["remote_path"], kw["local_path"]),
        "ssh_list_hosts": lambda **kw: _ssh_list_hosts(),
    }

    # 循环防护组件
    loop_detector = LoopDetector(LOOP_DETECTION_WINDOW, LOOP_DETECTION_THRESHOLD)
    progress_tracker = ProgressTracker(PROGRESS_WINDOW, PROGRESS_SIMILARITY_THRESHOLD)

    # 创建独立的消息历史
    sub_msgs: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]
    resp: Optional[Dict[str, Any]] = None
    forced_stop_reason: Optional[str] = None

    sub_call_llm = 0
    outcome: Optional[str] = None
    for _ in range(SUB_MAX_ROUNDS):
        sub_call_llm += 1
        log.info("[sub] call #%d messages=%d", sub_call_llm, len(sub_msgs))
        if LLM_DEBUG_PRINT:
            log.debug("[sub-debug] input: %s", sanitize_for_log(sub_msgs))

        try:
            resp = client.create_message(
                messages=sub_msgs,
                system=sys_prompt,
                tools=sub_tools,
                max_tokens=8000,
            )
        except Exception as e:
            outcome = f"(subagent LLM error: {type(e).__name__}: {e})"
            break

        log.info("[sub] call #%d return blocks=%d stop=%s",
                 sub_call_llm, len(resp['content']), resp['stop_reason'])
        if LLM_DEBUG_PRINT:
            log.debug("[sub-debug] return: %s", sanitize_for_log(resp['content']))

        sub_msgs.append({"role": "assistant", "content": resp["content"]})

        if resp["stop_reason"] != "tool_use":
            break

        # 防护 1：重复调用检测
        tool_calls = [b for b in resp["content"] if b["type"] == "tool_use"]
        if tool_calls:
            log.info(
                "[sub] call #%d planning %d tool(s): %s",
                sub_call_llm, len(tool_calls), format_tool_calls(tool_calls),
            )
        loop_hit = False
        for tc in tool_calls:
            if loop_detector.add_call(tc["name"], tc["input"]):
                forced_stop_reason = f"Loop detected: {tc['name']}"
                loop_hit = True
                break
        if loop_hit:
            break

        # 执行工具调用（每个 block 包 try/except，错误转 tool_result 反馈给 LLM）
        results = []
        call_index = 0
        for block in resp["content"]:
            if block["type"] != "tool_use":
                continue

            tool_input = dict(block.get("input") or {})

            # --- PreToolUse ---
            pre_messages: List[str] = []
            pre_blocked = False
            pre_reason = ""
            if hooks_active:
                pre_ctx = HookContext(
                    agent_role="subagent",
                    tool_name=block["name"],
                    tool_input=tool_input,
                    round=sub_call_llm,
                    call_index=call_index,
                )
                pre = hooks.run_hooks("PreToolUse", pre_ctx)
                pre_messages = pre.messages
                pre_blocked = pre.blocked
                pre_reason = pre.block_reason
                if pre.updated_input is not None:
                    tool_input = pre.updated_input

            if pre_blocked:
                blocked_text = f"[Hook 阻断] {pre_reason or 'Blocked by hook'}"
                if pre_messages:
                    blocked_text = "\n".join(
                        [f"[Hook] {m}" for m in pre_messages] + [blocked_text]
                    )
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": blocked_text,
                })
                call_index += 1
                continue

            output: Any = None
            error_str: Optional[str] = None
            t0 = time.monotonic()
            try:
                handler = sub_handlers.get(block["name"])
                if handler is None:
                    output = f"Unknown tool: {block['name']}"
                else:
                    output = handler(**tool_input)
            except Exception as e:
                output = f"Tool {block['name']} failed: {type(e).__name__}: {e}"
                error_str = f"{type(e).__name__}: {e}"
            duration_ms = int((time.monotonic() - t0) * 1000)

            # --- PostToolUse ---
            post_messages: List[str] = []
            if hooks_active:
                post_ctx = HookContext(
                    agent_role="subagent",
                    tool_name=block["name"],
                    tool_input=tool_input,
                    round=sub_call_llm,
                    call_index=call_index,
                    tool_output_text=stringify_tool_output(output),
                    tool_output_status=extract_tool_status(output),
                    duration_ms=duration_ms,
                    error=error_str,
                )
                post = hooks.run_hooks("PostToolUse", post_ctx)
                post_messages = post.messages

            if isinstance(output, ToolResult):
                tool_output = output.to_llm_format()
            elif isinstance(output, list):
                tool_output = output
            else:
                tool_output = str(output)

            # 拼接钩子注入信息
            if pre_messages or post_messages:
                prefix = "\n".join(f"[Hook] {m}" for m in pre_messages)
                suffix = "\n".join(f"[Hook] {m}" for m in post_messages)
                if isinstance(tool_output, list):
                    blocks = []
                    if prefix:
                        blocks.append({"type": "text", "text": prefix})
                    blocks.extend(tool_output)
                    if suffix:
                        blocks.append({"type": "text", "text": suffix})
                    tool_output = blocks
                else:
                    parts = [p for p in (prefix, str(tool_output), suffix) if p]
                    tool_output = "\n".join(parts)

            # list（含 image block）不截断，str 截到 50KB 防爆
            if isinstance(tool_output, str):
                tool_output = tool_output[:50000]
            results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": tool_output,
            })
            call_index += 1
        sub_msgs.append({"role": "user", "content": results})

        # 防护 2：无进展检测
        if progress_tracker.add_result(results):
            forced_stop_reason = "No progress detected in recent rounds"
            break

    # 触发循环防护时，再请 LLM 给一个无工具的最终总结（不污染 sub_msgs）
    if outcome is None and forced_stop_reason and resp is not None:
        guardrail_msg = {
            "role": "user",
            "content": (
                f"<loop-guard>\nStop tool usage. Reason: {forced_stop_reason}. "
                f"Provide a concise final summary of what was found/done.\n</loop-guard>"
            ),
        }
        try:
            final = client.create_message(
                messages=[*sub_msgs, guardrail_msg],
                system=sys_prompt,
                tools=None,
                max_tokens=800,
            )
            text = "".join(b.get("text", "") for b in final.get("content", []) if b.get("type") == "text")
            if text.strip():
                outcome = text
        except Exception:
            pass
        if outcome is None:
            outcome = f"(subagent stopped: {forced_stop_reason})"

    # 提取最终摘要
    if outcome is None:
        if resp:
            outcome = "".join(b["text"] for b in resp["content"] if b["type"] == "text") or "(no summary)"
        else:
            outcome = "(subagent failed)"

    log.info(
        "subagent finished: type=%s rounds=%d stop=%s outcome_len=%d",
        agent_type,
        sub_call_llm,
        forced_stop_reason or "normal",
        len(outcome),
    )
    return outcome


# 导入基础工具
from tools.base_tools import run_bash, run_read, run_write, run_edit, run_read_image
