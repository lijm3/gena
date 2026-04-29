"""
子 Agent - s04: 派生独立的子 Agent
"""
from typing import List, Dict, Any, Optional

from core.llm_client import LLMClient


def run_subagent(prompt: str, agent_type: str = "Explore") -> str:
    """
    派生子 Agent 执行隔离任务
    
    Args:
        prompt: 子 Agent 的任务描述
        agent_type: Agent 类型
            - "Explore": 只读模式
            - "general-purpose": 完整模式
            
    Returns:
        子 Agent 的工作摘要
    """
    client = LLMClient()
    
    # 配置子 Agent 的工具集
    sub_tools = [
        {"name": "bash", "description": "Run command.",
         "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "read_file", "description": "Read file.",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    ]
    
    if agent_type != "Explore":
        sub_tools += [
            {"name": "write_file", "description": "Write file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Edit file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
        ]
    
    # 工具处理函数映射
    sub_handlers = {
        "bash": lambda **kw: run_bash(kw["command"]),
        "read_file": lambda **kw: run_read(kw["path"]),
        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
        "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    }
    
    # 创建独立的消息历史
    sub_msgs = [{"role": "user", "content": prompt}]
    resp = None
    
    # 运行子 Agent 循环（最多 30 轮）
    sub_call_llm = 0
    for _ in range(30):
        sub_call_llm = sub_call_llm + 1
        print(f"子Agent第{sub_call_llm}调用上下文:{sub_msgs}")
        resp = client.create_message(
            messages=sub_msgs,
            tools=sub_tools,
            max_tokens=8000
        )
        print(f"子Agent第{sub_call_llm}调用返回:{resp["content"]}")

        sub_msgs.append({"role": "assistant", "content": resp["content"]})
        
        if resp["stop_reason"] != "tool_use":
            break
        
        # 执行工具调用
        results = []
        for block in resp["content"]:
            if block["type"] == "tool_use":
                h = sub_handlers.get(block["name"], lambda **kw: "Unknown tool")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": str(h(**block["input"]))[:50000]
                })
        sub_msgs.append({"role": "user", "content": results})
    
    # 提取最终摘要
    if resp:
        return "".join(b["text"] for b in resp["content"] if b["type"] == "text") or "(no summary)"
    return "(subagent failed)"


# 导入基础工具
from tools.base_tools import run_bash, run_read, run_write, run_edit
