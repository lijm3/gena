"""
主 Agent - s15: Agent 主循环
"""
import json
from typing import List, Dict, Any

from config.settings import WORKDIR, TOKEN_THRESHOLD
from core.llm_client import LLMClient
from managers.todo_manager import TodoManager
from managers.background_manager import BackgroundManager
from managers.message_bus import MessageBus
from managers.task_manager import TaskManager
from managers.skill_loader import SkillLoader
from managers.teammate_manager import TeammateManager
from tools.tool_dispatcher import ToolDispatcher
from utils.compression import auto_compact, estimate_tokens, microcompact


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
Skills: {skill_loader.descriptions()}"""
    
    def agent_loop(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        主 Agent 循环
        
        Args:
            messages: 消息历史
            
        Returns:
            更新后的消息历史
        """
        rounds_without_todo = 0

        call_llm_count = 0
        
        while True:
            # 预处理阶段
            self._preprocess(messages)
            call_llm_count = call_llm_count + 1
            print(f"第{call_llm_count}调用大模型的上下文:{messages}")
            
            # LLM 调用
            response = self._call_llm(messages)

            print(f"第{call_llm_count}调用大模型的返回:{response["content"]}")

            messages.append({"role": "assistant", "content": response["content"]})
            
            # 如果不再调用工具，退出循环
            if response["stop_reason"] != "tool_use":
                return messages
            
            # 工具执行阶段
            results, used_todo, manual_compress = self._execute_tools(response)
            
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
                
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": str(output)
                })
                
                if block["name"] == "TodoWrite":
                    used_todo = True
        
        return results, used_todo, manual_compress
