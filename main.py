#!/usr/bin/env python3
"""
主程序 - REPL 交互界面
"""
import sys
import os

# 添加 demo 目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import WORKDIR, TEAM_DIR, INBOX_DIR, TASKS_DIR, SKILLS_DIR, LOG_LEVEL, LOG_FILE
from utils.logging_setup import setup_logging
from managers.todo_manager import TodoManager
from managers.task_manager import TaskManager
from managers.background_manager import BackgroundManager
from managers.message_bus import MessageBus
from managers.skill_loader import SkillLoader
from managers.teammate_manager import TeammateManager
from managers.hook_manager import HookManager, HookContext, set_current_hook_manager
from agents.main_agent import MainAgent
from utils.compression import auto_compact
from utils.image_utils import file_to_image_block


def parse_user_input(query: str):
    """
    解析用户输入，返回 Anthropic content block 列表。

    支持的语法：
      普通文本                       → [text]
      /img <p1>[, <p2> ...] | <text> → [image..., text]
      /img <p1>[, <p2> ...]          → [image..., "请描述图片"]

    解析失败时抛 ValueError / FileNotFoundError，由调用方捕获展示给用户。
    """
    if not query.startswith("/img "):
        return [{"type": "text", "text": query}]

    body = query[len("/img "):].strip()
    if "|" in body:
        paths_part, text = body.split("|", 1)
        text = text.strip() or "请描述这张图片。"
    else:
        paths_part, text = body, "请描述这张图片。"

    blocks = []
    for p in [x.strip() for x in paths_part.split(",") if x.strip()]:
        blocks.append(file_to_image_block(p))
    if not blocks:
        raise ValueError("/img 后面必须提供至少一个图片路径")
    blocks.append({"type": "text", "text": text})
    return blocks


def initialize_directories():
    """初始化目录结构"""
    (WORKDIR / ".team" / "inbox").mkdir(parents=True, exist_ok=True)
    (WORKDIR / ".tasks").mkdir(exist_ok=True)
    (WORKDIR / "skills").mkdir(exist_ok=True)
    (WORKDIR / ".transcripts").mkdir(exist_ok=True)


def main():
    """主函数"""
    # 初始化目录（日志可能要写到 .transcripts/）
    initialize_directories()
    # 初始化日志
    setup_logging(level=LOG_LEVEL, log_file=LOG_FILE)
    
    # 初始化管理器
    todo_mgr = TodoManager()
    task_mgr = TaskManager()
    bg_mgr = BackgroundManager()
    bus = MessageBus()
    skill_loader = SkillLoader()
    team_mgr = TeammateManager(bus, task_mgr)

    # 钩子管理器：信任门 + .claude/hooks.json；未配置时所有事件零开销跳过。
    hook_mgr = HookManager()
    set_current_hook_manager(hook_mgr)  # 让 llm_client / compression 等跨模块也能 fire
    team_mgr.set_hook_manager(hook_mgr)
    if hook_mgr.is_active():
        hook_mgr.run_hooks(
            "SessionStart",
            HookContext(agent_role="lead", cwd=str(WORKDIR)),
        )

    # 初始化主 Agent
    agent = MainAgent(todo_mgr, bg_mgr, bus, task_mgr, skill_loader, team_mgr, hook_manager=hook_mgr)
    
    # 对话历史
    history = []
    
    print("完整 Agent 实现")
    print("输入 /help 查看命令")
    
    while True:
        try:
            query = input("\033[36m >> \033[0m")
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
            print("  /img <path>[, <path>] | <文本>  - 附图发送")
            print("  /help   - 显示帮助")
            print("  q/exit  - 退出")
            continue

        # 正常对话
        try:
            content_blocks = parse_user_input(query)
        except Exception as e:
            print(f"[输入解析失败] {e}")
            continue

        # 含图时打个本地反馈，避免用户怀疑卡住
        img_count = sum(1 for b in content_blocks if b.get("type") == "image")
        if img_count:
            print(f"[loaded {img_count} image(s)]")

        # UserPromptSubmit 钩子：可阻断该轮 / 可前置注入文本
        if hook_mgr.is_active():
            up_ctx = HookContext(agent_role="lead", user_prompt=query)
            up_result = hook_mgr.run_hooks("UserPromptSubmit", up_ctx)
            if up_result.blocked:
                print(f"[Hook 阻断] {up_result.block_reason or 'Blocked by hook'}")
                continue
            if up_result.messages:
                injected = "\n".join(f"[Hook] {m}" for m in up_result.messages)
                content_blocks.insert(0, {"type": "text", "text": injected})

        history.append({"role": "user", "content": content_blocks})
        agent.agent_loop(history)

        # 显示响应
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if block["type"] == "text":
                    print(block["text"])

        # Stop 钩子：lead 完成响应（observable，不阻断）
        if hook_mgr.is_active():
            outcome_text = ""
            if isinstance(response_content, list):
                outcome_text = "\n".join(
                    b["text"] for b in response_content if b.get("type") == "text"
                )
            hook_mgr.run_hooks(
                "Stop",
                HookContext(agent_role="lead", outcome=outcome_text),
            )

        print()

    # 退出循环 → SessionEnd（1.5s 硬超时，避免拖慢退出）
    if hook_mgr.is_active():
        hook_mgr.run_hooks(
            "SessionEnd",
            HookContext(agent_role="lead", cwd=str(WORKDIR)),
        )


def _cleanup_on_exit():
    """REPL 退出钩子:关 SSH 连接池(close_all 内部已清运行时 host)。

    任何异常都吞掉——退出阶段不能因为清理失败再抛错。
    """
    try:
        from utils.ssh_client import ssh_pool
        ssh_pool.close_all()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    finally:
        _cleanup_on_exit()
