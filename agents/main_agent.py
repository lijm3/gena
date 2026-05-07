"""
主 Agent - s15: Agent 主循环
"""
import json
from typing import List, Dict, Any

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
)
from core.llm_client import LLMClient
from managers.todo_manager import TodoManager
from managers.background_manager import BackgroundManager
from managers.message_bus import MessageBus
from managers.task_manager import TaskManager
from managers.skill_loader import SkillLoader
from managers.teammate_manager import TeammateManager
from tools.tool_dispatcher import ToolDispatcher
from utils.compression import auto_compact, estimate_tokens, microcompact
from utils.loop_control import LoopDetector, ProgressTracker, BudgetController, ToolResult


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
        team_mgr: TeammateManager
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
        """
        self.client = LLMClient()
        self.todo_mgr = todo_mgr
        self.bg_mgr = bg_mgr
        self.bus = bus
        self.task_mgr = task_mgr
        self.skill_loader = skill_loader
        self.team_mgr = team_mgr
        self.tool_dispatcher = ToolDispatcher(
            todo_mgr, skill_loader, task_mgr, bg_mgr, team_mgr, bus
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
        # 初始化预算控制器
        budget_controller = BudgetController(WALL_CLOCK_TIMEOUT, TOKEN_BUDGET)
        
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

            call_llm_count = call_llm_count + 1
            print(f"第{call_llm_count}调用大模型的上下文:{messages}")
            
            # LLM 调用
            response = self._call_llm(messages)

            print(f"第{call_llm_count}调用大模型的返回:{response['content']}")

            messages.append({"role": "assistant", "content": response["content"]})
            
            # 如果不再调用工具，退出循环
            if response["stop_reason"] != "tool_use":
                return messages
            
            # 工具执行阶段前先统计本轮工具数量并执行硬限制。
            tool_calls = [
                block for block in response["content"]
                if block["type"] == "tool_use"
            ]
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
            results, used_todo, manual_compress = self._execute_tools(response)
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
                print("[manual compact]")
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
            print("[auto-compact triggered]")
            messages[:] = auto_compact(messages)
    
    def _call_llm(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """调用 LLM"""
        return self.client.create_message(
            messages=messages,
            system=self.system_prompt,
            tools=self.tool_dispatcher.get_tools(),
            max_tokens=8000
        )



    def _execute_tools(self, response: Dict[str, Any]) -> tuple:
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
        
        for block in response["content"]:
            if block["type"] == "tool_use":
                if block["name"] == "compress":
                    manual_compress = True
                handler = self.tool_dispatcher.get_handler(block["name"])
                try:
                    output = handler(**block["input"]) if handler else f"Unknown tool: {block['name']}"
                except Exception as e:
                    output = f"Error: {e}"
                
                print(f"> {block['name']}:")
                print(str(output)[:200])
                
                tool_output = output.to_llm_format() if isinstance(output, ToolResult) else str(output)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": tool_output
                })
                
                if block["name"] == "TodoWrite":
                    used_todo = True
        
        return results, used_todo, manual_compress

    def _force_conclusion(self, messages: List[Dict[str, Any]], reason: str) -> List[Dict[str, Any]]:
        """
        在触发保护机制时强制收敛：
        1) 注入约束性提示，明确禁止继续调用工具
        2) 调用一次无工具 LLM，产出最终答复
        """
        guardrail = (
            f"<loop-guard>\n"
            f"Stop tool usage now. Reason: {reason}.\n"
            f"Provide final concise answer with what was done, current status, and next action.\n"
            f"</loop-guard>"
        )
        messages.append({"role": "user", "content": guardrail})
        final_response = self.client.create_message(
            messages=messages,
            system=self.system_prompt,
            tools=None,
            max_tokens=1200
        )
        messages.append({"role": "assistant", "content": final_response["content"]})
        return messages
