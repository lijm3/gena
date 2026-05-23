"""
主 Agent - s15: Agent 主循环
"""
import json
import time
from typing import List, Dict, Any, Optional

from config.settings import (
    WORKDIR,
    TOKEN_THRESHOLD,
    MAX_AGENT_ROUNDS,
    MAX_TOOL_CALLS,
    MAX_TOOL_CALLS_PER_ROUND,
    LOOP_DETECTION_WINDOW,
    LOOP_DETECTION_THRESHOLD,
    PROGRESS_WINDOW,
    PROGRESS_SIMILARITY_THRESHOLD,
    WALL_CLOCK_TIMEOUT,
    TOKEN_BUDGET,
    LLM_DEBUG_PRINT,
)
from core.llm_client import LLMClient
from managers.todo_manager import TodoManager
from managers.background_manager import BackgroundManager
from managers.message_bus import MessageBus
from managers.task_manager import TaskManager
from managers.skill_loader import SkillLoader
from managers.teammate_manager import TeammateManager
from managers.hook_manager import (
    HookManager, HookContext, extract_tool_status, stringify_tool_output,
)
from tools.tool_dispatcher import ToolDispatcher
from utils.compression import auto_compact, estimate_tokens, microcompact
from utils.loop_control import LoopDetector, ProgressTracker, BudgetController, ToolResult, format_tool_calls
from utils.log_sanitize import sanitize_for_log
from utils.logging_setup import get_logger

log = get_logger(__name__)

# 网关偶尔会返回空 content + stop_reason=end_turn（疑似缓存/路由命中后无生成）。
# 不做兜底的话用户在 REPL 里看到"无任何回应"，这里给一次"请重新作答"的注入重试。
MAX_EMPTY_RETRIES = 2


def _classify_guard_reason(reason: str) -> str:
    """把 _force_conclusion 的 reason 文本归到一个稳定的 metric 标签。

    供 GuardTriggered 钩子用，方便外部按 metric 聚合（hard_cap / budget / loop / progress）。
    """
    text = reason.lower()
    if "wall-clock timeout" in text:
        return "wall_clock"
    if "token budget" in text:
        return "token_budget"
    if "maximum rounds" in text:
        return "max_rounds"
    if "too many tool calls" in text and "one round" in text:
        return "tool_calls_per_round"
    if "maximum tool calls" in text:
        return "max_tool_calls"
    if "loop detected" in text:
        return "loop_detected"
    if "no progress" in text:
        return "no_progress"
    return "other"


