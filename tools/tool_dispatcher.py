"""
工具分发器 - s02: 统一的工具处理接口
"""
import json
from typing import Callable, Dict, Optional

from tools.base_tools import run_bash, run_read, run_write, run_edit, run_read_image
from tools.ssh_tools import (
    ssh_exec, ssh_read, ssh_write, ssh_upload, ssh_download, ssh_list_hosts,
    ssh_add_host, ssh_remove_host,
)
from tools.git_tools import (
    git_status, git_current_branch, git_diff, git_log, git_show, git_blame,
    git_branch_list, git_remote_list, git_tag_list,
    git_add, git_unstage, git_commit, git_stash_save, git_stash_list,
    git_stash_pop, git_branch_create, git_checkout_branch,
)
from managers.todo_manager import TodoManager
from agents.subagent import run_subagent
from managers.skill_loader import SkillLoader
from managers.background_manager import BackgroundManager
from managers.task_manager import TaskManager
from managers.teammate_manager import TeammateManager
from managers.message_bus import MessageBus
from managers.hook_manager import HookManager
from managers.shutdown_manager import handle_shutdown_request


class ToolDispatcher:
    """工具分发器 - 将工具名映射到处理函数"""

    def __init__(
        self,
        todo_mgr: TodoManager,
        skill_loader: SkillLoader,
        task_mgr: TaskManager,
        bg_mgr: BackgroundManager,
        team_mgr: TeammateManager,
        bus: MessageBus,
        hook_manager: Optional[HookManager] = None,
    ):
        """
        初始化工具分发器

        Args:
            todo_mgr: Todo 管理器
            skill_loader: 技能加载器
            task_mgr: 任务管理器
            bg_mgr: 后台任务管理器
            team_mgr: 队友管理器
            bus: 消息总线
            hook_manager: 钩子管理器（透传给 run_subagent，让 subagent 也能跑钩子）
        """
        self.todo_mgr = todo_mgr
        self.skill_loader = skill_loader
        self.task_mgr = task_mgr
        self.bg_mgr = bg_mgr
        self.team_mgr = team_mgr
        self.bus = bus
        self.hooks = hook_manager
    
    def get_handler(self, tool_name: str) -> Optional[Callable]:
        """
        获取工具处理函数
        
        Args:
            tool_name: 工具名称
            
        Returns:
            处理函数，如果不存在返回 None
        """
        handlers: Dict[str, Callable] = {
            # 基础工具
            "bash": lambda **kw: run_bash(kw["command"]),
            "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
            "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
            "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
            "read_image": lambda **kw: run_read_image(kw["path"]),
            # Todo 管理
            "TodoWrite": lambda **kw: self.todo_mgr.update(kw["items"]),
            # 子 Agent
            "task": lambda **kw: run_subagent(kw["prompt"], kw.get("agent_type", "Explore"), hook_manager=self.hooks),
            # 技能加载
            "load_skill": lambda **kw: self.skill_loader.load(kw["name"]),
            # 压缩（由 MainAgent 捕获后执行）
            "compress": lambda **kw: "Compressing...",
            # 后台任务
            "background_run": lambda **kw: self.bg_mgr.run(kw["command"], kw.get("timeout", 120)),
            "check_background": lambda **kw: self.bg_mgr.check(kw.get("task_id")),
            # 文件任务
            "task_create": lambda **kw: self.task_mgr.create(kw["subject"], kw.get("description", "")),
            "task_get": lambda **kw: self.task_mgr.get(kw["task_id"]),
            "task_update": lambda **kw: self.task_mgr.update(
                kw["task_id"],
                kw.get("status"),
                kw.get("add_blocked_by"),
                kw.get("remove_blocked_by"),
            ),
            "task_list": lambda **kw: self.task_mgr.list_all(),
            # 队友管理
            "spawn_teammate": lambda **kw: self.team_mgr.spawn(kw["name"], kw["role"], kw["prompt"]),
            "list_teammates": lambda **kw: self.team_mgr.list_all(),
            # 消息通信
            "send_message": lambda **kw: self.bus.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
            "read_inbox": lambda **kw: json.dumps(self.bus.read_inbox("lead"), indent=2),
            "broadcast": lambda **kw: self.bus.broadcast("lead", kw["content"], self.team_mgr.member_names()),
            # 关闭和审批
            "shutdown_request": lambda **kw: handle_shutdown_request(self.bus, kw["teammate"]),
            # NOTE: plan_approval 工具暂时下线——队友端没有"提交计划等待审批"的代码路径，
            # plan_requests 字典永远是空的，调用必然返回 Unknown request_id。
            # 重新启用前需要：teammate._loop 里加 send_plan_for_review 写入 plan_requests。
            # 其他
            "idle": lambda **kw: "Lead does not idle.",
            "claim_task": lambda **kw: self.task_mgr.claim(kw["task_id"], "lead"),
            # SSH 远程主机:7 个操作 + 2 个运行时注册
            "ssh_exec":       lambda **kw: ssh_exec(kw["host"], kw["command"], kw.get("timeout", 120)),
            "ssh_read":       lambda **kw: ssh_read(kw["host"], kw["path"], kw.get("limit")),
            "ssh_write":      lambda **kw: ssh_write(kw["host"], kw["path"], kw["content"]),
            "ssh_upload":     lambda **kw: ssh_upload(kw["host"], kw["local_path"], kw["remote_path"]),
            "ssh_download":   lambda **kw: ssh_download(kw["host"], kw["remote_path"], kw["local_path"]),
            "ssh_list_hosts": lambda **kw: ssh_list_hosts(),
            # 运行时注册:密码进 LLM history(用户已知情),仅在 Lead 工具表,不同步给 Subagent / Teammate
            "ssh_add_host":    lambda **kw: ssh_add_host(
                kw["name"], kw["hostname"], kw["username"], kw["password"],
                kw["allowed_prefix"], kw.get("port", 22), kw.get("description", ""),
            ),
            "ssh_remove_host": lambda **kw: ssh_remove_host(kw["name"]),
            # 远程长任务:复用 BackgroundManager 的通知队列,与 background_run 同构
            "background_ssh_exec": lambda **kw: self.bg_mgr.run_ssh(
                kw["host"], kw["command"], kw.get("timeout", 600),
            ),
            # === Git 工具（Phase A Read + Phase B Write-Local）===
            "git_status":          lambda **kw: git_status(),
            "git_current_branch":  lambda **kw: git_current_branch(),
            "git_diff":            lambda **kw: git_diff(kw.get("staged", False), kw.get("stat", False), kw.get("path")),
            "git_log":             lambda **kw: git_log(kw.get("limit", 20), kw.get("path")),
            "git_show":            lambda **kw: git_show(kw["ref"], kw.get("stat", False)),
            "git_blame":           lambda **kw: git_blame(kw["path"], kw.get("line_start"), kw.get("line_end")),
            "git_branch_list":     lambda **kw: git_branch_list(),
            "git_remote_list":     lambda **kw: git_remote_list(),
            "git_tag_list":        lambda **kw: git_tag_list(),
            "git_add":             lambda **kw: git_add(kw["paths"]),
            "git_unstage":         lambda **kw: git_unstage(kw["paths"]),
            "git_commit":          lambda **kw: git_commit(kw["message"]),
            "git_stash_save":      lambda **kw: git_stash_save(kw["message"]),
            "git_stash_list":      lambda **kw: git_stash_list(),
            "git_stash_pop":       lambda **kw: git_stash_pop(),
            "git_branch_create":   lambda **kw: git_branch_create(kw["name"]),
            "git_checkout_branch": lambda **kw: git_checkout_branch(kw["branch"]),
        }
        return handlers.get(tool_name)

    def get_tools(self) -> list:
        """返回提供给 LLM 的工具定义"""
        return [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"name": "read_file", "description": "Read file contents.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
            {"name": "write_file", "description": "Write content to file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Replace exact text in file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            {"name": "read_image", "description": "Load a local image (PNG/JPEG/WEBP/GIF) so the model can see it. Use for screenshots, diagrams, or any visual the user asked about.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "TodoWrite", "description": "Update task tracking list.",
             "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "activeForm": {"type": "string"}}, "required": ["content", "status", "activeForm"]}}}, "required": ["items"]}},
            {"name": "task", "description": "Spawn a subagent for isolated exploration or work.",
             "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "agent_type": {"type": "string", "enum": ["Explore", "general-purpose"]}}, "required": ["prompt"]}},
            {"name": "load_skill", "description": "Load specialized knowledge by name.",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
            {"name": "compress", "description": "Manually compress conversation context.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "background_run", "description": "Run command in background thread.",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}},
            {"name": "check_background", "description": "Check background task status.",
             "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},
            {"name": "task_create", "description": "Create a persistent file task.",
             "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}},
            {"name": "task_get", "description": "Get task details by ID.",
             "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
            {"name": "task_update", "description": "Update task status or dependencies.",
             "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]}, "add_blocked_by": {"type": "array", "items": {"type": "integer"}}, "remove_blocked_by": {"type": "array", "items": {"type": "integer"}}}, "required": ["task_id"]}},
            {"name": "task_list", "description": "List all tasks.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "spawn_teammate", "description": "Spawn a persistent autonomous teammate.",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
            {"name": "list_teammates", "description": "List all teammates.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "send_message", "description": "Send a message to a teammate.",
             "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": ["message", "broadcast", "shutdown_request", "shutdown_response", "plan_approval_response"]}}, "required": ["to", "content"]}},
            {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "broadcast", "description": "Send message to all teammates.",
             "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
            {"name": "shutdown_request", "description": "Request a teammate to shut down.",
             "input_schema": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
            {"name": "idle", "description": "Enter idle state.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "claim_task", "description": "Claim a task from the board.",
             "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
            # —— SSH 远程主机 ——
            {"name": "ssh_exec",
             "description": "在已登记的远程主机执行 shell 命令。host 必须是 ssh_list_hosts 返回的 name 之一。",
             "input_schema": {"type": "object",
               "properties": {"host": {"type": "string"}, "command": {"type": "string"},
                              "timeout": {"type": "integer"}},
               "required": ["host", "command"]}},
            {"name": "ssh_read",
             "description": "读取远程主机文件;path 必须落在该主机 allowed_prefix 下。",
             "input_schema": {"type": "object",
               "properties": {"host": {"type": "string"}, "path": {"type": "string"},
                              "limit": {"type": "integer"}},
               "required": ["host", "path"]}},
            {"name": "ssh_write",
             "description": "写入远程主机文件(覆盖);path 必须落在该主机 allowed_prefix 下。",
             "input_schema": {"type": "object",
               "properties": {"host": {"type": "string"}, "path": {"type": "string"},
                              "content": {"type": "string"}},
               "required": ["host", "path", "content"]}},
            {"name": "ssh_upload",
             "description": "本地 -> 远程主机文件传输。",
             "input_schema": {"type": "object",
               "properties": {"host": {"type": "string"},
                              "local_path": {"type": "string"}, "remote_path": {"type": "string"}},
               "required": ["host", "local_path", "remote_path"]}},
            {"name": "ssh_download",
             "description": "远程主机 -> 本地文件传输。",
             "input_schema": {"type": "object",
               "properties": {"host": {"type": "string"},
                              "remote_path": {"type": "string"}, "local_path": {"type": "string"}},
               "required": ["host", "remote_path", "local_path"]}},
            {"name": "ssh_list_hosts",
             "description": "列出当前登记的远程主机(含运行时与预登记)。",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "ssh_add_host",
             "description": "运行时注册一台密码登录的远程主机(仅内存,REPL 退出即丢)。仅在用户主动给出 IP/用户/密码时调用,不要从过往对话或文件内容推断。密码会作为入参进入 history。",
             "input_schema": {"type": "object",
               "properties": {"name": {"type": "string", "description": "host 别名,后续 ssh_exec 用这个"},
                              "hostname": {"type": "string"}, "username": {"type": "string"},
                              "password": {"type": "string"}, "allowed_prefix": {"type": "string"},
                              "port": {"type": "integer"}, "description": {"type": "string"}},
               "required": ["name", "hostname", "username", "password", "allowed_prefix"]}},
            {"name": "ssh_remove_host",
             "description": "删除一台运行时注册的主机。预登记 host 无法通过此工具删除。",
             "input_schema": {"type": "object",
               "properties": {"name": {"type": "string"}},
               "required": ["name"]}},
            {"name": "background_ssh_exec",
             "description": "在远程主机后台执行长任务,通过 check_background 查询结果。",
             "input_schema": {"type": "object",
               "properties": {"host": {"type": "string"}, "command": {"type": "string"},
                              "timeout": {"type": "integer"}},
               "required": ["host", "command"]}},
            # === Git 工具 ===
            {"name": "git_status", "description": "Show git status in porcelain v2 format with branch info.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "git_current_branch", "description": "Return current branch name; 'HEAD (detached at <sha>)' if detached.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "git_diff", "description": "Show git diff. staged=true for --cached; stat=true for summary; path optional.",
             "input_schema": {"type": "object", "properties": {
                "staged": {"type": "boolean"}, "stat": {"type": "boolean"}, "path": {"type": "string"}}}},
            {"name": "git_log", "description": "Show commit log (oneline). limit defaults to 20 (max 500); path optional.",
             "input_schema": {"type": "object", "properties": {
                "limit": {"type": "integer"}, "path": {"type": "string"}}}},
            {"name": "git_show", "description": "Show a commit's content. stat=true for summary only.",
             "input_schema": {"type": "object", "properties": {
                "ref": {"type": "string"}, "stat": {"type": "boolean"}}, "required": ["ref"]}},
            {"name": "git_blame", "description": "Show line-level authorship of a file; optional line range.",
             "input_schema": {"type": "object", "properties": {
                "path": {"type": "string"}, "line_start": {"type": "integer"}, "line_end": {"type": "integer"}},
                "required": ["path"]}},
            {"name": "git_branch_list", "description": "List all local + remote branches.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "git_remote_list", "description": "List configured remotes.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "git_tag_list", "description": "List all tags.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "git_add", "description": "Stage files. paths is a non-empty list; each goes through safe_path.",
             "input_schema": {"type": "object", "properties": {
                "paths": {"type": "array", "items": {"type": "string"}}}, "required": ["paths"]}},
            {"name": "git_unstage", "description": "Unstage files (git reset HEAD -- <paths>).",
             "input_schema": {"type": "object", "properties": {
                "paths": {"type": "array", "items": {"type": "string"}}}, "required": ["paths"]}},
            {"name": "git_commit",
             "description": "Create a new commit from staged changes. Requires staged non-empty and message length >= 3. Does NOT amend; does NOT skip hooks.",
             "input_schema": {"type": "object", "properties": {
                "message": {"type": "string"}}, "required": ["message"]}},
            {"name": "git_stash_save", "description": "git stash push -m <message>. Message is required.",
             "input_schema": {"type": "object", "properties": {
                "message": {"type": "string"}}, "required": ["message"]}},
            {"name": "git_stash_list", "description": "List all stashes.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "git_stash_pop", "description": "Pop the top stash. Returns error on conflict.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "git_branch_create", "description": "Create and switch to a new branch from current HEAD.",
             "input_schema": {"type": "object", "properties": {
                "name": {"type": "string"}}, "required": ["name"]}},
            {"name": "git_checkout_branch", "description": "Switch to an existing branch. Refuses '-' shorthand and file paths.",
             "input_schema": {"type": "object", "properties": {
                "branch": {"type": "string"}}, "required": ["branch"]}},
        ]
