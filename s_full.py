#!/usr/bin/env python3
# Harness: 所有机制的完整组合 -- 模型的完整驾驶舱
"""
s_full.py - 完整参考 Agent 实现

这是一个集大成的实现，组合了 s01-s11 的所有机制。
s12（任务感知的 worktree 隔离）单独教学。
这不是教学会话 -- 这是"把所有东西整合在一起"的参考实现。

    +------------------------------------------------------------------+
    |                        完整 AGENT 架构                             |
    |                                                                   |
    |  系统提示词 (s05 技能系统, 任务优先 + 可选的 todo 提醒)            |
    |                                                                   |
    |  每次 LLM 调用前:                                                  |
    |  +--------------------+  +------------------+  +--------------+  |
    |  | 微压缩 (s06)       |  | 排空后台 (s08)   |  | 检查收件箱   |  |
    |  | 自动压缩 (s06)     |  | 通知             |  | (s09)        |  |
    |  +--------------------+  +------------------+  +--------------+  |
    |                                                                   |
    |  工具分发 (s02 模式):                                              |
    |  +--------+----------+----------+---------+-----------+          |
    |  | bash   | read     | write    | edit    | TodoWrite |          |
    |  | task   | load_sk  | compress | bg_run  | bg_check  |          |
    |  | t_crt  | t_get    | t_upd    | t_list  | spawn_tm  |          |
    |  | list_tm| send_msg | rd_inbox | bcast   | shutdown  |          |
    |  | plan   | idle     | claim    |         |           |          |
    |  +--------+----------+----------+---------+-----------+          |
    |                                                                   |
    |  子 Agent (s04):  派生 -> 工作 -> 返回摘要                         |
    |  队友 (s09):  派生 -> 工作 -> 空闲 -> 自动认领 (s11)               |
    |  关闭 (s10):  request_id 握手                                     |
    |  计划门控 (s10): 提交 -> 批准/拒绝                                 |
    +------------------------------------------------------------------+

    REPL 命令: /compact /tasks /team /inbox
    
    【九大组件映射】
    s01: 基础工具系统 (bash, read, write, edit)
    s02: 工具分发模式 (TOOL_HANDLERS 字典)
    s03: Todo 管理 (TodoManager)
    s04: 子 Agent 派生 (run_subagent)
    s05: 技能加载系统 (SkillLoader)
    s06: 上下文压缩 (microcompact, auto_compact)
    s07: 文件任务系统 (TaskManager)
    s08: 后台任务管理 (BackgroundManager)
    s09: 消息总线 (MessageBus)
    s10: 关闭协议 + 计划审批
    s11: 自动任务认领 (TeammateManager 的空闲循环)
"""

import json
import os
import re
import requests
import subprocess
import threading
import time
import uuid
from pathlib import Path
from queue import Queue

from anthropic import Anthropic
from dotenv import load_dotenv

# === 环境配置 ===
load_dotenv(override=True)

# ============ 客户端配置(走代理指向 GPT) ============
BASE_URL = os.environ.get(
    "ANTHROPIC_BASE_URL",
    "https://cngpt.net",  # 不要包含 /v1/messages
)
AUTH_TOKEN = os.environ.get(
    "ANTHROPIC_AUTH_TOKEN",
    "sk-zkjEB9fbdGOb0WpdIzKPZNUdfrZ5QwAMW4fSpltqjceUL7Do",
)
MODEL = os.environ.get("MODEL_NAME", "gpt-5.4")

# === 全局常量 ===
WORKDIR = Path.cwd()  # 工作目录

# 创建 LLM 客户端（使用 api_key 参数传递认证信息）
client = Anthropic(
    base_url=BASE_URL,
    api_key=AUTH_TOKEN  # 使用 api_key 参数而不是 auth_token
)

def call_anthropic_api(messages, system=None, tools=None, max_tokens=8000):
    """使用 requests 调用 Anthropic 兼容 API"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AUTH_TOKEN}",
        "x-api-key": AUTH_TOKEN,
        "anthropic-version": "2023-06-01"
    }
    
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens
    }
    
    if system:
        payload["system"] = system
    
    if tools:
        payload["tools"] = tools
    
    response = requests.post(
        f"{BASE_URL}/v1/messages",
        headers=headers,
        json=payload,
        timeout=60
    )
    
    response.raise_for_status()
    return response.json()

# === 目录结构 ===
TEAM_DIR = WORKDIR / ".team"          # 团队配置目录
INBOX_DIR = TEAM_DIR / "inbox"        # 消息收件箱目录
TASKS_DIR = WORKDIR / ".tasks"        # 任务存储目录
SKILLS_DIR = WORKDIR / "skills"       # 技能文档目录
TRANSCRIPT_DIR = WORKDIR / ".transcripts"  # 对话记录目录

# === 配置参数 ===
TOKEN_THRESHOLD = 100000  # 触发自动压缩的 token 阈值
POLL_INTERVAL = 5         # 空闲时轮询间隔（秒）
IDLE_TIMEOUT = 60         # 空闲超时时间（秒）

# === 消息类型白名单 ===
VALID_MSG_TYPES = {"message", "broadcast", "shutdown_request",
                   "shutdown_response", "plan_approval_response"}

# === 第一部分: 基础工具 (s01) ===
# 这些是 Agent 与环境交互的最基本工具

def safe_path(p: str) -> Path:
    """
    安全路径检查：防止路径逃逸攻击
    
    确保所有文件操作都在工作目录内进行
    
    Args:
        p: 相对路径字符串
        
    Returns:
        解析后的绝对路径
        
    Raises:
        ValueError: 如果路径试图逃逸工作目录
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径逃逸工作空间: {p}")
    return path

