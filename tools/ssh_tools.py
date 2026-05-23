"""
SSH 远程主机工具集 - 统一返回 ToolResult,接入循环防护与进展检测

9 个工具:
  - ssh_exec / ssh_read / ssh_write / ssh_upload / ssh_download  操作
  - ssh_list_hosts                                                查询
  - ssh_add_host / ssh_remove_host                                运行时注册(仅 Lead)

同步纪律:
  - get_handler + get_tools 双改 (tools/tool_dispatcher.py)
  - Subagent (agents/subagent.py) 同步 6 项操作 + list_hosts,不同步 add/remove
  - Teammate (managers/teammate_manager.py) 只同步 ssh_exec + ssh_list_hosts(只读子集)
"""
from typing import Optional

from utils.loop_control import ToolResult
from utils.ssh_client import ssh_pool


def _wrap(ok: bool, output: str, *, changed: bool, done: bool = True) -> ToolResult:
    """ok=False 时把 changed 也压回 False,让 ProgressTracker 看到"无进展"。"""
    return ToolResult(content=output, changed=changed if ok else False, done=done, error=not ok)


def _err(e: Exception) -> ToolResult:
    return ToolResult(content=f"错误: {type(e).__name__}: {e}", changed=False, done=True, error=True)


def ssh_exec(host: str, command: str, timeout: int = 120) -> ToolResult:
    """远程执行命令。stdout+stderr 合并,带 exit code 前缀(失败时)。"""
    try:
        ok, out = ssh_pool.exec(host, command, timeout=timeout)
        return _wrap(ok, out, changed=True)
    except Exception as e:
        return _err(e)


def ssh_read(host: str, path: str, limit: Optional[int] = None) -> ToolResult:
    try:
        ok, out = ssh_pool.read_file(host, path, limit)
        return _wrap(ok, out, changed=False)
    except Exception as e:
        return _err(e)


def ssh_write(host: str, path: str, content: str) -> ToolResult:
    try:
        ok, out = ssh_pool.write_file(host, path, content)
        return _wrap(ok, out, changed=True)
    except Exception as e:
        return _err(e)


def ssh_upload(host: str, local_path: str, remote_path: str) -> ToolResult:
    try:
        ok, out = ssh_pool.upload(host, local_path, remote_path)
        return _wrap(ok, out, changed=True)
    except Exception as e:
        return _err(e)


def ssh_download(host: str, remote_path: str, local_path: str) -> ToolResult:
    try:
        ok, out = ssh_pool.download(host, remote_path, local_path)
        return _wrap(ok, out, changed=True)
    except Exception as e:
        return _err(e)


def ssh_list_hosts() -> ToolResult:
    return ToolResult(content=ssh_pool.list_hosts(), changed=False, done=True)


def ssh_add_host(
    name: str,
    hostname: str,
    username: str,
    password: str,
    allowed_prefix: str,
    port: int = 22,
    description: str = "",
) -> ToolResult:
    """运行时注册一台密码登录的远程主机(仅内存,REPL 退出即丢)。

    安全注意:密码作为入参传入,会出现在 LLM history、agent.log、transcript 中。
    仅推荐用于一次性内网测试。需要持久化请改用 password_env 预登记。

    返回里不回显密码——避免在 tool_result 再泄一次。
    """
    try:
        ssh_pool.register_runtime_host(
            name=name, hostname=hostname, username=username,
            password=password, allowed_prefix=allowed_prefix,
            port=port, description=description,
        )
        # 显式不回 password,避免下一轮 LLM 把它当成"上下文"再次输出
        return ToolResult(
            content=f"已注册运行时主机 '{name}' ({username}@{hostname}:{port}, prefix={allowed_prefix})",
            changed=True, done=True,
        )
    except Exception as e:
        return _err(e)


def ssh_remove_host(name: str) -> ToolResult:
    """删除运行时注册的主机。预登记 host 不可通过此工具删除。"""
    try:
        return ToolResult(content=ssh_pool.unregister_runtime_host(name), changed=True, done=True)
    except Exception as e:
        return _err(e)
