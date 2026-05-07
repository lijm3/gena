"""
配置管理模块 - 支持环境变量和配置文件
"""
import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# 加载环境变量
load_dotenv(override=True)

# === 全局常量 ===
WORKDIR = Path.cwd()  # 工作目录

# === 目录结构 ===
TEAM_DIR = WORKDIR / ".team"          # 团队配置目录
INBOX_DIR = TEAM_DIR / "inbox"        # 消息收件箱目录
TASKS_DIR = WORKDIR / ".tasks"        # 任务存储目录
SKILLS_DIR = WORKDIR / "skills"       # 技能文档目录
TRANSCRIPT_DIR = WORKDIR / ".transcripts"  # 对话记录目录

# === 配置参数 ===
TOKEN_THRESHOLD = int(os.environ.get("TOKEN_THRESHOLD", "100000"))  # 触发自动压缩的 token 阈值
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))           # 空闲时轮询间隔（秒）
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "60"))            # 空闲超时时间（秒）

# === Agent 循环控制参数 ===
# 硬上限：用于保证主循环可以在可预期的边界内结束，避免无限调用工具。
MAX_AGENT_ROUNDS = int(os.environ.get("MAX_AGENT_ROUNDS", "12"))
MAX_TOOL_CALLS = int(os.environ.get("MAX_TOOL_CALLS", "30"))
MAX_TOOL_CALLS_PER_ROUND = int(os.environ.get("MAX_TOOL_CALLS_PER_ROUND", "5"))

# 重复调用检测：窗口越小越敏感，阈值越低越容易触发收敛。
LOOP_DETECTION_WINDOW = int(os.environ.get("LOOP_DETECTION_WINDOW", "3"))
LOOP_DETECTION_THRESHOLD = int(os.environ.get("LOOP_DETECTION_THRESHOLD", "2"))

# 无进展检测：连续多轮工具结果相似时主动终止，避免“有调用、无推进”。
PROGRESS_WINDOW = int(os.environ.get("PROGRESS_WINDOW", "3"))
PROGRESS_SIMILARITY_THRESHOLD = float(os.environ.get("PROGRESS_SIMILARITY_THRESHOLD", "0.9"))

# 预算约束：双保险，任何一个预算触发都应终止工具循环并收敛输出。
WALL_CLOCK_TIMEOUT = int(os.environ.get("WALL_CLOCK_TIMEOUT", "60"))
TOKEN_BUDGET = int(os.environ.get("TOKEN_BUDGET", "150000"))

# === LLM 配置 ===
class LLMConfig:
    """LLM 配置类"""
    
    @staticmethod
    def get_base_url() -> str:
        """获取 API 基础 URL"""
        return os.environ.get(
            "ANTHROPIC_BASE_URL",
            "https://cngpt.net"
        )
    
    @staticmethod
    def get_auth_token() -> str:
        """获取认证 Token"""
        token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        if not token:
            raise ValueError("ANTHROPIC_AUTH_TOKEN 环境变量未设置")
        return token
    
    @staticmethod
    def get_model() -> str:
        """获取模型名称"""
        return os.environ.get("MODEL_NAME", "gpt-5.4")
    
    @staticmethod
    def get_max_tokens() -> int:
        """获取最大 token 数"""
        return int(os.environ.get("MAX_TOKENS", "8000"))
    
    @staticmethod
    def get_timeout() -> int:
        """获取请求超时时间（秒）"""
        return int(os.environ.get("REQUEST_TIMEOUT", "60"))

# === 消息类型白名单 ===
VALID_MSG_TYPES = {"message", "broadcast", "shutdown_request",
                   "shutdown_response", "plan_approval_response"}
