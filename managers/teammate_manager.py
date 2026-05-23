"""
队友管理器 - s09/s11: 持久化 Agent 团队
"""
import json
import threading
import time
from typing import Dict, List, Optional, Any

from config.settings import TEAM_DIR, POLL_INTERVAL, IDLE_TIMEOUT, TOKEN_THRESHOLD, WORKDIR
from managers.message_bus import MessageBus
from managers.task_manager import TaskManager
from tools.base_tools import run_bash, run_read, run_write, run_edit
from utils.compression import microcompact, auto_compact, estimate_tokens
from utils.logging_setup import get_logger

log = get_logger(__name__)


def _dispatch_git_tool(name: str, kw: Dict[str, Any]):
    """把 teammate 拿到的 git_* 调用映射到 tools.git_tools。

    限定在 teammate 实际开放的工具集（Read 全集 + add + commit + branch_create）；
    没列出的工具返回 "Unknown tool"——schema 没暴露就不应该被调用。
    """
    from tools.git_tools import (
        git_status, git_current_branch, git_diff, git_log, git_show, git_blame,
        git_branch_list, git_remote_list, git_tag_list,
        git_add, git_commit, git_branch_create,
    )
    handlers = {
        "git_status":         lambda: git_status(),
        "git_current_branch": lambda: git_current_branch(),
        "git_diff":           lambda: git_diff(kw.get("staged", False), kw.get("stat", False), kw.get("path")),
        "git_log":            lambda: git_log(kw.get("limit", 20), kw.get("path")),
        "git_show":           lambda: git_show(kw["ref"], kw.get("stat", False)),
        "git_blame":          lambda: git_blame(kw["path"], kw.get("line_start"), kw.get("line_end")),
        "git_branch_list":    lambda: git_branch_list(),
        "git_remote_list":    lambda: git_remote_list(),
        "git_tag_list":       lambda: git_tag_list(),
        "git_add":            lambda: git_add(kw["paths"]),
        "git_commit":         lambda: git_commit(kw["message"]),
        "git_branch_create":  lambda: git_branch_create(kw["name"]),
    }
    handler = handlers.get(name)
    return handler() if handler else f"Unknown tool: {name}"


