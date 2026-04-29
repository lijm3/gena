#!/usr/bin/env python3
"""
主程序 - REPL 交互界面
"""
import sys
import os

# 添加 demo 目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import WORKDIR, TEAM_DIR, INBOX_DIR, TASKS_DIR, SKILLS_DIR
from managers.todo_manager import TodoManager
from managers.task_manager import TaskManager
from managers.background_manager import BackgroundManager
from managers.message_bus import MessageBus
from managers.skill_loader import SkillLoader
from managers.teammate_manager import TeammateManager
from agents.main_agent import MainAgent
from utils.compression import auto_compact


def initialize_directories():
    """初始化目录结构"""
    (WORKDIR / ".team" / "inbox").mkdir(parents=True, exist_ok=True)
    (WORKDIR / ".tasks").mkdir(exist_ok=True)
    (WORKDIR / "skills").mkdir(exist_ok=True)
    (WORKDIR / ".transcripts").mkdir(exist_ok=True)


def main():
    """主函数"""
    # 初始化目录
    initialize_directories()
    
    # 初始化管理器
    todo_mgr = TodoManager()
    task_mgr = TaskManager()
    bg_mgr = BackgroundManager()
    bus = MessageBus()
    skill_loader = SkillLoader()
    team_mgr = TeammateManager(bus, task_mgr)
    
    # 初始化主 Agent
    agent = MainAgent(todo_mgr, bg_mgr, bus, task_mgr, skill_loader, team_mgr)
    
    # 对话历史
    history = []
    
    print("s_full.py - 完整 Agent 实现")
    print("输入 /help 查看命令")
    
    while True:
        try:
            query = input("\033[36ms_full >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        
        if query.strip().lower() in ("q", "exit", ""):
            break
        
        # 特殊命令
        if query.strip() == "/tasks":
            print(task_mgr.list_all())
            continue
        
        if query.strip() == "/team":
            print(team_mgr.list_all())
            continue
        
        if query.strip() == "/inbox":
            import json
            print(json.dumps(bus.read_inbox("lead"), indent=2))
            continue

        if query.strip() == "/compact":
            if history:
                print("[manual compact via /compact]")
                history[:] = auto_compact(history)
            continue
        
        if query.strip() == "/help":
            print("命令:")
            print("  /tasks  - 列出所有任务")
            print("  /team   - 列出所有队友")
            print("  /inbox  - 查看收件箱")
            print("  /compact- 手动压缩上下文")
            print("  /help   - 显示帮助")
            print("  q/exit  - 退出")
            continue
        
        # 正常对话
        history.append({"role": "user", "content": query})
        agent.agent_loop(history)
        
        # 显示响应
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if block["type"] == "text":
                    print(block["text"])
        
        print()


if __name__ == "__main__":
    main()
