"""
路径安全工具
"""
from pathlib import Path
from config.settings import WORKDIR


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
