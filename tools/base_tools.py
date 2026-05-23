"""
基础工具 - s01: Agent 与环境交互的最基本工具
"""
from typing import Optional

from config.settings import WORKDIR
from utils.path_utils import safe_path
from utils.loop_control import ToolResult
from utils.image_utils import file_to_image_block


def run_bash(command: str) -> ToolResult:
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
        return ToolResult(
            content="错误: 危险命令被阻止",
            changed=False,
            done=True,
            error=True
        )
    
    try:
        from utils.encoding_utils import safe_subprocess_run
        r = safe_subprocess_run(
            command,
            cwd=WORKDIR,
            timeout=120
        )
        out = (r.stdout + r.stderr).strip()
        output = out[:50000] if out else "(无输出)"
        return ToolResult(content=output, changed=True, done=True)
    except Exception as e:
        if "timed out" in str(e).lower():
            return ToolResult(
                content="错误: 超时 (120秒)",
                changed=False,
                done=False,
                error=True
            )
        return ToolResult(
            content=f"错误: {e}",
            changed=False,
            done=False,
            error=True
        )


def run_read(path: str, limit: Optional[int] = None) -> ToolResult:
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
        return ToolResult(content="\n".join(lines)[:50000], changed=False, done=True)
    except Exception as e:
        return ToolResult(
            content=f"错误: {e}",
            changed=False,
            done=False,
            error=True
        )


def run_write(path: str, content: str) -> ToolResult:
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
        if fp.exists():
            existing = fp.read_text()
            if existing == content:
                return ToolResult(
                    content=f"文件 {path} 内容未变化",
                    changed=False,
                    done=True
                )

        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return ToolResult(content=f"已写入 {len(content)} 字节到 {path}", changed=True, done=True)
    except Exception as e:
        return ToolResult(
            content=f"错误: {e}",
            changed=False,
            done=False,
            error=True
        )


def run_edit(path: str, old_text: str, new_text: str) -> ToolResult:
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
            return ToolResult(
                content=f"错误: 在 {path} 中找不到指定文本",
                changed=False,
                done=True,
                error=True
            )

        updated = c.replace(old_text, new_text, 1)
        if updated == c:
            return ToolResult(content=f"文件 {path} 无变化", changed=False, done=True)

        fp.write_text(updated)
        return ToolResult(content=f"已编辑 {path}", changed=True, done=True)
    except Exception as e:
        return ToolResult(
            content=f"错误: {e}",
            changed=False,
            done=False,
            error=True
        )


def run_read_image(path: str) -> ToolResult:
    """
    读取本地图片。

    为什么不直接把 image block 塞进 tool_result.content：
      Anthropic 官方协议允许，但很多第三方网关只在 user message 的顶层 content 列表里
      识别 image block，放在 tool_result 内部就当看不见。我们改为把 image block 放到
      ToolResult.metadata["sidecar_blocks"] 里，由 _execute_tools 提到 tool_result 的同级，
      让图片始终出现在 user message 顶层。

    Args:
        path: 工作目录下的相对路径或绝对路径

    Returns:
        ToolResult，content 为一行说明文字；图片在 metadata["sidecar_blocks"] 中
    """
    try:
        block = file_to_image_block(path)
        size = len(block["source"]["data"]) * 3 // 4
        return ToolResult(
            content=f"已加载图片 {path}（{block['source']['media_type']}, ~{size}B），见下方 image block。",
            changed=False,
            done=True,
            metadata={"sidecar_blocks": [block]},
        )
    except Exception as e:
        return ToolResult(
            content=f"错误: {e}",
            changed=False,
            done=True,
            error=True,
        )
