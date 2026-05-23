"""
钩子管理器 - 在生命周期事件上挂旁路能力（日志、审计、追踪、策略注入）。

设计与协议见 docs/钩子机制方案.md，参考实现 tests/hook.py。

Phase 1 事件：SessionStart / PreToolUse / PostToolUse
Phase 2 事件：UserPromptSubmit / Stop / SessionEnd / SubagentStop /
              PreCompact / LLMRequest / LLMResponse / GuardTriggered

退出码协议：
    0 — 继续。stdout 若为合法 JSON 可携带 updatedInput / additionalContext / permissionDecision。
    1 — 阻断（PreToolUse / UserPromptSubmit 生效；其他事件退化为 2）。stderr 作为阻断原因。
    2 — 注入 message。stderr 作为注入内容。
    其它 — 视作错误并按 0 处理。

信任门：仅当 .claude/.claude_trusted 存在或 sdk_mode=True 时执行钩子。
失败隔离：钩子任何异常都不会向上抛，最坏退化为 0。

性能注意：LLMRequest / LLMResponse 在每次 LLM 调用都会 fork subprocess（~30-100ms）。
单轮多次工具调用 + 多轮对话场景下累积开销显著。需要 LLM 级追踪时优先考虑：
    1. 用 utils/llm_logger.py（进程内）
    2. 只对采样请求挂钩（在钩子脚本里自己采样）
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import WORKDIR
from utils.logging_setup import get_logger

log = get_logger(__name__)


HOOK_EVENTS = (
    # Phase 1
    "SessionStart", "PreToolUse", "PostToolUse",
    # Phase 2
    "UserPromptSubmit", "Stop", "SessionEnd", "SubagentStop",
    "PreCompact", "LLMRequest", "LLMResponse", "GuardTriggered",
)

# 默认 30 秒；SessionEnd 仿 Claude Code 用 1.5s 硬超时（避免拖慢退出）
HOOK_TIMEOUT = 30
HOOK_TIMEOUTS: Dict[str, float] = {
    "SessionEnd": 1.5,
}

HOOK_ENV_VALUE_MAX = 10000  # 单个环境变量值截断长度

# 允许阻断（exit 1）的事件；其他事件 exit 1 退化为 inject
BLOCKABLE_EVENTS = {"PreToolUse", "UserPromptSubmit"}


@dataclass
class HookContext:
    """钩子调用上下文。

    不是每个事件都用所有字段——根据事件类型按需填。env 映射在 `_build_env`，
    缺省字段不会出现在环境变量里（避免噪声）。
    """
    agent_role: str = "lead"
    cwd: str = ""
    # Tool 事件（Phase 1）
    tool_name: str = ""
    tool_input: Dict[str, Any] = field(default_factory=dict)
    round: int = 0
    call_index: int = 0
    tool_output_text: str = ""
    tool_output_status: str = ""  # DONE / CHANGED / NO_CHANGE / ERROR / UNKNOWN
    duration_ms: int = 0
    error: Optional[str] = None
    # Phase 2
    user_prompt: str = ""          # UserPromptSubmit
    outcome: str = ""              # Stop / SubagentStop
    agent_type: str = ""           # SubagentStop（Explore / general-purpose）
    message_count: int = 0         # PreCompact / LLMRequest
    token_estimate: int = 0        # PreCompact
    llm_model: str = ""            # LLMRequest / LLMResponse
    llm_stop_reason: str = ""      # LLMResponse
    llm_input_tokens: int = 0      # LLMResponse
    llm_output_tokens: int = 0     # LLMResponse
    llm_duration_ms: int = 0       # LLMResponse
    guard_reason: str = ""         # GuardTriggered
    guard_metric: str = ""         # GuardTriggered
    # 兜底：单个钩子要传额外信息时往这里塞，会按 HOOK_EXTRA_<key> 注入环境
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HookResult:
    """钩子聚合结果。"""
    blocked: bool = False
    block_reason: str = ""
    messages: List[str] = field(default_factory=list)
    updated_input: Optional[Dict[str, Any]] = None
    permission_override: Optional[str] = None  # allow / deny


class HookManager:
    """加载并执行 .claude/hooks.json 中定义的钩子。"""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        sdk_mode: bool = False,
        workdir: Optional[Path] = None,
    ):
        self._workdir = workdir or WORKDIR
        self._sdk_mode = sdk_mode
        self.hooks: Dict[str, List[Dict[str, Any]]] = {e: [] for e in HOOK_EVENTS}
        self._config_path = self._resolve_config_path(config_path)
        self._load_config()

    def _resolve_config_path(self, explicit: Optional[Path]) -> Optional[Path]:
        if explicit is not None:
            return explicit
        primary = self._workdir / ".claude" / "hooks.json"
        if primary.exists():
            return primary
        # 回退：教学版用的 WORKDIR/.hooks.json
        fallback = self._workdir / ".hooks.json"
        if fallback.exists():
            return fallback
        return primary  # 不存在也返回主路径，便于日志显示

    def _load_config(self) -> None:
        if not self._config_path or not self._config_path.exists():
            return
        try:
            config = json.loads(self._config_path.read_text(encoding="utf-8"))
            for event in HOOK_EVENTS:
                self.hooks[event] = list(config.get("hooks", {}).get(event, []))
            log.info("[hooks] loaded from %s", self._config_path)
        except Exception as e:
            log.warning("[hooks] config error at %s: %s", self._config_path, e)

    def _trust_marker(self) -> Path:
        return self._workdir / ".claude" / ".claude_trusted"

    def _is_trusted(self) -> bool:
        if self._sdk_mode:
            return True
        return self._trust_marker().exists()

    def is_active(self) -> bool:
        """配置存在且工作区受信。给调用方做 zero-cost 短路用。"""
        if not self._is_trusted():
            return False
        return any(self.hooks.get(e) for e in HOOK_EVENTS)

    def _build_env(self, event: str, ctx: HookContext) -> Dict[str, str]:
        env = dict(os.environ)
        env["HOOK_EVENT"] = event
        env["HOOK_AGENT_ROLE"] = ctx.agent_role
        # Tool 事件公共
        if ctx.tool_name:
            env["HOOK_TOOL_NAME"] = ctx.tool_name
        if ctx.tool_input:
            try:
                env["HOOK_TOOL_INPUT"] = json.dumps(
                    ctx.tool_input, ensure_ascii=False, default=str
                )[:HOOK_ENV_VALUE_MAX]
            except Exception:
                env["HOOK_TOOL_INPUT"] = ""
        if ctx.round:
            env["HOOK_ROUND"] = str(ctx.round)
        if ctx.call_index or event in ("PreToolUse", "PostToolUse"):
            env["HOOK_CALL_INDEX"] = str(ctx.call_index)
        # PostToolUse 专属
        if event == "PostToolUse":
            env["HOOK_TOOL_OUTPUT"] = ctx.tool_output_text[:HOOK_ENV_VALUE_MAX]
            env["HOOK_OUTPUT_STATUS"] = ctx.tool_output_status
            env["HOOK_DURATION_MS"] = str(ctx.duration_ms)
            if ctx.error:
                env["HOOK_ERROR"] = ctx.error[:HOOK_ENV_VALUE_MAX]
        if event == "SessionStart" and ctx.cwd:
            env["HOOK_CWD"] = ctx.cwd
        # Phase 2
        if event == "UserPromptSubmit":
            env["HOOK_USER_PROMPT"] = ctx.user_prompt[:HOOK_ENV_VALUE_MAX]
        if event in ("Stop", "SubagentStop") and ctx.outcome:
            env["HOOK_OUTCOME"] = ctx.outcome[:HOOK_ENV_VALUE_MAX]
        if event == "SubagentStop" and ctx.agent_type:
            env["HOOK_AGENT_TYPE"] = ctx.agent_type
        if event == "PreCompact":
            env["HOOK_MESSAGE_COUNT"] = str(ctx.message_count)
            env["HOOK_TOKEN_ESTIMATE"] = str(ctx.token_estimate)
        if event in ("LLMRequest", "LLMResponse"):
            if ctx.llm_model:
                env["HOOK_LLM_MODEL"] = ctx.llm_model
            if ctx.message_count:
                env["HOOK_MESSAGE_COUNT"] = str(ctx.message_count)
        if event == "LLMResponse":
            if ctx.llm_stop_reason:
                env["HOOK_LLM_STOP_REASON"] = ctx.llm_stop_reason
            env["HOOK_LLM_INPUT_TOKENS"] = str(ctx.llm_input_tokens)
            env["HOOK_LLM_OUTPUT_TOKENS"] = str(ctx.llm_output_tokens)
            env["HOOK_LLM_DURATION_MS"] = str(ctx.llm_duration_ms)
        if event == "GuardTriggered":
            env["HOOK_GUARD_REASON"] = ctx.guard_reason[:HOOK_ENV_VALUE_MAX]
            if ctx.guard_metric:
                env["HOOK_GUARD_METRIC"] = ctx.guard_metric
        # 兜底
        for key, value in ctx.extra.items():
            env[f"HOOK_EXTRA_{key.upper()}"] = str(value)[:HOOK_ENV_VALUE_MAX]
        return env

    def _matches(self, hook_def: Dict[str, Any], ctx: HookContext) -> bool:
        matcher = hook_def.get("matcher", "*")
        if matcher == "*" or not matcher:
            return True
        return matcher == ctx.tool_name

    def _apply_structured_stdout(
        self, stdout: str, result: HookResult, ctx: HookContext
    ) -> None:
        """exit 0 时若 stdout 是合法 JSON，按协议解析。"""
        stdout = (stdout or "").strip()
        if not stdout:
            return
        try:
            payload = json.loads(stdout)
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        if "updatedInput" in payload and isinstance(payload["updatedInput"], dict):
            # 后一个钩子基于前一个的结果；同时更新 ctx 让链式钩子看到最新值
            result.updated_input = payload["updatedInput"]
            ctx.tool_input = payload["updatedInput"]
        if "additionalContext" in payload and isinstance(
            payload["additionalContext"], str
        ):
            result.messages.append(payload["additionalContext"])
        if "permissionDecision" in payload:
            decision = str(payload["permissionDecision"]).lower()
            if decision in ("allow", "deny"):
                result.permission_override = decision
                if decision == "deny":
                    result.blocked = True
                    if not result.block_reason:
                        result.block_reason = "permissionDecision=deny"

    def run_hooks(self, event: str, ctx: HookContext) -> HookResult:
        """执行某事件的所有钩子，聚合结果返回。

        任何钩子的异常都不会向上抛。`blocked=True` 表示 PreToolUse / UserPromptSubmit
        阶段要求跳过本次工具/Prompt。
        """
        result = HookResult()
        if event not in HOOK_EVENTS:
            return result
        if not self._is_trusted():
            return result
        hooks = self.hooks.get(event, [])
        if not hooks:
            return result

        timeout = HOOK_TIMEOUTS.get(event, HOOK_TIMEOUT)
        for hook_def in hooks:
            if not self._matches(hook_def, ctx):
                continue
            command = hook_def.get("command", "")
            if not command:
                continue
            env = self._build_env(event, ctx)
            try:
                r = subprocess.run(
                    command,
                    shell=True,
                    cwd=str(self._workdir),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    encoding="utf-8",
                    errors="replace",
                )
            except subprocess.TimeoutExpired:
                log.warning("[hook:%s] timeout (%.1fs): %s", event, timeout, command)
                continue
            except Exception as e:
                log.warning("[hook:%s] error: %s (cmd=%s)", event, e, command)
                continue

            rc = r.returncode
            stdout = r.stdout or ""
            stderr = (r.stderr or "").strip()

            if rc == 0:
                if stdout.strip():
                    log.info("[hook:%s] %s", event, stdout.strip()[:200])
                self._apply_structured_stdout(stdout, result, ctx)
            elif rc == 1:
                reason = stderr or "Blocked by hook"
                if event in BLOCKABLE_EVENTS:
                    result.blocked = True
                    result.block_reason = reason
                    log.info("[hook:%s] BLOCKED: %s", event, reason[:200])
                    # 阻断后不再跑后续钩子
                    return result
                # 不可阻断事件 → exit 1 退化为 inject
                result.messages.append(reason)
                log.info("[hook:%s] INJECT (rc1→inject): %s", event, reason[:200])
            elif rc == 2:
                if stderr:
                    result.messages.append(stderr)
                    log.info("[hook:%s] INJECT: %s", event, stderr[:200])
            else:
                log.warning("[hook:%s] unexpected exit %d, treating as 0", event, rc)
                if stdout.strip():
                    self._apply_structured_stdout(stdout, result, ctx)
        return result

    def with_role(self, agent_role: str) -> "HookManager":
        """返回共享同一配置但默认 agent_role 不同的视图。

        当前 HookManager 是无状态的（钩子配置只读），三个 agent 共用同一实例即可，
        agent_role 由各自构造 HookContext 时填入。本方法预留作为子 agent /
        teammate 持有"带默认 role"句柄时的便捷方式 —— Phase 1 暂不使用。
        """
        # 浅复制即可：hooks dict 共享
        clone = HookManager.__new__(HookManager)
        clone._workdir = self._workdir
        clone._sdk_mode = self._sdk_mode
        clone.hooks = self.hooks
        clone._config_path = self._config_path
        clone._default_role = agent_role
        return clone


def extract_tool_status(output: Any) -> str:
    """从工具返回值推断 ToolResult 状态串。
    str / list 返回 UNKNOWN；ToolResult 按 done/error/changed 标志映射。
    """
    try:
        from utils.loop_control import ToolResult  # 局部导入避免循环
        if isinstance(output, ToolResult):
            if output.done:
                return "DONE"
            if output.error:
                return "ERROR"
            if output.changed:
                return "CHANGED"
            return "NO_CHANGE"
    except Exception:
        pass
    return "UNKNOWN"


# === 模块级注册：跨模块共享 HookManager ===
# llm_client / compression / main_agent 等不便直接接管理器引用的位置，
# 通过 get_current_hook_manager() 拿到 main.py 启动时注册的实例。
# 未注册时返回 None，所有触发位置应当用 try/except + None 判断兜底。

_CURRENT_HOOK_MANAGER: Optional["HookManager"] = None


def set_current_hook_manager(mgr: Optional["HookManager"]) -> None:
    """注册当前进程的全局 HookManager。main.py 启动时调用一次。"""
    global _CURRENT_HOOK_MANAGER
    _CURRENT_HOOK_MANAGER = mgr


def get_current_hook_manager() -> Optional["HookManager"]:
    """获取当前进程的全局 HookManager；未注册返回 None。"""
    return _CURRENT_HOOK_MANAGER


def fire_hook(event: str, ctx: HookContext) -> HookResult:
    """跨模块触发钩子的便捷函数：自动用全局 HookManager；未注册时返回空结果。

    任何异常都吞掉，绝不向上抛——调用方拿空 HookResult 就够了。
    """
    mgr = _CURRENT_HOOK_MANAGER
    if mgr is None:
        return HookResult()
    try:
        return mgr.run_hooks(event, ctx)
    except Exception as e:
        log.warning("[hook:%s] unexpected error: %s", event, e)
        return HookResult()


def stringify_tool_output(output: Any) -> str:
    """把工具返回值压成一个供钩子审计用的字符串。

    ToolResult.content 是 list（带 image block）时，只把 text block 拼起来。
    """
    try:
        from utils.loop_control import ToolResult
        if isinstance(output, ToolResult):
            content = output.content
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "\n".join(
                    str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"
                )
    except Exception:
        pass
    if isinstance(output, list):
        return "\n".join(
            str(b.get("text", "")) for b in output if isinstance(b, dict) and b.get("type") == "text"
        ) or str(output)
    return str(output)
