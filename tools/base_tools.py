"""
基础工具 - s01: Agent 与环境交互的最基本工具
"""
from pathlib import Path
from typing import Optional

from config.settings import WORKDIR
from utils.path_utils import safe_path


def run_bash(command: str) -> str:
    """
    执行 Shell 命令（带安全检查）
    
    Args:
        command: 要执行的 shell 命令
        
    Returns:
        命令输出（stdout + stderr）
    """
    # 危险命令黑名单
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "错误: 危险命令被阻止"
    
    try:
        from utils.encoding_utils import safe_subprocess_run
        r = safe_subprocess_run(
            command,
            cwd=WORKDIR,
            timeout=120
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(无输出)"
    except Exception as e:
        if "timed out" in str(e).lower():
            return "错误: 超时 (120秒)"
        return f"错误: {e}"


def run_read(path: str, limit: Optional[int] = None) -> str:
    """
    读取文件内容
    
    Args:
        path: 文件路径
        limit: 可选的行数限制
        
    Returns:
        文件内容（可能被截断）
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
    
    Args:
        path: 文件路径
        old_text: 要替换的旧文本
        new_text: 新文本
        
    Returns:
        操作结果消息
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