class MainAgent:
    """
    主 Agent - 协调所有机制
    
    特性:
        - 压缩（s06）
        - 后台通知（s08）
        - 收件箱（s09）
        - LLM 调用
        - 工具执行（s02）
        - Todo 提醒（s03）
    """
    
    def __init__(
        self,
        todo_mgr: TodoManager,
        bg_mgr: BackgroundManager,
        bus: MessageBus,
        task_mgr: TaskManager,
        skill_loader: SkillLoader,
        team_mgr: TeammateManager,
        hook_manager: Optional[HookManager] = None,
    ):
        """
        初始化主 Agent

        Args:
            todo_mgr: Todo 管理器
            bg_mgr: 后台任务管理器
            bus: 消息总线
            task_mgr: 任务管理器
            skill_loader: 技能加载器
            team_mgr: 队友管理器
            hook_manager: 钩子管理器（可选，None 时构造一个空的）
        """
        self.client = LLMClient()
        self.todo_mgr = todo_mgr
        self.bg_mgr = bg_mgr
        self.bus = bus
        self.task_mgr = task_mgr
        self.skill_loader = skill_loader
        self.team_mgr = team_mgr
        self.hooks = hook_manager or HookManager()
        self.tool_dispatcher = ToolDispatcher(
            todo_mgr, skill_loader, task_mgr, bg_mgr, team_mgr, bus,
            hook_manager=self.hooks,
        )
        
        self.system_prompt = f"""You are a coding agent at {WORKDIR}. Use tools to solve tasks.
Prefer task_create/task_update/task_list for multi-step work. Use TodoWrite for short checklists.
Use task for subagent delegation. Use load_skill for specialized knowledge.
Skills: {skill_loader.descriptions()}

CRITICAL LOOP PREVENTION RULES:
1. Maximum {MAX_AGENT_ROUNDS} tool-calling rounds per request.
2. Maximum {MAX_TOOL_CALLS} total tool calls per request.
3. Maximum {MAX_TOOL_CALLS_PER_ROUND} tool calls in one round.
4. If tool returns NO_CHANGE twice, avoid repeating same call.
5. If no progress after several rounds, conclude with summary.
6. Approaching limits: prioritize final answer over more exploration.
"""
        # 多层循环控制组件：分别负责重复调用检测、结果进展检测和预算约束。
        self.loop_detector = LoopDetector(LOOP_DETECTION_WINDOW, LOOP_DETECTION_THRESHOLD)
        self.progress_tracker = ProgressTracker(PROGRESS_WINDOW, PROGRESS_SIMILARITY_THRESHOLD)
    
    def agent_loop(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        主 Agent 循环
        
        Args:
            messages: 消息历史
            
        Returns:
            更新后的消息历史
        """
        # 初始化变量
        rounds_without_todo = 0
        # 初始化工具调用次数
        total_tool_calls = 0
        # 初始化轮次
        rounds = 0
        # 初始化调用大模型次数
        call_llm_count = 0
        # 空响应连续计数（一旦本轮收到非空响应会清零）
        empty_retries = 0
        # 初始化预算控制器
        budget_controller = BudgetController(WALL_CLOCK_TIMEOUT, TOKEN_BUDGET)

        # 重置跨请求的循环检测状态。loop_detector / progress_tracker 是 MainAgent 的实例属性，
        # 不清空会导致上一次对话里的工具指纹/结果摘要继续命中本次：例如用户两次让我"重读同一张图"，
        # 第二次的 read_image 会被误判为循环，直接 _force_conclusion 静默退出。
        # 这两个保护机制的语义是"单次请求内的循环防护"，每个 turn 重新开始即可。
        self.loop_detector = LoopDetector(LOOP_DETECTION_WINDOW, LOOP_DETECTION_THRESHOLD)
        self.progress_tracker = ProgressTracker(PROGRESS_WINDOW, PROGRESS_SIMILARITY_THRESHOLD)
        
        while True:
            rounds += 1
            # 硬上限 1：轮次超限时强制进入收敛，避免出现无尽“思考-调工具”。
            if rounds > MAX_AGENT_ROUNDS:
                return self._force_conclusion(messages, f"Maximum rounds reached ({MAX_AGENT_ROUNDS})")
            # 硬上限 2：工具总调用次数超限时收敛。
            if total_tool_calls >= MAX_TOOL_CALLS:
                return self._force_conclusion(messages, f"Maximum tool calls reached ({MAX_TOOL_CALLS})")
            # 预算约束 1：wall-clock 超时保护。
            timeout, timeout_msg = budget_controller.check_timeout()
            if timeout:
                return self._force_conclusion(messages, timeout_msg)

            # 预处理阶段
            self._preprocess(messages)
            # 预算约束 2：token 预算保护，防止上下文膨胀导致成本失控。
            current_tokens = estimate_tokens(messages)
            over_budget, budget_msg = budget_controller.check_token_budget(current_tokens)
            if over_budget:
                return self._force_conclusion(messages, budget_msg)

            call_llm_count += 1
            log.info("[llm] call #%d messages=%d", call_llm_count, len(messages))
            if LLM_DEBUG_PRINT:
                log.debug("[llm-debug] input: %s", sanitize_for_log(messages))

            # LLM 调用
            response = self._call_llm(messages)

            log.info("[llm] call #%d return blocks=%d stop=%s",
                     call_llm_count, len(response['content']), response['stop_reason'])
            if LLM_DEBUG_PRINT:
                log.debug("[llm-debug] return: %s", sanitize_for_log(response['content']))

            messages.append({"role": "assistant", "content": response["content"]})

            # 空响应兜底：网关偶发返回 blocks=0 + stop=end_turn，没有任何文本/工具调用。
            # 静默退出会让用户在 REPL 里看到"无回应"。先撤掉刚才那条空 assistant，
            # 注入一条提示让模型重试；累计超过 MAX_EMPTY_RETRIES 就给固定兜底文案。
            if not response["content"] and response["stop_reason"] != "tool_use":
                messages.pop()
                if empty_retries < MAX_EMPTY_RETRIES:
                    empty_retries += 1
                    log.warning(
                        "[llm] call #%d empty response, retrying (%d/%d)",
                        call_llm_count, empty_retries, MAX_EMPTY_RETRIES,
                    )
                    messages.append({
                        "role": "user",
                        "content": (
                            "<empty-response-recovery>\n"
                            "Your previous response was empty. Please answer the user's "
                            "latest message directly, or call a tool if needed.\n"
                            "</empty-response-recovery>"
                        ),
                    })
                    continue
                log.error("[llm] call #%d empty response after %d retries, giving up",
                          call_llm_count, MAX_EMPTY_RETRIES)
                messages.append({
                    "role": "assistant",
                    "content": [{"type": "text", "text": "（模型返回了空响应，请重新提问或换种问法。）"}],
                })
                return messages
            empty_retries = 0

            # 如果不再调用工具，退出循环
            if response["stop_reason"] != "tool_use":
                return messages
            
            # 工具执行阶段前先统计本轮工具数量并执行硬限制。
            tool_calls = [
                block for block in response["content"]
                if block["type"] == "tool_use"
            ]
            # 把本轮将要调用的工具列出来——日志里"先说想做什么，再说做了什么"
            # 即使被硬上限/LoopDetector 拦下，也能看到模型的意图。
            if tool_calls:
                log.info(
                    "[llm] call #%d planning %d tool(s): %s",
                    call_llm_count, len(tool_calls), format_tool_calls(tool_calls),
                )
            if len(tool_calls) > MAX_TOOL_CALLS_PER_ROUND:
                return self._force_conclusion(
                    messages,
                    f"Too many tool calls in one round ({len(tool_calls)} > {MAX_TOOL_CALLS_PER_ROUND})"
                )

            # 重复调用检测：在执行前拦截“同名+同参”高频调用。
            for tool_call in tool_calls:
                if self.loop_detector.add_call(tool_call["name"], tool_call["input"]):
                    return self._force_conclusion(messages, f"Loop detected: {tool_call['name']}")

            # 工具执行阶段
            results, used_todo, manual_compress = self._execute_tools(response, rounds)
            total_tool_calls += len(tool_calls)

            # 无进展检测：连续多轮结果高度相似时，直接收敛总结。
            if self.progress_tracker.add_result(results):
                return self._force_conclusion(messages, "No progress detected in recent rounds")
            
            # Todo 提醒机制
            rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
            if self.todo_mgr.has_open_items() and rounds_without_todo >= 3:
                results.append({
                    "type": "text",
                    "text": "<reminder>Update your todos.</reminder>"
                })
            
            messages.append({"role": "user", "content": results})

            if manual_compress:
                log.info("[manual compact]")
                messages[:] = auto_compact(messages)
                return messages
    
    def _preprocess(self, messages: List[Dict[str, Any]]):
        """预处理阶段"""
        # 压缩管道
        self._compress(messages)
        
        # 排空后台通知
        notifs = self.bg_mgr.drain()
        if notifs:
            txt = "\n".join(f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs)
            messages.append({"role": "user", "content": f"<background-results>\n{txt}\n</background-results>"})
        
        # 检查收件箱
        inbox = self.bus.read_inbox("lead")
        if inbox:
            messages.append({"role": "user", "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>"})
    
    def _compress(self, messages: List[Dict[str, Any]]):
        """压缩管道"""
        # 微压缩
        microcompact(messages)
        
        # 自动压缩
        if estimate_tokens(messages) > TOKEN_THRESHOLD:
            log.info("[auto-compact triggered]")
            messages[:] = auto_compact(messages)
    
    def _call_llm(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """调用 LLM"""
        return self.client.create_message(
            messages=messages,
            system=self.system_prompt,
            tools=self.tool_dispatcher.get_tools(),
            max_tokens=8000
        )



    def _execute_tools(self, response: Dict[str, Any], round_num: int = 0) -> tuple:
        """
        results

        类型：list
        含义：本轮所有工具执行结果（tool_result 列表），后面会塞回 messages 给模型继续推理。
        used_todo

        类型：bool
        含义：本轮是否调用过 TodoWrite。
        用途：控制 todo 提醒计数（调用了就清零；连续几轮不调且有未完成 todo 就提醒）。
        manual_compress

        类型：bool
        含义：本轮是否调用过 compress 工具。
        用途：若为 True，主循环会立刻执行一次 auto_compact(messages) 并结束当前轮
        """
        """执行工具"""
        results = []
        used_todo = False
        manual_compress = False
        hooks_active = self.hooks.is_active()

        call_index = 0
        for block in response["content"]:
            if block["type"] != "tool_use":
                continue
            if block["name"] == "compress":
                manual_compress = True

            # 拷贝 tool_input 避免被钩子的 updatedInput 直接改到 messages 历史里
            tool_input = dict(block.get("input") or {})

            # --- PreToolUse ---
            pre_messages: List[str] = []
            pre_blocked = False
            pre_reason = ""
            if hooks_active:
                pre_ctx = HookContext(
                    agent_role="lead",
                    tool_name=block["name"],
                    tool_input=tool_input,
                    round=round_num,
                    call_index=call_index,
                )
                pre = self.hooks.run_hooks("PreToolUse", pre_ctx)
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

            # --- 执行工具 ---
            handler = self.tool_dispatcher.get_handler(block["name"])
            t0 = time.monotonic()
            error_str: Optional[str] = None
            try:
                output = handler(**tool_input) if handler else f"Unknown tool: {block['name']}"
            except Exception as e:
                output = f"Error: {e}"
                error_str = f"{type(e).__name__}: {e}"
            duration_ms = int((time.monotonic() - t0) * 1000)

            log.info("> %s: %s", block["name"], str(output)[:200])

            # --- PostToolUse ---
            post_messages: List[str] = []
            if hooks_active:
                post_ctx = HookContext(
                    agent_role="lead",
                    tool_name=block["name"],
                    tool_input=tool_input,
                    round=round_num,
                    call_index=call_index,
                    tool_output_text=stringify_tool_output(output),
                    tool_output_status=extract_tool_status(output),
                    duration_ms=duration_ms,
                    error=error_str,
                )
                post = self.hooks.run_hooks("PostToolUse", post_ctx)
                post_messages = post.messages

            # --- 组装 tool_result ---
            if isinstance(output, ToolResult):
                tool_output = output.to_llm_format()
            elif isinstance(output, list):
                tool_output = output
            else:
                tool_output = str(output)

            tool_output = self._wrap_hook_messages(tool_output, pre_messages, post_messages)
            results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": tool_output
            })

            # sidecar_blocks：read_image 之类的工具把 image block 放在这里，
            # 而不是塞进 tool_result.content——很多第三方 Anthropic 兼容网关
            # 不识别 tool_result 内部的 image，必须把图片提到 user message 顶层。
            if isinstance(output, ToolResult):
                sidecar = output.metadata.get("sidecar_blocks")
                if sidecar:
                    results.extend(sidecar)

            if block["name"] == "TodoWrite":
                used_todo = True
            call_index += 1

        return results, used_todo, manual_compress

    @staticmethod
    def _wrap_hook_messages(tool_output, pre_messages: List[str], post_messages: List[str]):
        """把钩子注入的 message 拼到 tool_result.content 前后。

        - str：直接前后字符串拼接
        - list（image 透传场景）：在头/尾各加一个 text block
        """
        if not pre_messages and not post_messages:
            return tool_output
        prefix = "\n".join(f"[Hook] {m}" for m in pre_messages)
        suffix = "\n".join(f"[Hook] {m}" for m in post_messages)
        if isinstance(tool_output, list):
            blocks = []
            if prefix:
                blocks.append({"type": "text", "text": prefix})
            blocks.extend(tool_output)
            if suffix:
                blocks.append({"type": "text", "text": suffix})
            return blocks
        parts = []
        if prefix:
            parts.append(prefix)
        parts.append(str(tool_output))
        if suffix:
            parts.append(suffix)
        return "\n".join(parts)

    def _force_conclusion(self, messages: List[Dict[str, Any]], reason: str) -> List[Dict[str, Any]]:
        """
        在触发保护机制时强制收敛：
        1) 临时拼接约束性提示（不写入 messages），明确禁止继续调用工具
        2) 调用一次无工具 LLM，产出最终答复
        3) 仅把最终答复写入 messages

        为什么 guardrail 不进 history：
        如果 <loop-guard>...Stop tool usage now...</loop-guard> 留在历史里，
        用户下一轮提问时模型会读到"停止使用工具"的指令，导致新问题里也不调工具
        而是瞎答。临时拼接到 LLM 调用但不污染持久 history 是更干净的做法。
        """
        log.warning("[force-conclusion] triggered: %s", reason)

        # GuardTriggered 钩子：把闸门命中事件抛给外部 metrics/审计（observer-only）
        try:
            self.hooks.run_hooks(
                "GuardTriggered",
                HookContext(
                    agent_role="lead",
                    guard_reason=reason,
                    guard_metric=_classify_guard_reason(reason),
                ),
            )
        except Exception as e:
            log.warning("GuardTriggered hook error (ignored): %s", e)

        guardrail_msg = {
            "role": "user",
            "content": (
                f"<loop-guard>\n"
                f"Stop tool usage now. Reason: {reason}.\n"
                f"Provide final concise answer with what was done, current status, and next action.\n"
                f"</loop-guard>"
            ),
        }
        final_response = self.client.create_message(
            messages=[*messages, guardrail_msg],
            system=self.system_prompt,
            tools=None,
            max_tokens=1200,
        )
        messages.append({"role": "assistant", "content": final_response["content"]})
        return messages
