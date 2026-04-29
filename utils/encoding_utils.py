"""
编码处理工具 - 处理非 UTF-8 编码的输出
"""
import subprocess
from pathlib import Path
from typing import Optional


def safe_subprocess_run(
    command: str,
    cwd: Path,
    timeout: int = 120,
    encoding: str = 'utf-8',
    errors: str = 'replace',
    capture_output: bool = True
) -> subprocess.CompletedProcess:
    """
    安全的 subprocess.run 封装，处理编码问题
    
    Args:
        command: 要执行的命令
        cwd: 工作目录
        timeout: 超时时间
        encoding: 编码格式
        errors: 错误处理策略（'replace', 'ignore', 'strict'）
        capture_output: 是否捕获输出
        
    Returns:
        subprocess.CompletedProcess 对象
    """
    return subprocess.run(
        command,
        shell=True,
        cwd=str(cwd),
        capture_output=capture_output,
        text=True,
        timeout=timeout,
        encoding=encoding,
        errors=errors
    )


def decode_output(output: bytes, encoding: str = 'utf-8', errors: str = 'replace') -> str:
    """
    安全地解码字节输出
    
    Args:
        output: 字节输出
        encoding: 目标编码
        errors: 错误处理策略
        
    Returns:
        解码后的字符串
    """
    try:
        return output.decode(encoding=encoding, errors=errors)
    except Exception:
        # 回退到 latin-1，它能解码任何字节
        return output.decode(encoding='latin-1', errors='replace')
