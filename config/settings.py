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