def run_bash(command: str) -> str:
    """
    执行 Shell 命令（带安全检查）
    
    这是 Agent 的"手"，用于执行系统命令
    
    Args:
        command: 要执行的 shell 命令
        
    Returns:
        命令输出（stdout + stderr）
        
    安全措施:
        - 阻止危险命令（rm -rf /, sudo 等）
        - 120 秒超时限制
        - 输出截断到 50000 字符
    """
    # 危险命令黑名单
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "错误: 危险命令被阻止"
    
    try:
        r = subprocess.run(
            command, 
            shell=True, 
            cwd=WORKDIR,
            capture_output=True, 
            text=True, 
            timeout=120,
            encoding='utf-8',
            errors='replace'  # 遇到无法解码的字符时用替换字符代替
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误: 超时 (120秒)"

def run_read(path: str, limit: int = None) -> str:
    """
    读取文件内容
    
    Args:
        path: 文件路径
        limit: 可选的行数限制
        
    Returns:
        文件内容（可能被截断）
        
    特性:
        - 支持行数限制（防止大文件溢出上下文）
        - 输出截断到 50000 字符
    """
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... (还有 {len(lines) - limit} 行)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"错误: {e}"

def run_write(path: str, content: str) -> str:
    """
    写入文件内容
    
    Args:
        path: 文件路径
        content: 要写入的内容
        
    Returns:
        操作结果消息
        
    特性:
        - 自动创建父目录
        - 覆盖现有文件
    """
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"已写入 {len(content)} 字节到 {path}"
    except Exception as e:
        return f"错误: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    """
    精确编辑文件（字符串替换）
    
    这是比 write 更精确的编辑方式，只替换指定的文本
    
    Args:
        path: 文件路径
        old_text: 要替换的旧文本（必须精确匹配）
        new_text: 新文本
        
    Returns:
        操作结果消息
        
    注意:
        - 只替换第一次出现的文本
        - 如果找不到 old_text 会报错
    """
    try:
        fp = safe_path(path)
        c = fp.read_text()
        if old_text not in c:
            return f"错误: 在 {path} 中找不到指定文本"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误: {e}"


# === 第二部分: Todo 管理系统 (s03) ===
# 用于管理会话内的短期任务清单

class TodoManager:
    """
    Todo 管理器 - 会话内任务清单
    
    这是"会话内 Todo"层，用于管理当前对话中的短期任务。
    与持久任务系统（TaskManager）不同，这些 todos 不跨会话保存。
    
    特性:
        - 最多 20 个 todo 项
        - 只允许一个 in_progress 状态
        - 三种状态: pending, in_progress, completed
        - 每个 todo 有 activeForm（当前正在做什么）
    """
    
    def __init__(self):
        self.items = []  # Todo 项列表

    def update(self, items: list) -> str:
        """
        更新整个 todo 列表
        
        Args:
            items: todo 项列表，每项包含:
                - content: 任务内容（必需）
                - status: 状态（必需）
                - activeForm: 当前形式（必需）
                
        Returns:
            渲染后的 todo 列表
            
        验证规则:
            - 最多 20 个 todos
            - 只能有一个 in_progress
            - 所有必需字段都要填写
            
        Raises:
            ValueError: 验证失败时
        """
        validated, ip = [], 0
        
        # 验证每个 todo 项
        for i, item in enumerate(items):
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).lower()
            af = str(item.get("activeForm", "")).strip()
            
            # 验证必需字段
            if not content: 
                raise ValueError(f"项 {i}: content 必需")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"项 {i}: 无效状态 '{status}'")
            if not af: 
                raise ValueError(f"项 {i}: activeForm 必需")
            
            # 统计 in_progress 数量
            if status == "in_progress": 
                ip += 1
            
            validated.append({
                "content": content, 
                "status": status, 
                "activeForm": af
            })
        
        # 验证约束
        if len(validated) > 20: 
            raise ValueError("最多 20 个 todos")
        if ip > 1: 
            raise ValueError("只允许一个 in_progress")
        
        self.items = validated
        return self.render()

    def render(self) -> str:
        """
        渲染 todo 列表为可读文本
        
        Returns:
            格式化的 todo 列表字符串
            
        格式:
            [x] 已完成的任务
            [>] 进行中的任务 <- 当前形式
            [ ] 待处理的任务
            
            (2/5 completed)
        """
        if not self.items: 
            return "无 todos。"
        
        lines = []
        for item in self.items:
            # 状态标记
            m = {
                "completed": "[x]", 
                "in_progress": "[>]", 
                "pending": "[ ]"
            }.get(item["status"], "[?]")
            
            # 进行中的任务显示 activeForm
            suffix = f" <- {item['activeForm']}" if item["status"] == "in_progress" else ""
            lines.append(f"{m} {item['content']}{suffix}")
        
        # 统计信息
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} 已完成)")
        return "\n".join(lines)

    def has_open_items(self) -> bool:
        """
        检查是否有未完成的 todo
        
        用于决定是否需要提醒 Agent 更新 todos
        
        Returns:
            True 如果有未完成的项
        """
        return any(item.get("status") != "completed" for item in self.items)


# === 第三部分: 子 Agent 派生系统 (s04) ===
# 用于派生独立的子 Agent 执行隔离任务

def run_subagent(prompt: str, agent_type: str = "Explore") -> str:
    """
    派生子 Agent 执行隔离任务
    
    这是上下文隔离的核心机制：子 Agent 有独立的消息历史，
    不会污染主 Agent 的上下文。适合用于大规模搜索、代码分析等。
    
    Args:
        prompt: 子 Agent 的任务描述
        agent_type: Agent 类型
            - "Explore": 只读模式（bash, read_file）
            - "general-purpose": 完整模式（bash, read, write, edit）
    
    Returns:
        子 Agent 的工作摘要（不返回完整历史）
    
    工作流程:
        1. 创建独立的工具集和消息历史
        2. 运行最多 30 轮对话
        3. 提取最终的文本摘要
        4. 只返回摘要给主 Agent
    
    使用场景:
        - 海量文件搜索（不污染主上下文）
        - 代码库分析（隔离大量文件内容）
        - 实验性操作（失败不影响主 Agent）
    """
    # 配置子 Agent 的工具集
    # 基础工具：所有类型都有 bash 和 read_file
    sub_tools = [
        {"name": "bash", "description": "Run command.",
         "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "read_file", "description": "Read file.",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    ]
    
    # 如果不是 Explore 模式，添加写入工具
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
    
    # 创建独立的消息历史（关键：上下文隔离）
    sub_msgs = [{"role": "user", "content": prompt}]
    resp = None
    
    # 运行子 Agent 循环（最多 30 轮）
    for _ in range(30):
        # 调用 LLM
        resp = client.messages.create(model=MODEL, messages=sub_msgs, tools=sub_tools, max_tokens=8000)
        sub_msgs.append({"role": "assistant", "content": resp.content})
        
        # 如果不再调用工具，说明任务完成
        if resp.stop_reason != "tool_use":
            break
        
        # 执行工具调用
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                h = sub_handlers.get(b.name, lambda **kw: "Unknown tool")
                # 执行工具并截断输出（防止过大）
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": str(h(**b.input))[:50000]})
        sub_msgs.append({"role": "user", "content": results})
    
    # 提取最终摘要（只返回文本部分）
    if resp:
        return "".join(b.text for b in resp.content if hasattr(b, "text")) or "(no summary)"
    return "(subagent failed)"


# === 第四部分: 技能加载系统 (s05) ===
# 按需加载专业知识，避免上下文爆炸

class SkillLoader:
    """
    技能加载器 - Agent 的"知识库"
    
    这是按需加载知识的核心机制：
    - 系统提示词只包含技能列表（索引）
    - Agent 需要时才加载完整内容
    - 避免一开始就塞满上下文
    
    目录结构:
        skills/
        ├── python/
        │   └── SKILL.md
        ├── api_design/
        │   └── SKILL.md
        └── ...
    
    SKILL.md 格式:
        ---
        name: python_best_practices
        description: Python 编程最佳实践
        ---
        
        # 技能内容
        详细的知识和指南...
    """
    
    def __init__(self, skills_dir: Path):
        """
        初始化技能加载器
        
        Args:
            skills_dir: 技能目录路径
        
        扫描目录中的所有 SKILL.md 文件并解析元数据
        """
        self.skills = {}  # 技能字典: {name: {meta, body}}
        
        if skills_dir.exists():
            # 递归查找所有 SKILL.md 文件
            for f in sorted(skills_dir.rglob("SKILL.md")):
                text = f.read_text()
                
                # 解析 YAML 前置元数据（如果存在）
                match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
                meta, body = {}, text
                
                if match:
                    # 解析元数据
                    for line in match.group(1).strip().splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip()] = v.strip()
                    body = match.group(2).strip()
                
                # 技能名称：优先使用元数据中的 name，否则使用目录名
                name = meta.get("name", f.parent.name)
                self.skills[name] = {"meta": meta, "body": body}

    def descriptions(self) -> str:
        """
        返回所有技能的简短描述
        
        这个列表会注入到系统提示词中，让 Agent 知道有哪些技能可用
        
        Returns:
            格式化的技能列表字符串
        """
        if not self.skills: 
            return "(no skills)"
        return "\n".join(f"  - {n}: {s['meta'].get('description', '-')}" for n, s in self.skills.items())

    def load(self, name: str) -> str:
        """
        加载指定技能的完整内容
        
        只有当 Agent 明确需要时才调用，避免一开始就塞满上下文
        
        Args:
            name: 技能名称
        
        Returns:
            包装在 XML 标签中的技能内容，或错误消息
        """
        s = self.skills.get(name)
        if not s: 
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        
        # 用 XML 标签包装，便于 LLM 识别
        return f"<skill name=\"{name}\">\n{s['body']}\n</skill>"


# === 第五部分: 上下文压缩系统 (s06) ===
# 两级压缩机制：微压缩（快速）+ 自动压缩（彻底）

def estimate_tokens(messages: list) -> int:
    """
    估算消息列表的 token 数
    
    使用简单的启发式方法：JSON 字符串长度 / 4
    这个估算足够用于触发压缩的阈值判断
    
    Args:
        messages: 消息历史列表
    
    Returns:
        估算的 token 数
    """
    return len(json.dumps(messages, default=str)) // 4

def microcompact(messages: list):
    """
    微压缩：清理旧的工具结果
    
    这是第一级压缩，每轮都执行，快速减少 token 使用。
    
    策略:
        - 保留最近 3 个工具结果的完整内容
        - 清理更早的工具结果内容（替换为 "[cleared]"）
        - 保留工具调用本身（保持对话连贯性）
    
    效果:
        - 减少 token 使用（工具结果通常很长）
        - 不影响对话理解（保留了调用记录）
        - 不需要 LLM 调用（快速）
    
    Args:
        messages: 消息历史列表（原地修改）
    """
    # 收集所有工具结果的引用
    indices = []
    for i, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part in msg["content"]:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    indices.append(part)
    
    # 如果工具结果少于等于 3 个，不需要清理
    if len(indices) <= 3:
        return
    
    # 清理除了最后 3 个之外的所有工具结果
    for part in indices[:-3]:
        if isinstance(part.get("content"), str) and len(part["content"]) > 100:
            part["content"] = "[cleared]"

def auto_compact(messages: list) -> list:
    """
    自动压缩：生成历史摘要
    
    这是第二级压缩，当 token 超过阈值时触发，彻底压缩历史。
    
    触发条件:
        - token 数超过 TOKEN_THRESHOLD (100000)
    
    工作流程:
        1. 保存完整记录到 .transcripts/ 目录
        2. 提取最近 80000 字符的对话内容
        3. 调用 LLM 生成连续性摘要
        4. 用摘要替换原始历史
    
    返回:
        只包含摘要的新消息列表
    
    Args:
        messages: 原始消息历史
    
    Returns:
        压缩后的消息列表（只包含摘要）
    """
    # 确保 transcript 目录存在
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    
    # 保存完整历史到文件（重要：防止信息丢失）
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    
    # 提取最近的对话内容（最多 80000 字符）
    conv_text = json.dumps(messages, default=str)[-80000:]
    
    # 调用 LLM 生成摘要
    resp = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content": f"Summarize for continuity:\n{conv_text}"}],
        max_tokens=2000,
    )
    summary = resp.content[0].text
    
    # 返回新的消息列表（只包含摘要）
    return [
        {"role": "user", "content": f"[Compressed. Transcript: {path}]\n{summary}"},
    ]


# === 第六部分: 文件任务系统 (s07) ===
# 持久化任务管理，支持依赖关系和跨会话保存

class TaskManager:
    """
    持久任务管理器 - Agent 的"项目管理系统"
    
    这是"持久任务图"层，与 TodoManager 的区别：
    - TodoManager: 会话内短期任务，不保存到文件
    - TaskManager: 持久化任务，跨会话保存，支持依赖关系
    
    目录结构:
        .tasks/
        ├── task_1.json
        ├── task_2.json
        └── ...
    
    任务文件格式:
        {
            "id": 1,
            "subject": "实现登录功能",
            "description": "详细描述...",
            "status": "pending",  # pending, in_progress, completed, deleted
            "owner": "agent-backend",  # 认领者
            "blockedBy": [2, 3]  # 依赖的任务 ID 列表
        }
    
    特性:
        - 自动递增 ID
        - 支持任务依赖（blockedBy）
        - 任务完成时自动解锁下游任务
        - 支持多 Agent 认领
    """
    
    def __init__(self):
        """初始化任务管理器，确保任务目录存在"""
        TASKS_DIR.mkdir(exist_ok=True)

    def _next_id(self) -> int:
        """
        生成下一个任务 ID
        
        扫描现有任务文件，返回最大 ID + 1
        
        Returns:
            新任务的 ID
        """
        ids = [int(f.stem.split("_")[1]) for f in TASKS_DIR.glob("task_*.json")]
        return max(ids, default=0) + 1

    def _load(self, tid: int) -> dict:
        """
        加载任务数据
        
        Args:
            tid: 任务 ID
        
        Returns:
            任务数据字典
        
        Raises:
            ValueError: 任务不存在
        """
        p = TASKS_DIR / f"task_{tid}.json"
        if not p.exists(): 
            raise ValueError(f"Task {tid} not found")
        return json.loads(p.read_text())

    def _save(self, task: dict):
        """
        保存任务数据到文件
        
        Args:
            task: 任务数据字典
        """
        (TASKS_DIR / f"task_{task['id']}.json").write_text(json.dumps(task, indent=2))

    def create(self, subject: str, description: str = "") -> str:
        """
        创建新任务
        
        Args:
            subject: 任务主题（必需）
            description: 任务详细描述（可选）
        
        Returns:
            创建的任务 JSON 字符串
        """
        task = {
            "id": self._next_id(), 
            "subject": subject, 
            "description": description,
            "status": "pending", 
            "owner": None, 
            "blockedBy": []
        }
        self._save(task)
        return json.dumps(task, indent=2)

    def get(self, tid: int) -> str:
        """
        获取任务详情
        
        Args:
            tid: 任务 ID
        
        Returns:
            任务 JSON 字符串
        """
        return json.dumps(self._load(tid), indent=2)

    def update(self, tid: int, status: str = None,
               add_blocked_by: list = None, remove_blocked_by: list = None) -> str:
        """
        更新任务
        
        Args:
            tid: 任务 ID
            status: 新状态（可选）
            add_blocked_by: 要添加的依赖任务 ID 列表（可选）
            remove_blocked_by: 要移除的依赖任务 ID 列表（可选）
        
        Returns:
            更新后的任务 JSON 字符串
        
        特殊逻辑:
            - status="completed": 自动解锁所有依赖此任务的下游任务
            - status="deleted": 删除任务文件
        """
        task = self._load(tid)
        
        # 更新状态
        if status:
            task["status"] = status
            
            # 任务完成：解锁下游任务
            if status == "completed":
                for f in TASKS_DIR.glob("task_*.json"):
                    t = json.loads(f.read_text())
                    if tid in t.get("blockedBy", []):
                        t["blockedBy"].remove(tid)
                        self._save(t)
            
            # 删除任务
            if status == "deleted":
                (TASKS_DIR / f"task_{tid}.json").unlink(missing_ok=True)
                return f"Task {tid} deleted"
        
        # 添加依赖
        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        
        # 移除依赖
        if remove_blocked_by:
            task["blockedBy"] = [x for x in task["blockedBy"] if x not in remove_blocked_by]
        
        self._save(task)
        return json.dumps(task, indent=2)

    def list_all(self) -> str:
        """
        列出所有任务
        
        Returns:
            格式化的任务列表字符串
        
        格式:
            [ ] #1: 设计数据库 schema
            [>] #2: 实现 API @agent-backend
            [x] #3: 写测试
            [ ] #4: 部署 (blocked by: [2])
        """
        tasks = [json.loads(f.read_text()) for f in sorted(TASKS_DIR.glob("task_*.json"))]
        if not tasks: 
            return "No tasks."
        
        lines = []
        for t in tasks:
            # 状态标记
            m = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            
            # 所有者标记
            owner = f" @{t['owner']}" if t.get("owner") else ""
            
            # 依赖标记
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            
            lines.append(f"{m} #{t['id']}: {t['subject']}{owner}{blocked}")
        
        return "\n".join(lines)

    def claim(self, tid: int, owner: str) -> str:
        """
        认领任务
        
        将任务分配给指定的 Agent 并设置状态为 in_progress
        
        Args:
            tid: 任务 ID
            owner: 认领者名称（Agent 名称）
        
        Returns:
            操作结果消息
        """
        task = self._load(tid)
        task["owner"] = owner
        task["status"] = "in_progress"
        self._save(task)
        return f"Claimed task #{tid} for {owner}"


# === 第七部分: 后台任务管理系统 (s08) ===
# 异步执行长时间命令，不阻塞主 Agent 循环

class BackgroundManager:
    """
    后台任务管理器 - 异步命令执行
    
    用于运行长时间命令而不阻塞 Agent 的主循环。
    命令在独立线程中执行，完成时发送通知。
    
    使用场景:
        - 长时间编译（make, cargo build）
        - 运行测试套件（pytest, npm test）
        - 下载大文件（wget, curl）
        - 数据处理任务
    
    工作流程:
        1. Agent 调用 background_run 启动任务
        2. 任务在后台线程中执行
        3. Agent 继续处理其他工作
        4. 任务完成时，通知进入队列
        5. 下次 LLM 调用前，通知自动注入到上下文
    """
    
    def __init__(self):
        """初始化后台任务管理器"""
        self.tasks = {}  # 任务字典: {task_id: {status, command, result}}
        self.notifications = Queue()  # 通知队列（线程安全）

    def run(self, command: str, timeout: int = 120) -> str:
        """
        在后台线程中运行命令
        
        Args:
            command: 要执行的 shell 命令
            timeout: 超时时间（秒），默认 120 秒
        
        Returns:
            任务 ID（用于后续查询状态）
        """
        # 生成唯一的任务 ID
        tid = str(uuid.uuid4())[:8]
        
        # 初始化任务状态
        self.tasks[tid] = {"status": "running", "command": command, "result": None}
        
        # 启动后台线程执行命令
        threading.Thread(target=self._exec, args=(tid, command, timeout), daemon=True).start()
        
        return f"Background task {tid} started: {command[:80]}"

    def _exec(self, tid: str, command: str, timeout: int):
        """
        后台线程执行函数（内部使用）
        
        Args:
            tid: 任务 ID
            command: 要执行的命令
            timeout: 超时时间
        """
        try:
            # 执行命令
            r = subprocess.run(
                command, 
                shell=True, 
                cwd=WORKDIR,
                capture_output=True, 
                text=True, 
                timeout=timeout,
                encoding='utf-8',
                errors='replace'  # 遇到无法解码的字符时用替换字符代替
            )
            
            # 收集输出（stdout + stderr）
            output = (r.stdout + r.stderr).strip()[:50000]
            
            # 更新任务状态
            self.tasks[tid].update({
                "status": "completed", 
                "result": output or "(no output)"
            })
        except Exception as e:
            # 处理错误（超时、命令失败等）
            self.tasks[tid].update({
                "status": "error", 
                "result": str(e)
            })
        
        # 发送通知到队列
        self.notifications.put({
            "task_id": tid, 
            "status": self.tasks[tid]["status"],
            "result": self.tasks[tid]["result"][:500]  # 通知中只包含前 500 字符
        })

    def check(self, tid: str = None) -> str:
        """
        检查后台任务状态
        
        Args:
            tid: 任务 ID（可选）
                - 如果提供：返回指定任务的状态
                - 如果为 None：返回所有任务的列表
        
        Returns:
            任务状态字符串
        """
        if tid:
            # 查询单个任务
            t = self.tasks.get(tid)
            return f"[{t['status']}] {t.get('result') or '(running)'}" if t else f"Unknown: {tid}"
        
        # 列出所有任务
        return "\n".join(
            f"{k}: [{v['status']}] {v['command'][:60]}" 
            for k, v in self.tasks.items()
        ) or "No bg tasks."

    def drain(self) -> list:
        """
        排空通知队列
        
        返回所有待处理的通知，这些通知会在下次 LLM 调用前注入到上下文
        
        Returns:
            通知列表
        """
        notifs = []
        while not self.notifications.empty():
            notifs.append(self.notifications.get_nowait())
        return notifs


# === 第八部分: 消息总线系统 (s09) ===
# Agent 间通信的核心机制，使用文件作为消息队列

class MessageBus:
    """
    消息总线 - Agent 间通信
    
    使用文件系统作为消息队列，实现 Agent 之间的异步通信。
    每个 Agent 有独立的收件箱（.jsonl 文件）。
    
    目录结构:
        .team/inbox/
        ├── lead.jsonl      # 主 Agent 的收件箱
        ├── backend.jsonl   # 后端 Agent 的收件箱
        └── ...
    
    消息格式:
        {
            "type": "message",  # 消息类型
            "from": "lead",     # 发送者
            "content": "...",   # 消息内容
            "timestamp": 1234567890.0
        }
    
    支持的消息类型:
        - message: 普通消息
        - broadcast: 广播消息
        - shutdown_request: 关闭请求
        - shutdown_response: 关闭响应
        - plan_approval_response: 计划审批响应
    
    特性:
        - 异步通信（发送者不等待）
        - 持久化（消息保存在文件中）
        - 读后清空（避免重复处理）
    """
    
    def __init__(self):
        """初始化消息总线，确保收件箱目录存在"""
        INBOX_DIR.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        """
        发送消息
        
        消息追加到接收者的 .jsonl 文件中
        
        Args:
            sender: 发送者名称
            to: 接收者名称
            content: 消息内容
            msg_type: 消息类型（默认 "message"）
            extra: 额外的字段（可选）
        
        Returns:
            操作结果消息
        """
        # 构建消息对象
        msg = {
            "type": msg_type, 
            "from": sender, 
            "content": content,
            "timestamp": time.time()
        }
        
        # 添加额外字段（如 request_id）
        if extra: 
            msg.update(extra)
        
        # 追加到接收者的收件箱文件
        with open(INBOX_DIR / f"{to}.jsonl", "a") as f:
            f.write(json.dumps(msg) + "\n")
        
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        """
        读取并清空收件箱
        
        这是"读后清空"模式，避免重复处理消息
        
        Args:
            name: Agent 名称
        
        Returns:
            消息列表
        """
        path = INBOX_DIR / f"{name}.jsonl"
        
        # 如果收件箱不存在，返回空列表
        if not path.exists(): 
            return []
        
        # 读取所有消息
        msgs = [json.loads(l) for l in path.read_text().strip().splitlines() if l]
        
        # 清空收件箱
        path.write_text("")
        
        return msgs

    def broadcast(self, sender: str, content: str, names: list) -> str:
        """
        广播消息给多个 Agent
        
        Args:
            sender: 发送者名称
            content: 消息内容
            names: 接收者名称列表
        
        Returns:
            操作结果消息
        """
        count = 0
        for n in names:
            # 不发送给自己
            if n != sender:
                self.send(sender, n, content, "broadcast")
                count += 1
        
        return f"Broadcast to {count} teammates"


# === 第九部分: 关闭协议 + 计划审批 (s10) ===
# 优雅关闭和计划审批的握手协议

# 全局跟踪字典
shutdown_requests = {}  # 跟踪关闭请求: {request_id: {target, status}}
plan_requests = {}      # 跟踪计划请求: {request_id: {from, status}}


def handle_shutdown_request(teammate: str) -> str:
    """
    请求队友 Agent 关闭
    
    这是一个握手协议，确保优雅关闭：
    1. Lead 生成 request_id
    2. 发送 shutdown_request 消息给队友
    3. 队友收到后执行关闭流程
    4. （可选）队友发送 shutdown_response 确认
    
    Args:
        teammate: 要关闭的队友名称
    
    Returns:
        操作结果消息
    
    使用场景:
        - 任务完成，不再需要队友
        - 队友出现错误，需要重启
        - 系统关闭前清理资源
    """
    # 生成唯一的请求 ID
    req_id = str(uuid.uuid4())[:8]
    
    # 记录请求状态
    shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    
    # 发送关闭请求消息
    BUS.send("lead", teammate, "Please shut down.", "shutdown_request", {"request_id": req_id})
    
    return f"Shutdown request {req_id} sent to '{teammate}'"


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    """
    审批队友的计划
    
    这是计划门控机制，用于重要操作前的人工审批：
    1. 队友提交计划（通过消息）
    2. Lead 审批或拒绝
    3. 发送 plan_approval_response 给队友
    4. 队友根据审批结果继续或调整
    
    Args:
        request_id: 计划请求 ID
        approve: 是否批准（True/False）
        feedback: 反馈意见（可选）
    
    Returns:
        操作结果消息
    
    使用场景:
        - 删除重要文件前审批
        - 部署到生产环境前审批
        - 大规模重构前审批
    """
    # 查找计划请求
    req = plan_requests.get(request_id)
    if not req: 
        return f"Error: Unknown plan request_id '{request_id}'"
    
    # 更新请求状态
    req["status"] = "approved" if approve else "rejected"
    
    # 发送审批响应消息
    BUS.send(
        "lead", 
        req["from"], 
        feedback, 
        "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback}
    )
    
    return f"Plan {req['status']} for '{req['from']}'"
# === 第十部分: 队友管理系统 (s09/s11) ===
# 持久化 Agent 团队，支持自动任务认领

class TeammateManager:
    """
    队友管理器 - 持久化 Agent 团队
    
    管理一个持久化的 Agent 团队，每个队友在独立线程中运行。
    这是多 Agent 协作的核心机制。
    
    配置文件:
        .team/config.json
        {
            "team_name": "default",
            "members": [
                {
                    "name": "backend",
                    "role": "后端开发",
                    "status": "working"  # working, idle, shutdown
                }
            ]
        }
    
    队友生命周期:
        1. spawn: 创建队友，启动后台线程
        2. working: 处理任务和消息
        3. idle: 空闲，轮询新任务和消息
        4. shutdown: 关闭
    
    特性:
        - 持久化配置
        - 独立的消息历史
        - 自动任务认领（s11）
        - 优雅关闭
    """
    
    def __init__(self, bus: MessageBus, task_mgr: TaskManager):
        """
        初始化队友管理器
        
        Args:
            bus: 消息总线实例
            task_mgr: 任务管理器实例
        """
        TEAM_DIR.mkdir(exist_ok=True)
        self.bus = bus
        self.task_mgr = task_mgr
        self.config_path = TEAM_DIR / "config.json"
        self.config = self._load()
        self.threads = {}  # 线程字典: {name: thread}

    def _load(self) -> dict:
        """
        加载团队配置
        
        Returns:
            配置字典
        """
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save(self):
        """保存团队配置到文件"""
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find(self, name: str) -> dict:
        """
        查找队友
        
        Args:
            name: 队友名称
        
        Returns:
            队友配置字典，如果不存在返回 None
        """
        for m in self.config["members"]:
            if m["name"] == name: 
                return m
        return None

    def spawn(self, name: str, role: str, prompt: str) -> str:
        """
        派生队友 Agent
        
        创建一个持久化的 Agent，在后台线程中运行
        
        Args:
            name: 队友名称（唯一标识）
            role: 队友角色（如 "后端开发"）
            prompt: 初始任务描述
        
        Returns:
            操作结果消息
        """
        member = self._find(name)
        
        if member:
            # 队友已存在，检查状态
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            
            # 重新激活队友
            member["status"] = "working"
            member["role"] = role
        else:
            # 创建新队友
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        
        self._save()
        
        # 启动后台线程
        threading.Thread(target=self._loop, args=(name, role, prompt), daemon=True).start()
        
        return f"Spawned '{name}' (role: {role})"

    def _set_status(self, name: str, status: str):
        """
        更新队友状态
        
        Args:
            name: 队友名称
            status: 新状态
        """
        member = self._find(name)
        if member:
            member["status"] = status
            self._save()

    def _loop(self, name: str, role: str, prompt: str):
        """
        队友的主循环（在后台线程中运行）
        
        这是队友 Agent 的核心逻辑，包含两个阶段：
        1. 工作阶段：处理任务和消息
        2. 空闲阶段：轮询新任务和消息（s11 自动认领）
        
        Args:
            name: 队友名称
            role: 队友角色
            prompt: 初始任务
        """
        team_name = self.config["team_name"]
        
        # 系统提示词：定义队友的身份和能力
        sys_prompt = (
            f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
            f"Use idle when done with current work. You may auto-claim tasks."
        )
        
        # 初始化消息历史
        messages = [{"role": "user", "content": prompt}]
        
        # 定义队友的工具集
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
        ]
        
        # 主循环：工作 -> 空闲 -> 工作 -> ...
        while True:
            # ========== 工作阶段 ==========
            for _ in range(50):  # 最多 50 轮对话
                # 检查收件箱
                inbox = self.bus.read_inbox(name)
                for msg in inbox:
                    # 处理关闭请求
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return
                    
                    # 将消息注入到上下文
                    messages.append({"role": "user", "content": json.dumps(msg)})
                
                # 调用 LLM
                try:
                    response = client.messages.create(
                        model=MODEL, 
                        system=sys_prompt, 
                        messages=messages,
                        tools=tools, 
                        max_tokens=8000
                    )
                except Exception:
                    # LLM 调用失败，关闭队友
                    self._set_status(name, "shutdown")
                    return
                
                messages.append({"role": "assistant", "content": response.content})
                
                # 如果不再调用工具，继续下一轮
                if response.stop_reason != "tool_use":
                    break
                
                # 执行工具调用
                results = []
                idle_requested = False
                
                for block in response.content:
                    if block.type == "tool_use":
                        # 处理 idle 工具
                        if block.name == "idle":
                            idle_requested = True
                            output = "Entering idle phase."
                        
                        # 处理 claim_task 工具
                        elif block.name == "claim_task":
                            output = self.task_mgr.claim(block.input["task_id"], name)
                        
                        # 处理 send_message 工具
                        elif block.name == "send_message":
                            output = self.bus.send(name, block.input["to"], block.input["content"])
                        
                        # 处理其他工具
                        else:
                            dispatch = {
                                "bash": lambda **kw: run_bash(kw["command"]),
                                "read_file": lambda **kw: run_read(kw["path"]),
                                "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
                                "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"])
                            }
                            output = dispatch.get(block.name, lambda **kw: "Unknown")(**block.input)
                        
                        # 打印工具执行日志
                        print(f"  [{name}] {block.name}: {str(output)[:120]}")
                        
                        # 收集工具结果
                        results.append({
                            "type": "tool_result", 
                            "tool_use_id": block.id, 
                            "content": str(output)
                        })
                
                messages.append({"role": "user", "content": results})
                
                # 如果请求进入空闲，退出工作阶段
                if idle_requested:
                    break
            
            # ========== 空闲阶段（s11 自动认领）==========
            self._set_status(name, "idle")
            resume = False
            
            # 轮询新任务和消息（最多 IDLE_TIMEOUT 秒）
            for _ in range(IDLE_TIMEOUT // max(POLL_INTERVAL, 1)):
                time.sleep(POLL_INTERVAL)
                
                # 检查收件箱
                inbox = self.bus.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        # 处理关闭请求
                        if msg.get("type") == "shutdown_request":
                            self._set_status(name, "shutdown")
                            return
                        
                        # 将消息注入到上下文
                        messages.append({"role": "user", "content": json.dumps(msg)})
                    
                    resume = True
                    break
                
                # 检查未认领的任务
                unclaimed = []
                for f in sorted(TASKS_DIR.glob("task_*.json")):
                    t = json.loads(f.read_text())
                    # 查找 pending 状态、无所有者、无依赖的任务
                    if t.get("status") == "pending" and not t.get("owner") and not t.get("blockedBy"):
                        unclaimed.append(t)
                
                if unclaimed:
                    # 自动认领第一个未认领的任务
                    task = unclaimed[0]
                    self.task_mgr.claim(task["id"], name)
                    
                    # 身份重注入（用于压缩后的上下文）
                    if len(messages) <= 3:
                        messages.insert(0, {"role": "user", "content":
                            f"<identity>You are '{name}', role: {role}, team: {team_name}.</identity>"})
                        messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})
                    
                    # 注入任务信息
                    messages.append({"role": "user", "content":
                        f"<auto-claimed>Task #{task['id']}: {task['subject']}\n{task.get('description', '')}</auto-claimed>"})
                    messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                    
                    resume = True
                    break
            
            # 如果没有新任务或消息，超时关闭
            if not resume:
                self._set_status(name, "shutdown")
                return
            
            # 恢复工作状态
            self._set_status(name, "working")

    def list_all(self) -> str:
        """
        列出所有队友
        
        Returns:
            格式化的队友列表字符串
        """
        if not self.config["members"]: 
            return "No teammates."
        
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        
        return "\n".join(lines)

    def member_names(self) -> list:
        """
        获取所有队友名称
        
        Returns:
            队友名称列表
        """
        return [m["name"] for m in self.config["members"]]


# === 第十一部分: 全局实例初始化 ===
# 创建所有管理器的单例实例

TODO = TodoManager()                      # Todo 管理器（会话内任务）
SKILLS = SkillLoader(SKILLS_DIR)          # 技能加载器（按需知识）
TASK_MGR = TaskManager()                  # 任务管理器（持久化任务）
BG = BackgroundManager()                  # 后台任务管理器（异步执行）
BUS = MessageBus()                        # 消息总线（Agent 间通信）
TEAM = TeammateManager(BUS, TASK_MGR)     # 队友管理器（多 Agent 协作）


# === 第十二部分: 系统提示词 ===
# 定义主 Agent 的身份、能力和工作方式

SYSTEM = f"""You are a coding agent at {WORKDIR}. Use tools to solve tasks.
Prefer task_create/task_update/task_list for multi-step work. Use TodoWrite for short checklists.
Use task for subagent delegation. Use load_skill for specialized knowledge.
Skills: {SKILLS.descriptions()}"""


# === SECTION: shutdown_protocol (s10) ===
def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]
    shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send("lead", teammate, "Please shut down.", "shutdown_request", {"request_id": req_id})
    return f"Shutdown request {req_id} sent to '{teammate}'"

# === SECTION: plan_approval (s10) ===
def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    req = plan_requests.get(request_id)
    if not req: return f"Error: Unknown plan request_id '{request_id}'"
    req["status"] = "approved" if approve else "rejected"
    BUS.send("lead", req["from"], feedback, "plan_approval_response",
             {"request_id": request_id, "approve": approve, "feedback": feedback})
    return f"Plan {req['status']} for '{req['from']}'"


# === 第十三部分: 工具分发表 (s02) ===
# 统一的工具处理接口，将工具名映射到处理函数

TOOL_HANDLERS = {
    # 基础工具 (s01)
    "bash":             lambda **kw: run_bash(kw["command"]),
    "read_file":        lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":       lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":        lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    
    # Todo 管理 (s03)
    "TodoWrite":        lambda **kw: TODO.update(kw["items"]),
    
    # 子 Agent (s04)
    "task":             lambda **kw: run_subagent(kw["prompt"], kw.get("agent_type", "Explore")),
    
    # 技能加载 (s05)
    "load_skill":       lambda **kw: SKILLS.load(kw["name"]),
    
    # 压缩 (s06)
    "compress":         lambda **kw: "Compressing...",
    
    # 后台任务 (s08)
    "background_run":   lambda **kw: BG.run(kw["command"], kw.get("timeout", 120)),
    "check_background": lambda **kw: BG.check(kw.get("task_id")),
    
    # 文件任务 (s07)
    "task_create":      lambda **kw: TASK_MGR.create(kw["subject"], kw.get("description", "")),
    "task_get":         lambda **kw: TASK_MGR.get(kw["task_id"]),
    "task_update":      lambda **kw: TASK_MGR.update(kw["task_id"], kw.get("status"), kw.get("add_blocked_by"), kw.get("remove_blocked_by")),
    "task_list":        lambda **kw: TASK_MGR.list_all(),
    
    # 队友管理 (s09)
    "spawn_teammate":   lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":   lambda **kw: TEAM.list_all(),
    
    # 消息通信 (s09)
    "send_message":     lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":       lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    "broadcast":        lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    
    # 关闭和审批 (s10)
    "shutdown_request": lambda **kw: handle_shutdown_request(kw["teammate"]),
    "plan_approval":    lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    
    # 其他
    "idle":             lambda **kw: "Lead does not idle.",
    "claim_task":       lambda **kw: TASK_MGR.claim(kw["task_id"], "lead"),
}

# === 第十四部分: 工具定义列表 ===
# 提供给 LLM 的工具描述（JSON Schema 格式）

TOOLS = [
    # 基础工具
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    
    # Todo 管理
    {"name": "TodoWrite", "description": "Update task tracking list.",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "activeForm": {"type": "string"}}, "required": ["content", "status", "activeForm"]}}}, "required": ["items"]}},
    
    # 子 Agent
    {"name": "task", "description": "Spawn a subagent for isolated exploration or work.",
     "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "agent_type": {"type": "string", "enum": ["Explore", "general-purpose"]}}, "required": ["prompt"]}},
    
    # 技能加载
    {"name": "load_skill", "description": "Load specialized knowledge by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    
    # 压缩
    {"name": "compress", "description": "Manually compress conversation context.",
     "input_schema": {"type": "object", "properties": {}}},
    
    # 后台任务
    {"name": "background_run", "description": "Run command in background thread.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}},
    
    {"name": "check_background", "description": "Check background task status.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},
    
    # 文件任务系统
    {"name": "task_create", "description": "Create a persistent file task.",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}},
    
    {"name": "task_get", "description": "Get task details by ID.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
    
    {"name": "task_update", "description": "Update task status or dependencies.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]}, "add_blocked_by": {"type": "array", "items": {"type": "integer"}}, "remove_blocked_by": {"type": "array", "items": {"type": "integer"}}}, "required": ["task_id"]}},
    
    {"name": "task_list", "description": "List all tasks.",
     "input_schema": {"type": "object", "properties": {}}},
    
    # 队友管理
    {"name": "spawn_teammate", "description": "Spawn a persistent autonomous teammate.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    
    {"name": "list_teammates", "description": "List all teammates.",
     "input_schema": {"type": "object", "properties": {}}},
    
    # 消息通信
    {"name": "send_message", "description": "Send a message to a teammate.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
     "input_schema": {"type": "object", "properties": {}}},
    
    {"name": "broadcast", "description": "Send message to all teammates.",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    
    # 关闭和审批
    {"name": "shutdown_request", "description": "Request a teammate to shut down.",
     "input_schema": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
    
    {"name": "plan_approval", "description": "Approve or reject a teammate's plan.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}},
    
    # 其他
    {"name": "idle", "description": "Enter idle state.",
     "input_schema": {"type": "object", "properties": {}}},
    
    {"name": "claim_task", "description": "Claim a task from the board.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
]


# === 第十五部分: Agent 主循环 ===
# 这是主 Agent 的核心逻辑

def agent_loop(messages: list):
    """
    主 Agent 循环
    
    这是整个系统的核心，协调所有机制：
    - 压缩（s06）
    - 后台通知（s08）
    - 收件箱（s09）
    - LLM 调用
    - 工具执行（s02）
    - Todo 提醒（s03）
    
    Args:
        messages: 消息历史列表（会被原地修改）
    
    循环直到 LLM 不再调用工具为止
    """
    rounds_without_todo = 0  # 跟踪未更新 todo 的轮数
    call_llm_counts = 0 #调用大模型的此时
    while True:
        # ========== 预处理阶段 ==========
        
        # s06: 压缩管道
        # 第一级：微压缩（清理旧工具结果）
        microcompact(messages)
        
        # 第二级：自动压缩（超过阈值时触发）
        if estimate_tokens(messages) > TOKEN_THRESHOLD:
            print("[auto-compact triggered]")
            messages[:] = auto_compact(messages)
        
        # s08: 排空后台通知
        notifs = BG.drain()
        if notifs:
            # 将通知注入到上下文
            txt = "\n".join(f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs)
            messages.append({"role": "user", "content": f"<background-results>\n{txt}\n</background-results>"})
        
        # s10: 检查主 Agent 的收件箱
        inbox = BUS.read_inbox("lead")
        if inbox:
            # 将消息注入到上下文
            messages.append({"role": "user", "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>"})
        
        # ========== LLM 调用 ==========
        try:
            #response = client.messages.create(
            #    model=MODEL,
            #    system=SYSTEM,
            #    messages=messages,
            #    tools=TOOLS,
            #    max_tokens=8000,
            #)
            call_llm_counts = call_llm_counts +1
            print(f"第{call_llm_counts}调用大模型的上下:{messages}")
            response = call_anthropic_api(
                messages=messages,
                system=SYSTEM,
                tools=TOOLS,
                max_tokens=8000
            )
            print(f"第{call_llm_counts}调用大模型的返回:{response}")

            messages.append({"role": "assistant", "content": response["content"]})
        except Exception as e:
            print(f"\n❌ LLM 调用失败:")
            print(f"   错误类型: {type(e).__name__}")
            print(f"   错误信息: {str(e)}")
            print(f"\n配置信息:")
            print(f"   BASE_URL: {BASE_URL}")
            print(f"   MODEL: {MODEL}")
            print(f"   AUTH_TOKEN: {AUTH_TOKEN[:20]}...")
            print(f"\n请检查:")
            print(f"   1. API Token 是否有效")
            print(f"   2. 账户是否有余额/配额")
            print(f"   3. 代理服务器是否正常")
            print(f"   4. 模型名称是否正确")
            raise
        
        # 如果不再调用工具，退出循环
        if response["stop_reason"] != "tool_use":
            return
        
        # ========== 工具执行阶段 ==========
        results = []
        used_todo = False
        manual_compress = False
        
        for block in response["content"]:
            if block["type"] == "tool_use":
                # 检测手动压缩请求
                if block["name"] == "compress":
                    manual_compress = True
                
                # 获取工具处理函数
                handler = TOOL_HANDLERS.get(block["name"])
                
                # 执行工具
                try:
                    output = handler(**block["input"]) if handler else f"Unknown tool: {block['name']}"
                except Exception as e:
                    output = f"Error: {e}"
                
                # 打印工具执行日志
                print(f"> {block['name']}:")
                print(str(output)[:200])
                
                # 收集工具结果
                results.append({
                    "type": "tool_result", 
                    "tool_use_id": block["id"], 
                    "content": str(output)
                })
                
                # 检测 TodoWrite 使用
                if block["name"] == "TodoWrite":
                    used_todo = True
        
        # ========== s03: Todo 提醒机制 ==========
        # 只有当 todo 工作流激活时才提醒
        rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
        
        if TODO.has_open_items() and rounds_without_todo >= 3:
            # 3 轮未更新 todo，添加提醒
            results.append({
                "type": "text", 
                "text": "<reminder>Update your todos.</reminder>"
            })
        
        # 将工具结果注入到上下文
        messages.append({"role": "user", "content": results})
        
        # ========== s06: 手动压缩 ==========
        if manual_compress:
            print("[manual compact]")
            messages[:] = auto_compact(messages)
            return


# === 第十六部分: REPL 交互界面 ===
# 命令行交互界面，用户与 Agent 对话的入口

if __name__ == "__main__":
    """
    REPL (Read-Eval-Print Loop) 主程序
    
    提供交互式命令行界面，支持：
    - 自然语言对话
    - 特殊命令（/compact, /tasks, /team, /inbox）
    - 优雅退出（q, exit, Ctrl+C）
    
    工作流程:
        1. 读取用户输入
        2. 处理特殊命令或调用 agent_loop
        3. 显示 Agent 响应
        4. 重复
    """
    history = []  # 对话历史
    
    while True:
        try:
            # 读取用户输入（带颜色提示符）
            query = input("\033[36ms_full >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            # Ctrl+C 或 Ctrl+D 退出
            break
        
        # 处理退出命令
        if query.strip().lower() in ("q", "exit", ""):
            break
        
        # ========== 特殊命令处理 ==========
        
        # /compact: 手动触发压缩
        if query.strip() == "/compact":
            if history:
                print("[manual compact via /compact]")
                history[:] = auto_compact(history)
            continue
        
        # /tasks: 列出所有任务
        if query.strip() == "/tasks":
            print(TASK_MGR.list_all())
            continue
        
        # /team: 列出所有队友
        if query.strip() == "/team":
            print(TEAM.list_all())
            continue
        
        # /inbox: 查看收件箱
        if query.strip() == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            continue
        
        # ========== 正常对话处理 ==========
        
        # 将用户输入添加到历史
        history.append({"role": "user", "content": query})
        
        # 调用 Agent 主循环
        agent_loop(history)
        
        # 提取并显示 Agent 的响应
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            # 响应可能包含多个块（文本 + 工具调用）
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        
        print()  # 空行分隔