class TeammateManager:
    """
    队友管理器 - 持久化 Agent 团队

    特性:
        - 持久化配置
        - 独立的消息历史
        - 自动任务认领
        - 优雅关闭
    """

    def __init__(self, bus: MessageBus, task_mgr: TaskManager, hook_manager=None):
        """
        初始化队友管理器

        Args:
            bus: 消息总线
            task_mgr: 任务管理器
            hook_manager: 钩子管理器（None 时延迟构造空实例）
        """
        TEAM_DIR.mkdir(exist_ok=True)
        self.bus = bus
        self.task_mgr = task_mgr
        self.config_path = TEAM_DIR / "config.json"
        self.config = self._load()
        self.threads: Dict[str, threading.Thread] = {}
        self._hooks = hook_manager

    def set_hook_manager(self, hook_manager):
        """注入钩子管理器。

        因为 MainAgent / TeammateManager 互相依赖循环引用，HookManager 通常先于两者
        构造好后再回填进来。
        """
        self._hooks = hook_manager
    
    def _load(self) -> dict:
        """加载团队配置"""
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}
    
    def _save(self):
        """保存团队配置"""
        self.config_path.write_text(json.dumps(self.config, indent=2))
    
    def _find(self, name: str) -> Optional[dict]:
        """查找队友"""
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None
    
    def spawn(self, name: str, role: str, prompt: str) -> str:
        """
        派生队友 Agent
        
        Args:
            name: 队友名称
            role: 队友角色
            prompt: 初始任务
            
        Returns:
            操作结果消息
        """
        member = self._find(name)
        
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        
        self._save()

        threading.Thread(
            target=self._loop,
            args=(name, role, prompt),
            daemon=True,
            name=f"teammate-{name}",
        ).start()

        active = sum(1 for m in self.config["members"] if m["status"] not in ("shutdown",))
        log.info(
            "teammate spawned: '%s' (role=%s); active=%d total=%d",
            name, role, active, len(self.config["members"]),
        )
        return f"Spawned '{name}' (role: {role})"
    
    def _set_status(self, name: str, status: str):
        """更新队友状态"""
        member = self._find(name)
        if member:
            member["status"] = status
            self._save()
    
    def _loop(self, name: str, role: str, prompt: str):
        """队友主循环（工作阶段 + 空闲阶段）

        外层 try/finally：任何未处理异常都把状态置为 shutdown，避免"线程已死但配置里还显示 working"。
        """
        try:
            self._loop_inner(name, role, prompt)
        except Exception as e:
            # 兜底捕获任何未预期异常——daemon 线程崩溃会被 Python runtime 静默吞掉。
            log.exception("[%s] fatal in _loop: %s", name, e)
        finally:
            # 不管怎么退出（return / break / 异常），最终状态都是 shutdown，
            # 避免 /team 看到的状态和实际线程状态长期不一致。
            self._set_status(name, "shutdown")

    def _loop_inner(self, name: str, role: str, prompt: str):
        """队友主循环的实际逻辑（异常由 _loop 外层兜底）"""
        from core.llm_client import LLMClient
        from managers.hook_manager import HookManager, HookContext, stringify_tool_output, extract_tool_status

        hooks = self._hooks or HookManager()
        hooks_active = hooks.is_active()

        # 线程启动时触发 SessionStart（队友视角）
        if hooks_active:
            try:
                hooks.run_hooks(
                    "SessionStart",
                    HookContext(agent_role=name, cwd=str(WORKDIR)),
                )
            except Exception as e:
                log.warning("[%s] SessionStart hook error: %s", name, e)

        team_name = self.config["team_name"]
        sys_prompt = (
            f"You are '{name}', role: {role}, team: {team_name}. "
            "Use idle when done with current work. You may auto-claim tasks."
        )

        tools = [
            {"name": "bash", "description": "Run command.",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"name": "read_file", "description": "Read file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "write_file", "description": "Write file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Edit file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            {"name": "send_message", "description": "Send message.",
             "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}}, "required": ["to", "content"]}},
            {"name": "idle", "description": "Signal no more work.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "claim_task", "description": "Claim task by ID.",
             "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
            # SSH 只读子集:不给写/上传/下载/add_host/remove_host
            # 队友可观测性差,凭据 / 写操作只能从 Lead-用户对话进入,需要写远端就 send_message 给 Lead
            {"name": "ssh_exec", "description": "Run command on remote host (read-only intent).",
             "input_schema": {"type": "object",
               "properties": {"host": {"type": "string"}, "command": {"type": "string"}},
               "required": ["host", "command"]}},
            {"name": "ssh_list_hosts", "description": "List registered remote hosts.",
             "input_schema": {"type": "object", "properties": {}}},
            # Git Read 全集（Phase A）+ 最小写集（add / commit / branch_create）
            # 不给 stash / unstage / checkout —— 这些属于交互式 / 长事务决策，应回 lead
            {"name": "git_status", "description": "Show git status (porcelain v2).",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "git_current_branch", "description": "Return current branch name.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "git_diff", "description": "Show git diff.",
             "input_schema": {"type": "object", "properties": {
                "staged": {"type": "boolean"}, "stat": {"type": "boolean"}, "path": {"type": "string"}}}},
            {"name": "git_log", "description": "Show commit log (oneline).",
             "input_schema": {"type": "object", "properties": {
                "limit": {"type": "integer"}, "path": {"type": "string"}}}},
            {"name": "git_show", "description": "Show a commit's content.",
             "input_schema": {"type": "object", "properties": {
                "ref": {"type": "string"}, "stat": {"type": "boolean"}}, "required": ["ref"]}},
            {"name": "git_blame", "description": "Show line-level authorship of a file.",
             "input_schema": {"type": "object", "properties": {
                "path": {"type": "string"}, "line_start": {"type": "integer"}, "line_end": {"type": "integer"}},
                "required": ["path"]}},
            {"name": "git_branch_list", "description": "List branches.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "git_remote_list", "description": "List remotes.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "git_tag_list", "description": "List tags.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "git_add", "description": "Stage files.",
             "input_schema": {"type": "object", "properties": {
                "paths": {"type": "array", "items": {"type": "string"}}}, "required": ["paths"]}},
            {"name": "git_commit", "description": "Create a commit from staged changes.",
             "input_schema": {"type": "object", "properties": {
                "message": {"type": "string"}}, "required": ["message"]}},
            {"name": "git_branch_create", "description": "Create + switch to a new branch.",
             "input_schema": {"type": "object", "properties": {
                "name": {"type": "string"}}, "required": ["name"]}},
        ]

        client = LLMClient()
        messages = [{"role": "user", "content": prompt}]

        def reinject_identity():
            """把身份信息插回 history 头部。
            压缩 / 长时间无自我提及之后调用，降低模型角色漂移风险。
            """
            messages.insert(0, {
                "role": "user",
                "content": f"<identity>You are '{name}', role: {role}, team: {team_name}.</identity>",
            })
            messages.insert(1, {
                "role": "assistant",
                "content": f"I am {name}. Continuing.",
            })

        def maybe_compress():
            """工作阶段每轮跑一次：
            - microcompact 把旧 tool_result 改成 [cleared]（结构不变）
            - 超 token 阈值则 auto_compact + 紧接着身份重注入

            为什么压缩后立刻重注入：auto_compact 会把整个 history 摘成 1 条 user 消息，
            模型再读不到"我是谁"——必须立刻补回去，否则下一轮工具调用容易乱。
            """
            microcompact(messages)
            try:
                if estimate_tokens(messages) > TOKEN_THRESHOLD:
                    log.info("[%s] auto-compact triggered", name)
                    messages[:] = auto_compact(messages)
                    reinject_identity()
            except Exception as e:
                # 压缩本身不能成为新故障源——失败就跳过，下一轮再试。
                log.warning("[%s] compress error (ignored): %s", name, e)

        round_idx = 0
        while True:
            # 工作阶段
            for _ in range(50):
                round_idx += 1
                maybe_compress()

                inbox = self.bus.read_inbox(name)
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        return
                    messages.append({"role": "user", "content": json.dumps(msg)})

                try:
                    resp = client.create_message(
                        messages=messages,
                        system=sys_prompt,
                        tools=tools,
                        max_tokens=8000,
                    )
                except Exception as e:
                    log.error("[%s] LLM error: %s", name, e)
                    return

                messages.append({"role": "assistant", "content": resp["content"]})
                if resp["stop_reason"] != "tool_use":
                    break

                results = []
                idle_requested = False
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
                            agent_role=name,
                            tool_name=block["name"],
                            tool_input=tool_input,
                            round=round_idx,
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

                    # 工具执行兜底：任何异常都转成 tool_result 还给 LLM，让它自行决定下一步。
                    output: Any = None
                    error_str: Optional[str] = None
                    t0 = time.monotonic()
                    try:
                        if block["name"] == "idle":
                            idle_requested = True
                            output = "Entering idle phase."
                        elif block["name"] == "claim_task":
                            output = self.task_mgr.claim(tool_input["task_id"], name)
                        elif block["name"] == "send_message":
                            output = self.bus.send(name, tool_input["to"], tool_input["content"])
                        elif block["name"] == "bash":
                            output = run_bash(tool_input["command"])
                        elif block["name"] == "read_file":
                            output = run_read(tool_input["path"])
                        elif block["name"] == "write_file":
                            output = run_write(tool_input["path"], tool_input["content"])
                        elif block["name"] == "edit_file":
                            output = run_edit(
                                tool_input["path"],
                                tool_input["old_text"],
                                tool_input["new_text"],
                            )
                        elif block["name"] == "ssh_exec":
                            from tools.ssh_tools import ssh_exec
                            output = ssh_exec(tool_input["host"], tool_input["command"])
                        elif block["name"] == "ssh_list_hosts":
                            from tools.ssh_tools import ssh_list_hosts
                            output = ssh_list_hosts()
                        elif block["name"].startswith("git_"):
                            # 集中分派给 tools.git_tools，避免一长串 elif
                            output = _dispatch_git_tool(block["name"], tool_input)
                        else:
                            output = f"Unknown tool: {block['name']}"
                    except Exception as e:
                        output = f"Tool {block['name']} failed: {type(e).__name__}: {e}"
                        error_str = f"{type(e).__name__}: {e}"
                    duration_ms = int((time.monotonic() - t0) * 1000)

                    # --- PostToolUse ---
                    post_messages: List[str] = []
                    if hooks_active:
                        post_ctx = HookContext(
                            agent_role=name,
                            tool_name=block["name"],
                            tool_input=tool_input,
                            round=round_idx,
                            call_index=call_index,
                            tool_output_text=stringify_tool_output(output),
                            tool_output_status=extract_tool_status(output),
                            duration_ms=duration_ms,
                            error=error_str,
                        )
                        post = hooks.run_hooks("PostToolUse", post_ctx)
                        post_messages = post.messages

                    log.debug("[%s] %s: %s", name, block["name"], str(output)[:120])

                    # ToolResult → 走 to_llm_format() 拿到 [DONE | CHANGED] 前缀的 LLM 文本；
                    # 裸字符串 → 直接用。修复了之前 SSH/git 工具被 str(dataclass) 渲染成 repr 的问题。
                    from utils.loop_control import ToolResult as _TR
                    if isinstance(output, _TR):
                        rendered = output.to_llm_format()
                        content_text = rendered if isinstance(rendered, str) else str(rendered)
                    else:
                        content_text = str(output)

                    if pre_messages or post_messages:
                        prefix = "\n".join(f"[Hook] {m}" for m in pre_messages)
                        suffix = "\n".join(f"[Hook] {m}" for m in post_messages)
                        content_text = "\n".join(
                            p for p in (prefix, content_text, suffix) if p
                        )

                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": content_text,
                    })
                    call_index += 1

                messages.append({"role": "user", "content": results})
                if idle_requested:
                    break

            # 空闲阶段（自动任务认领）
            self._set_status(name, "idle")
            resume = False
            for _ in range(IDLE_TIMEOUT // max(POLL_INTERVAL, 1)):
                time.sleep(POLL_INTERVAL)

                inbox = self.bus.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            return
                        messages.append({"role": "user", "content": json.dumps(msg)})
                    resume = True
                    break

                unclaimed = self.task_mgr.find_claimable()

                if unclaimed:
                    task = unclaimed[0]
                    # 原子认领：多个空闲队友同时扫到同一任务时，只有一个会成功。
                    # 失败（被别人抢走）就放弃本轮，下一个 tick 再扫。
                    if not self.task_mgr.try_claim(task["id"], name):
                        continue

                    # 身份重注入：在历史很短时补回身份信息，降低压缩后漂移风险
                    if len(messages) <= 3:
                        reinject_identity()

                    messages.append({
                        "role": "user",
                        "content": f"<auto-claimed>Task #{task['id']}: {task['subject']}\n{task.get('description', '')}</auto-claimed>",
                    })
                    messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                    resume = True
                    break

            if not resume:
                # 空闲超时，结束。状态由外层 finally 置 shutdown。
                return

            self._set_status(name, "working")
    
    def list_all(self) -> str:
        """列出所有队友"""
        if not self.config["members"]:
            return "No teammates."
        
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        
        return "\n".join(lines)
    
    def member_names(self) -> List[str]:
        """获取所有队友名称"""
        return [m["name"] for m in self.config["members"]]
