# SSH 远程主机支持方案

> 编写日期：2026/05/21
> 适用版本：当前 `develop` 分支（已包含图片支持、SQLite 状态层）
> 目标：让 Lead / Subagent / Teammate 都能在受控的远程主机上执行命令、读写文件、传输文件，并复用现有的循环防护与压缩管道。

---

## 一、设计目标

把"模型只能在本机 `subprocess` 里跑命令"扩展为"模型可以在一组**受控的远程主机**(预登记 / 运行时注册皆可)上以同样的语义工作",要满足:

1. **分层凭据策略**:
   - **key 模式(预登记)**:私钥路径、passphrase 从 `.env` + `.team/ssh_hosts.json` 读,**LLM 看不到原文**
   - **密码模式(预登记)**:密码从 `.env` 读,LLM 看不到
   - **密码模式(运行时)**:用户在 REPL 直接告诉 agent `连 192.168.1.5 root xxxxxx`,**密码作为工具入参进 LLM history**——已知信息泄露面换取最简体验,仅推荐用于一次性 / 内网测试场景
2. **复用现有防护层**:远程工具返回 `ToolResult`,带 `changed/done/error` 状态前缀,让 `ProgressTracker` 与 `LoopDetector` 照常生效。
3. **连接复用**:同一 host 多次调用复用一个 `SSHClient` + `SFTPClient`,避免每次工具调用都付 TCP/握手的 200–500ms。
4. **路径安全**:远程路径走与本地 `safe_path` 等价的约束(仅允许在 host 配置的 `allowed_prefix` 之下,无论该 host 来自 `ssh_hosts.json` 还是运行时 `ssh_add_host`),避免被 prompt injection 写 `/etc/passwd`。
5. **编码一致**:远程命令默认 UTF-8 解码,失败回退 latin-1,与本地 `safe_subprocess_run` 行为对齐(项目主跑 Windows,远端常见 Ubuntu)。
6. **多层 Agent 同步**:Lead 工具表(`tools/tool_dispatcher.py`)、Subagent 硬编码工具表(`agents/subagent.py`)、Teammate 硬编码工具表(`managers/teammate_manager.py`)需要按 `CLAUDE.md` 的工具同步纪律统一加。
7. **优雅退出**:进程结束时把所有 SSH/SFTP 连接关掉,避免远端僵尸会话(部分堡垒机会因此踢人或计费)。

---

## 二、技术选型

| 维度 | 选择 | 理由 |
|---|---|---|
| SSH 库 | **`paramiko>=3.4`** | 纯 Python,无须本机有 `ssh` 二进制(Windows 友好);成熟、社区大、`SFTPClient` 用法直观 |
| 替代方案否决 | `fabric` | 在 paramiko 上又套一层 Task/Context,本项目工具粒度本就细,徒增依赖 |
| 替代方案否决 | 调用系统 `ssh user@host cmd` | Windows 部分环境不带 `ssh.exe`;输出编码、长命令、SFTP 都需要自己重做;无连接复用 |
| 凭据存储 | `.env`(passphrase / 预登记密码) + `.team/ssh_hosts.json`(预登记 host 元数据) + 进程内存(运行时 host) | 与项目现有 `python-dotenv` + `.team/config.json` 习惯一致;`.gitignore` 已覆盖 `.team/` |
| 凭据形态 | **三档**:① key + passphrase(env,推荐);② 密码 + env 变量名(预登记 legacy);③ 密码 + 明文(运行时 LLM 直接调 `ssh_add_host` 注册,凭据进 history 与 transcript) | 默认走 ① ② 严格模式;③ 仅在用户明示"我接受信息泄露"时启用,目标是临时内网测试机 |
| 运行时 host 持久化 | **不持久化**:`ssh_add_host` 注册的 host 只活在进程内存,REPL 退出即丢 | 避免明文密码意外落盘;持久化需求请改走 ② 预登记模式 |
| 连接池 | 进程级单例 `SSHPool` | 与 `BackgroundManager`、`TeammateManager` 同层级,模块加载即实例化 |
| 长命令 | 与现有 `BackgroundManager` 协作 | 加 `background_ssh_exec`,把远程长任务也归口到统一的后台通知队列 |

---

## 三、实施方案

### 3.1 新模块:`utils/ssh_client.py`

集中放 SSH 连接池、凭据装载、远程命令执行、SFTP 读写。**绝不在 utils 之外直接 import paramiko**——保持网络细节封闭在这一处,日后换实现(比如换 `asyncssh`)只动这文件。

```python
# utils/ssh_client.py
"""
SSH 连接池与远程 IO

设计:
  - SSHPool 进程级单例,按 host 名缓存 (SSHClient, SFTPClient) 二元组
  - 第一次访问 host 时按 .team/ssh_hosts.json 的配置连上
  - 所有公开方法都返回 (ok: bool, output: str),由上层 ssh_tools 包成 ToolResult
  - 远程命令默认 UTF-8 → latin-1 回退解码,与 utils/encoding_utils 行为一致
  - 不暴露 SSHClient/SFTPClient 给外部,避免 paramiko 类型泄漏到上层
"""
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Optional, Tuple

import paramiko

from config.settings import TEAM_DIR


SSH_HOSTS_PATH = TEAM_DIR / "ssh_hosts.json"
DEFAULT_CONNECT_TIMEOUT = 10   # 秒,握手超时
DEFAULT_EXEC_TIMEOUT = 120     # 秒,与本地 run_bash 一致
MAX_OUTPUT_BYTES = 50_000      # 与本地 run_bash 一致,避免单条 tool_result 过大


@dataclass(frozen=True)
class HostConfig:
    """单台主机的连接配置。

    凭据三选一(_load_host_configs / register_runtime_host 强校验):
      ① key_path + passphrase_env(预登记,推荐)
      ② password_env(预登记密码,legacy 兜底)
      ③ password_plain(运行时,LLM 直接给的明文,仅活在内存)

    allowed_prefix 是远端路径白名单——所有 read/write/upload/download 必须落在它下面,
    与本地 safe_path 守护工作目录是同一个思路。
    """
    name: str
    hostname: str
    port: int
    username: str
    # —— 凭据(三选一) ——
    key_path: Optional[str] = None          # ① 私钥文件路径(本机),例如 ~/.ssh/id_ed25519
    passphrase_env: Optional[str] = None    # ① 私钥 passphrase 的环境变量名
    password_env: Optional[str] = None      # ② 密码的环境变量名(预登记,legacy)
    password_plain: Optional[str] = None    # ③ 运行时明文密码(仅 register_runtime_host 写)
    # —— 其他 ——
    allowed_prefix: str = ""                # 远端路径白名单根,例如 /home/deploy/app
    description: str = ""
    is_runtime: bool = False                # True = 内存中,不可被持久化到 ssh_hosts.json


def _load_host_configs() -> Dict[str, HostConfig]:
    """读 .team/ssh_hosts.json。文件不存在视为空配置,不抛错。

    凭据校验:每台预登记 host 必须且仅能配置 key_path / password_env 中的一项。
    password_plain 是运行时专用,不允许从 JSON 加载——避免明文密码落盘。
    """
    if not SSH_HOSTS_PATH.exists():
        return {}
    raw = json.loads(SSH_HOSTS_PATH.read_text(encoding="utf-8"))
    out: Dict[str, HostConfig] = {}
    for h in raw.get("hosts", []):
        if "password_plain" in h:
            raise ValueError(
                f"主机 '{h['name']}' 在 ssh_hosts.json 出现 password_plain 字段;"
                f"明文密码禁止落盘,请改用 password_env 或运行时 ssh_add_host"
            )
        key_path = h.get("key_path")
        password_env = h.get("password_env")
        # 严格互斥:不允许"key 装载失败时静默降级到密码"——凭据形态必须在配置阶段确定
        if bool(key_path) == bool(password_env):
            raise ValueError(
                f"主机 '{h['name']}' 必须且仅能配置 key_path 或 password_env 中的一项;"
                f"当前 key_path={key_path!r}, password_env={password_env!r}"
            )
        out[h["name"]] = HostConfig(
            name=h["name"],
            hostname=h["hostname"],
            port=int(h.get("port", 22)),
            username=h["username"],
            key_path=os.path.expanduser(key_path) if key_path else None,
            passphrase_env=h.get("passphrase_env"),
            password_env=password_env,
            allowed_prefix=h["allowed_prefix"].rstrip("/") + "/",
            description=h.get("description", ""),
            is_runtime=False,
        )
    return out


def _remote_safe_path(host: HostConfig, path: str) -> str:
    """对远端路径做白名单约束,等价本地 safe_path。

    - 拒绝 .. 解析后跨出 allowed_prefix
    - 统一为 POSIX 形式(远端基本都是 Linux/Unix)
    """
    p = PurePosixPath(path)
    if not p.is_absolute():
        p = PurePosixPath(host.allowed_prefix) / p
    # POSIX 没有 resolve(strict=False),用字符串归一化:os.path.normpath 在 POSIX 系统语义下
    # 处理 ..;为避免 Windows 上跑出 \,这里手工归一。
    parts = []
    for seg in str(p).split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    normalized = "/" + "/".join(parts)
    if not (normalized + "/").startswith(host.allowed_prefix):
        raise PermissionError(
            f"远端路径 {normalized} 越出主机 '{host.name}' 的白名单 {host.allowed_prefix}"
        )
    return normalized


class SSHPool:
    """进程级 SSH 连接池。线程安全。

    维护两组 host:
      - self._hosts:预登记,来自 ssh_hosts.json,进程重启会重新加载
      - self._runtime_hosts:运行时通过 ssh_add_host 注册,只活在内存
    查找顺序:运行时 > 预登记;同名时运行时覆盖(让用户可以临时改某台机器的配置)
    """

    def __init__(self):
        self._hosts = _load_host_configs()
        self._runtime_hosts: Dict[str, HostConfig] = {}
        self._conns: Dict[str, Tuple[paramiko.SSHClient, paramiko.SFTPClient]] = {}
        self._lock = threading.Lock()

    # —— 元信息 ——

    def reload(self) -> None:
        """ssh_hosts.json 改了之后用,不动现有活跃连接。"""
        self._hosts = _load_host_configs()

    def list_hosts(self) -> str:
        all_hosts = {**self._hosts, **self._runtime_hosts}
        if not all_hosts:
            return "(no hosts; use ssh_add_host(...) or fill .team/ssh_hosts.json)"
        rows = ["name | user@host:port | mode | allowed_prefix | desc"]
        for h in all_hosts.values():
            if h.key_path:
                mode = "key"
            elif h.password_env:
                mode = "pwd-env"
            else:
                mode = "pwd-runtime"
            scope = " [runtime]" if h.is_runtime else ""
            rows.append(
                f"{h.name}{scope} | {h.username}@{h.hostname}:{h.port} | "
                f"{mode} | {h.allowed_prefix} | {h.description}"
            )
        return "\n".join(rows)

    def get_host(self, name: str) -> HostConfig:
        # 运行时优先,允许临时覆盖同名预登记 host
        h = self._runtime_hosts.get(name) or self._hosts.get(name)
        if h is None:
            raise KeyError(
                f"主机 '{name}' 未注册;预登记: {list(self._hosts)}, "
                f"运行时: {list(self._runtime_hosts)}"
            )
        return h

    def register_runtime_host(
        self,
        name: str,
        hostname: str,
        username: str,
        password: str,
        allowed_prefix: str,
        port: int = 22,
        description: str = "",
    ) -> str:
        """运行时注册一台密码登录的 host。仅活在内存,REPL 退出丢失。

        允许重复注册同名 host(覆盖),方便用户改错密码后重试。
        """
        if not all([name, hostname, username, password, allowed_prefix]):
            raise ValueError("name / hostname / username / password / allowed_prefix 都必须非空")
        with self._lock:
            self._runtime_hosts[name] = HostConfig(
                name=name,
                hostname=hostname,
                port=port,
                username=username,
                password_plain=password,
                allowed_prefix=allowed_prefix.rstrip("/") + "/",
                description=description or "(runtime)",
                is_runtime=True,
            )
            # 同名旧连接立刻关闭(密码/IP 可能变了,旧 channel 必须丢)
            old = self._conns.pop(name, None)
            if old:
                self._safe_close(old)
        return f"已注册运行时主机 '{name}' ({username}@{hostname}:{port}) [memory-only]"

    def unregister_runtime_host(self, name: str) -> str:
        with self._lock:
            if name not in self._runtime_hosts:
                return f"主机 '{name}' 不存在或不是运行时主机"
            self._runtime_hosts.pop(name)
            old = self._conns.pop(name, None)
            if old:
                self._safe_close(old)
        return f"已删除运行时主机 '{name}'"

    # —— 连接装载 ——

    def _connect(self, host: HostConfig) -> Tuple[paramiko.SSHClient, paramiko.SFTPClient]:
        cli = paramiko.SSHClient()
        # 安全:不接受未知 host key,要求用户先 ssh 一次写进 known_hosts。
        # 防止中间人替换 host key 后 agent 无感知地把命令发去别处。
        cli.load_system_host_keys()
        cli.set_missing_host_key_policy(paramiko.RejectPolicy())

        # 通用连接参数:无论 key 还是 password 模式都禁掉 agent 转发和盲扫 ~/.ssh,
        # 强制走 ssh_hosts.json 里配的那一份凭据,杜绝"凭据来源含糊"。
        kwargs = dict(
            hostname=host.hostname,
            port=host.port,
            username=host.username,
            timeout=DEFAULT_CONNECT_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )

        if host.key_path:
            # —— ① key 模式(推荐):仅支持 Ed25519 / RSA / ECDSA ——
            passphrase = os.environ.get(host.passphrase_env) if host.passphrase_env else None
            kwargs["pkey"] = self._load_private_key(host.key_path, passphrase)
        elif host.password_env:
            # —— ② 预登记密码(legacy 兜底) ——
            pwd = os.environ.get(host.password_env)
            if not pwd:
                raise ValueError(
                    f"主机 '{host.name}' 配了 password_env={host.password_env},"
                    f"但该环境变量为空 / 未设置"
                )
            kwargs["password"] = pwd
        elif host.password_plain:
            # —— ③ 运行时明文密码(LLM 直接给,仅内存) ——
            kwargs["password"] = host.password_plain
        else:
            raise ValueError(f"主机 '{host.name}' 无可用凭据(既无 key_path 也无 password_*)")

        cli.connect(**kwargs)
        sftp = cli.open_sftp()
        return cli, sftp

    @staticmethod
    def _load_private_key(path: str, passphrase: Optional[str]):
        """按 key 类型尝试装载。paramiko 没有统一入口,只能挨个试。"""
        errors = []
        for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
            try:
                return cls.from_private_key_file(path, password=passphrase)
            except paramiko.SSHException as e:
                errors.append(f"{cls.__name__}: {e}")
        raise ValueError(f"无法装载私钥 {path}: {'; '.join(errors)}")

    def _get_conn(self, name: str) -> Tuple[paramiko.SSHClient, paramiko.SFTPClient, HostConfig]:
        with self._lock:
            host = self.get_host(name)
            conn = self._conns.get(name)
            if conn is not None:
                # 简单存活检查:SFTP listdir 一下根目录(失败说明连接断了,重连)
                try:
                    conn[1].stat(host.allowed_prefix)
                    return conn[0], conn[1], host
                except Exception:
                    self._safe_close(conn)
                    self._conns.pop(name, None)
            cli, sftp = self._connect(host)
            self._conns[name] = (cli, sftp)
            return cli, sftp, host

    # —— 业务方法 ——

    def exec(self, name: str, command: str, timeout: int = DEFAULT_EXEC_TIMEOUT) -> Tuple[bool, str]:
        """远程执行单条命令。stdout + stderr 合并截断。"""
        cli, _, _ = self._get_conn(name)
        # get_pty=False:不分配伪终端;tty 会把 stdout/stderr 混着回来还带控制字符。
        stdin, stdout, stderr = cli.exec_command(command, timeout=timeout, get_pty=False)
        out_b = stdout.read()
        err_b = stderr.read()
        code = stdout.channel.recv_exit_status()
        text = self._decode(out_b) + self._decode(err_b)
        text = text[:MAX_OUTPUT_BYTES] if text else "(无输出)"
        ok = code == 0
        prefix = f"(exit={code}) " if not ok else ""
        return ok, prefix + text

    def read_file(self, name: str, path: str, limit: Optional[int] = None) -> Tuple[bool, str]:
        _, sftp, host = self._get_conn(name)
        rp = _remote_safe_path(host, path)
        with sftp.open(rp, "rb") as f:
            data = f.read(MAX_OUTPUT_BYTES + 1)
        text = self._decode(data)
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... (还有 {len(lines) - limit} 行)"]
        out = "\n".join(lines)[:MAX_OUTPUT_BYTES]
        if len(data) > MAX_OUTPUT_BYTES:
            out += "\n... (内容被截断)"
        return True, out

    def write_file(self, name: str, path: str, content: str) -> Tuple[bool, str]:
        _, sftp, host = self._get_conn(name)
        rp = _remote_safe_path(host, path)
        # mkdir -p 远端目录(SFTP 没有原生 -p,要自己来)
        self._sftp_mkdirs(sftp, str(PurePosixPath(rp).parent), host.allowed_prefix)
        with sftp.open(rp, "wb") as f:
            f.write(content.encode("utf-8"))
        return True, f"已写入 {len(content)} 字节到 {name}:{rp}"

    def upload(self, name: str, local_path: str, remote_path: str) -> Tuple[bool, str]:
        from utils.path_utils import safe_path
        lp = safe_path(local_path)  # 本地路径走原有守护
        if not lp.is_file():
            return False, f"本地文件不存在: {local_path}"
        _, sftp, host = self._get_conn(name)
        rp = _remote_safe_path(host, remote_path)
        self._sftp_mkdirs(sftp, str(PurePosixPath(rp).parent), host.allowed_prefix)
        sftp.put(str(lp), rp)
        return True, f"已上传 {lp.stat().st_size} 字节 → {name}:{rp}"

    def download(self, name: str, remote_path: str, local_path: str) -> Tuple[bool, str]:
        from utils.path_utils import safe_path
        _, sftp, host = self._get_conn(name)
        rp = _remote_safe_path(host, remote_path)
        lp = safe_path(local_path)
        lp.parent.mkdir(parents=True, exist_ok=True)
        sftp.get(rp, str(lp))
        return True, f"已下载 {name}:{rp} → {lp} ({lp.stat().st_size} 字节)"

    # —— 内部 ——

    @staticmethod
    def _sftp_mkdirs(sftp: paramiko.SFTPClient, remote_dir: str, root: str) -> None:
        """SFTP mkdir -p,从 allowed_prefix 一级一级往下建。"""
        if not (remote_dir + "/").startswith(root):
            raise PermissionError(f"mkdir 目标 {remote_dir} 越出白名单 {root}")
        parts = remote_dir.strip("/").split("/")
        cur = ""
        for p in parts:
            cur += "/" + p
            try:
                sftp.stat(cur)
            except IOError:
                sftp.mkdir(cur)

    @staticmethod
    def _decode(b: bytes) -> str:
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return b.decode("latin-1", errors="replace")

    @staticmethod
    def _safe_close(conn: Tuple[paramiko.SSHClient, paramiko.SFTPClient]) -> None:
        try:
            conn[1].close()
        except Exception:
            pass
        try:
            conn[0].close()
        except Exception:
            pass

    def close_all(self) -> None:
        """进程退出时调一次。同时清空运行时 host(明文密码不该跨进程残留)。"""
        with self._lock:
            for c in self._conns.values():
                self._safe_close(c)
            self._conns.clear()
            self._runtime_hosts.clear()


# 模块级单例;按需 import 即可
ssh_pool = SSHPool()
```

### 3.2 配置:`.team/ssh_hosts.json` + `.env`

`.team/ssh_hosts.json` 范例(用户手动维护,**不要让 LLM 通过工具写它**——`ssh_add_host` 注册的运行时 host 永远只在内存,不应写入此文件)。**JSON 内每台 host 必须二选一**:配 `key_path`(推荐) 或 `password_env`(legacy);第三档"明文密码"只走运行时 `ssh_add_host`,不允许出现在 JSON 中:

```json
{
  "hosts": [
    {
      "name": "staging",
      "hostname": "10.0.1.42",
      "port": 22,
      "username": "deploy",
      "key_path": "~/.ssh/id_ed25519_staging",
      "passphrase_env": "SSH_PASSPHRASE_STAGING",
      "allowed_prefix": "/home/deploy/app",
      "description": "预发布环境,key + passphrase"
    },
    {
      "name": "build-box",
      "hostname": "build.internal",
      "port": 2222,
      "username": "ci",
      "key_path": "~/.ssh/id_ed25519_ci",
      "passphrase_env": null,
      "allowed_prefix": "/srv/ci-workspace",
      "description": "构建机,key 无 passphrase"
    },
    {
      "name": "legacy-server",
      "hostname": "192.168.10.5",
      "port": 22,
      "username": "admin",
      "password_env": "SSH_PASSWORD_LEGACY",
      "allowed_prefix": "/data/scripts",
      "description": "老设备,只能密码登录;封闭内网仅做兜底"
    }
  ]
}
```

> **互斥规则**:一个 host 同时出现 `key_path` 和 `password_env`(或两者都没有)会在加载时直接抛 `ValueError`。这样做是为了堵死"key 装载失败 → 静默降级到密码"的攻击面——凭据形态必须在配置阶段就确定。

`.env` 追加(只放敏感字段,**仓库永远看不到**):

```ini
# key 模式 - passphrase
SSH_PASSPHRASE_STAGING=your-passphrase-here
# SSH_PASSPHRASE_BUILDBOX 不需要(passphrase_env=null)

# 密码模式 - 仅 legacy
SSH_PASSWORD_LEGACY=your-password-here
```

`.gitignore` 验证:`.team/` 已被忽略;`.env` 已被忽略(`79df037` commit 加的)。**无需新增 ignore 规则**。

### 3.3 新模块:`tools/ssh_tools.py`

把 `ssh_pool` 的方法包成统一的 `ToolResult`。**所有错误都以 `ToolResult(error=True)` 返回**,让 LLM 看到状态前缀,与本地 `run_bash` 的语义对齐。

```python
# tools/ssh_tools.py
"""
SSH 远程主机工具集 - 统一返回 ToolResult,接入循环防护与进展检测
"""
from typing import Optional

from utils.loop_control import ToolResult
from utils.ssh_client import ssh_pool


def _wrap(ok: bool, output: str, *, changed: bool, done: bool = True) -> ToolResult:
    return ToolResult(content=output, changed=changed if ok else False, done=done, error=not ok)


def ssh_exec(host: str, command: str, timeout: int = 120) -> ToolResult:
    """远程执行命令。stdout+stderr 合并,带 exit code 前缀(失败时)。"""
    try:
        ok, out = ssh_pool.exec(host, command, timeout=timeout)
        return _wrap(ok, out, changed=True)
    except Exception as e:
        return ToolResult(content=f"错误: {type(e).__name__}: {e}", changed=False, done=True, error=True)


def ssh_read(host: str, path: str, limit: Optional[int] = None) -> ToolResult:
    try:
        ok, out = ssh_pool.read_file(host, path, limit)
        return _wrap(ok, out, changed=False)
    except Exception as e:
        return ToolResult(content=f"错误: {type(e).__name__}: {e}", changed=False, done=True, error=True)


def ssh_write(host: str, path: str, content: str) -> ToolResult:
    try:
        ok, out = ssh_pool.write_file(host, path, content)
        return _wrap(ok, out, changed=True)
    except Exception as e:
        return ToolResult(content=f"错误: {type(e).__name__}: {e}", changed=False, done=True, error=True)


def ssh_upload(host: str, local_path: str, remote_path: str) -> ToolResult:
    try:
        ok, out = ssh_pool.upload(host, local_path, remote_path)
        return _wrap(ok, out, changed=True)
    except Exception as e:
        return ToolResult(content=f"错误: {type(e).__name__}: {e}", changed=False, done=True, error=True)


def ssh_download(host: str, remote_path: str, local_path: str) -> ToolResult:
    try:
        ok, out = ssh_pool.download(host, remote_path, local_path)
        return _wrap(ok, out, changed=True)
    except Exception as e:
        return ToolResult(content=f"错误: {type(e).__name__}: {e}", changed=False, done=True, error=True)


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

    返回里**不回显密码**——避免在 tool_result 再泄一次。
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
        return ToolResult(content=f"错误: {type(e).__name__}: {e}", changed=False, done=True, error=True)


def ssh_remove_host(name: str) -> ToolResult:
    """删除运行时注册的主机。预登记 host 不可通过此工具删除。"""
    try:
        return ToolResult(content=ssh_pool.unregister_runtime_host(name), changed=True, done=True)
    except Exception as e:
        return ToolResult(content=f"错误: {type(e).__name__}: {e}", changed=False, done=True, error=True)
```

### 3.4 主分发器同步:`tools/tool_dispatcher.py`

`get_handler` 与 `get_tools` 双改(`CLAUDE.md` 反复强调过这两者必须同步)。**SSH 工具一律标注 host 是必填,且 description 里明确"必须是 ssh_list_hosts 返回的 name 之一"**——给模型一个清晰的约束。

```python
# tools/tool_dispatcher.py 顶部 import
from tools.ssh_tools import (
    ssh_exec, ssh_read, ssh_write, ssh_upload, ssh_download, ssh_list_hosts,
    ssh_add_host, ssh_remove_host,
)
```

`get_handler` 字典追加:

```python
"ssh_exec":      lambda **kw: ssh_exec(kw["host"], kw["command"], kw.get("timeout", 120)),
"ssh_read":      lambda **kw: ssh_read(kw["host"], kw["path"], kw.get("limit")),
"ssh_write":     lambda **kw: ssh_write(kw["host"], kw["path"], kw["content"]),
"ssh_upload":    lambda **kw: ssh_upload(kw["host"], kw["local_path"], kw["remote_path"]),
"ssh_download":  lambda **kw: ssh_download(kw["host"], kw["remote_path"], kw["local_path"]),
"ssh_list_hosts": lambda **kw: ssh_list_hosts(),
# 运行时注册:用户在 REPL 里把 IP/用户名/密码直接告诉 agent 时调用
"ssh_add_host": lambda **kw: ssh_add_host(
    kw["name"], kw["hostname"], kw["username"], kw["password"],
    kw["allowed_prefix"], kw.get("port", 22), kw.get("description", ""),
),
"ssh_remove_host": lambda **kw: ssh_remove_host(kw["name"]),
# 远程后台任务:复用 BackgroundManager 的通知队列,与 background_run 同构
"background_ssh_exec": lambda **kw: self.bg_mgr.run_ssh(
    kw["host"], kw["command"], kw.get("timeout", 600)
),
```

`get_tools` 追加(略,JSON Schema 同构):

```python
{"name": "ssh_exec",
 "description": "在已登记的远程主机执行 shell 命令。host 必须是 ssh_list_hosts 返回的 name 之一。",
 "input_schema": {"type": "object",
   "properties": {"host": {"type": "string"}, "command": {"type": "string"},
                  "timeout": {"type": "integer"}},
   "required": ["host", "command"]}},
{"name": "ssh_read",
 "description": "读取远程主机文件;path 必须落在该主机 allowed_prefix 下。",
 "input_schema": {"type": "object",
   "properties": {"host": {"type": "string"}, "path": {"type": "string"},
                  "limit": {"type": "integer"}},
   "required": ["host", "path"]}},
{"name": "ssh_write",
 "description": "写入远程主机文件(覆盖);path 必须落在该主机 allowed_prefix 下。",
 "input_schema": {"type": "object",
   "properties": {"host": {"type": "string"}, "path": {"type": "string"},
                  "content": {"type": "string"}},
   "required": ["host", "path", "content"]}},
{"name": "ssh_upload",
 "description": "本地 → 远程主机文件传输。",
 "input_schema": {"type": "object",
   "properties": {"host": {"type": "string"},
                  "local_path": {"type": "string"}, "remote_path": {"type": "string"}},
   "required": ["host", "local_path", "remote_path"]}},
{"name": "ssh_download",
 "description": "远程主机 → 本地文件传输。",
 "input_schema": {"type": "object",
   "properties": {"host": {"type": "string"},
                  "remote_path": {"type": "string"}, "local_path": {"type": "string"}},
   "required": ["host", "remote_path", "local_path"]}},
{"name": "ssh_list_hosts",
 "description": "列出当前登记的远程主机(含运行时与预登记)。",
 "input_schema": {"type": "object", "properties": {}}},
{"name": "ssh_add_host",
 "description": "运行时注册一台密码登录的远程主机(仅内存,REPL 退出即丢)。仅在用户主动给出 IP/用户/密码时调用。密码会作为入参进入 history,请仅用于用户明确指定的内网测试场景。",
 "input_schema": {"type": "object",
   "properties": {"name": {"type": "string", "description": "host 别名,后续 ssh_exec 用这个"},
                  "hostname": {"type": "string"}, "username": {"type": "string"},
                  "password": {"type": "string"}, "allowed_prefix": {"type": "string"},
                  "port": {"type": "integer"}, "description": {"type": "string"}},
   "required": ["name", "hostname", "username", "password", "allowed_prefix"]}},
{"name": "ssh_remove_host",
 "description": "删除一台运行时注册的主机。预登记 host 无法删除。",
 "input_schema": {"type": "object",
   "properties": {"name": {"type": "string"}},
   "required": ["name"]}},
{"name": "background_ssh_exec",
 "description": "在远程主机后台执行长任务,通过 check_background 查询结果。",
 "input_schema": {"type": "object",
   "properties": {"host": {"type": "string"}, "command": {"type": "string"},
                  "timeout": {"type": "integer"}},
   "required": ["host", "command"]}},
```

### 3.5 `BackgroundManager` 扩展:`managers/background_manager.py`

加一个 `run_ssh` 方法,把远程任务也归到统一的通知队列;`MainAgent._preprocess` 第 3 步排空通知时,本地/远程任务的结果对模型是一致的:

```python
# managers/background_manager.py 内
def run_ssh(self, host: str, command: str, timeout: int = 600) -> str:
    import uuid
    tid = f"ssh-{str(uuid.uuid4())[:6]}"
    self.tasks[tid] = {"status": "running", "command": f"[{host}] {command}", "result": None}
    threading.Thread(target=self._exec_ssh, args=(tid, host, command, timeout), daemon=True).start()
    return f"Background SSH task {tid} started on {host}: {command[:60]}"

def _exec_ssh(self, tid: str, host: str, command: str, timeout: int):
    from utils.ssh_client import ssh_pool
    try:
        ok, out = ssh_pool.exec(host, command, timeout=timeout)
        self.tasks[tid].update({
            "status": "completed" if ok else "error",
            "result": (out or "(no output)")[:50000],
        })
    except Exception as e:
        self.tasks[tid].update({"status": "error", "result": f"{type(e).__name__}: {e}"})
    self.notifications.put({
        "task_id": tid, "status": self.tasks[tid]["status"],
        "result": self.tasks[tid]["result"][:500],
    })
```

### 3.6 Subagent 同步:`agents/subagent.py`

CLAUDE.md 明确"主 Agent 加重要工具时要判断是否同步给 subagent"。SSH 工具**全量同步**——Explore 模式只给只读三件套(`ssh_exec` / `ssh_read` / `ssh_list_hosts`),general-purpose 再加 `ssh_write` / `ssh_upload` / `ssh_download`。

**不同步的工具:`ssh_add_host` / `ssh_remove_host`**。理由:
- 这两个工具的入参里含明文密码,只能从"用户告诉 Lead"这条链路进入系统。
- Subagent 是 Lead 派出的短期 worker,只承担"用现有 host 干活",不该自己决定注册新 host。
- 若 Lead 派 Subagent 后又要新 host,应该 Lead 自己调 `ssh_add_host` 再二次派 Subagent。

```python
# agents/subagent.py:sub_tools 构造处
sub_tools += [
    {"name": "ssh_exec", "description": "Run command on remote host.",
     "input_schema": {"type": "object",
       "properties": {"host": {"type": "string"}, "command": {"type": "string"}},
       "required": ["host", "command"]}},
    {"name": "ssh_read", "description": "Read remote file.",
     "input_schema": {"type": "object",
       "properties": {"host": {"type": "string"}, "path": {"type": "string"}},
       "required": ["host", "path"]}},
    {"name": "ssh_list_hosts", "description": "List registered remote hosts.",
     "input_schema": {"type": "object", "properties": {}}},
]
if agent_type != "Explore":
    sub_tools += [
        {"name": "ssh_write", "description": "Write remote file.",
         "input_schema": {"type": "object",
           "properties": {"host": {"type": "string"}, "path": {"type": "string"},
                          "content": {"type": "string"}},
           "required": ["host", "path", "content"]}},
        {"name": "ssh_upload", "description": "Upload local → remote.",
         "input_schema": {"type": "object",
           "properties": {"host": {"type": "string"},
                          "local_path": {"type": "string"}, "remote_path": {"type": "string"}},
           "required": ["host", "local_path", "remote_path"]}},
        {"name": "ssh_download", "description": "Download remote → local.",
         "input_schema": {"type": "object",
           "properties": {"host": {"type": "string"},
                          "remote_path": {"type": "string"}, "local_path": {"type": "string"}},
           "required": ["host", "remote_path", "local_path"]}},
    ]
```

`sub_handlers` 字典对应追加 6 个 lambda(直接调 `tools.ssh_tools` 里的同名函数即可)。

子 agent 的 system prompt 末尾追加一句:`"You may also operate registered remote hosts via ssh_* tools. Always call ssh_list_hosts first to see which hosts exist."`——避免模型瞎猜 host 名字浪费一次失败调用。

### 3.7 Teammate 同步:`managers/teammate_manager.py`

队友默认**只开 `ssh_exec` 和 `ssh_list_hosts`**(只读),不给文件传输——理由:

- 队友是后台自动认领任务的,行为可观测性比 Lead 差;放开 `ssh_write/upload/download` 后,一个被 prompt injection 的任务可以悄悄把远端文件改了。
- 如果队友确实需要写远端,通过 `send_message` 让 Lead 来做,Lead 可以问用户。
- 同理 `ssh_add_host` / `ssh_remove_host` 也**不同步给队友**——队友不该决定新增主机,凭据只能从 Lead-用户对话进入。

`tools` 列表与硬编码 if/elif 分支照例同步加:

```python
# managers/teammate_manager.py:tools 列表
{"name": "ssh_exec", "description": "Run command on remote host (read-only intent).",
 "input_schema": {"type": "object",
   "properties": {"host": {"type": "string"}, "command": {"type": "string"}},
   "required": ["host", "command"]}},
{"name": "ssh_list_hosts", "description": "List registered remote hosts.",
 "input_schema": {"type": "object", "properties": {}}},
```

`_loop_inner` 工具分发分支追加:

```python
elif block["name"] == "ssh_exec":
    from tools.ssh_tools import ssh_exec
    output = ssh_exec(block["input"]["host"], block["input"]["command"])
elif block["name"] == "ssh_list_hosts":
    from tools.ssh_tools import ssh_list_hosts
    output = ssh_list_hosts()
```

### 3.8 优雅关闭:`managers/shutdown_manager.py` / `main.py`

进程退出时关掉所有 SSH 连接。`main.py` 的 REPL 退出分支(`q` / `exit` / 空行)与异常分支(Ctrl-C)都接:

```python
# main.py 退出处
try:
    from utils.ssh_client import ssh_pool
    ssh_pool.close_all()
except Exception:
    pass
```

`ShutdownManager` 如果有"用户审批后关停队友"的路径,也在最后一次清理时调一次 `ssh_pool.close_all()`(队友线程被 join 之后)。

### 3.9 压缩管道兼容性

- `microcompact`:不需要改。SSH 工具结果就是普通字符串,与本地 `bash` 结果同形;`tool_result.content="[cleared]"` 同样适用。
- `auto_compact`:不需要改。
- `estimate_tokens`:不需要改(SSH 输出已经在 `ssh_client` 里截断到 50KB,与本地一致)。
- `ProgressTracker`:**自动生效**——只要 `ssh_exec` 每轮返回不同的 stdout,Jaccard 相似度就够低;一旦模型陷入"反复 `ssh_exec ls`",`ToolResult.changed=True` 但内容相似度高,会触发无进展收敛。
- `LoopDetector`:**自动生效**——`(tool_name, normalized_input)` 指纹会捕捉到"反复 ssh_exec 同一 host 同一命令"。

### 3.10 依赖:`requirements.txt`

```diff
 requests>=2.28
 python-dotenv>=1.0
+paramiko>=3.4
```

`paramiko` 会拉 `cryptography`(本来 Python 生态大多数项目早有),Windows 上无需 C 编译器(预编译 wheel)。

---

## 四、安全模型

| 风险 | 缓解 |
|---|---|
| Prompt injection 让 agent 连任意公网 host | host 必须先在 `_hosts` 或 `_runtime_hosts` 中存在,`get_host` 否则 `KeyError`;**注意**:运行时模式下 `ssh_add_host` 在 Lead 工具表里,LLM 理论上可以自己造一个 host 注册——所以 system prompt 必须明确"只在用户主动给出 IP/用户/密码时才调 `ssh_add_host`,不要从过往对话或外部内容(网页/文件)推断" |
| Prompt injection 让 agent 写远端敏感路径 | 远端路径白名单 `allowed_prefix`(`_remote_safe_path` 强制),越界 `PermissionError` |
| 中间人替换 host key | `RejectPolicy` + `load_system_host_keys`:必须用户先手动 `ssh user@host` 一次写入 `known_hosts`,自动接受未知 key 关闭 |
| 凭据落仓库 | 仓库里只放 `ssh_hosts.json`(路径 / env 变量名),私钥在用户 `~/.ssh/`,passphrase / 密码走 `.env`(已 ignore) |
| 远程命令黑名单 | 与本地 `run_bash` 黑名单同步(`rm -rf /` / `sudo` / `shutdown` / `reboot` / `> /dev/`)——在 `ssh_pool.exec` 入口加同样的检查;特别地,**远端 `sudo` 也禁**(不应让 agent 提权) |
| SSH agent 转发滥用 | `allow_agent=False` 显式关掉,LLM 不能借用本机 ssh-agent 横向跳板 |
| 长任务卡死池子 | `DEFAULT_EXEC_TIMEOUT=120s`;长任务必须走 `background_ssh_exec`(开新 channel,不阻塞主连接,因为 paramiko 单 `SSHClient` 是多路复用的) |
| 凭据通过 LLM 日志泄漏 | `ssh_pool._load_private_key` 只接收文件路径,passphrase / 密码从 env 读;**所有工具入参都不接受私钥本体、密码、passphrase 原文** |
| 凭据形态混淆 | `_load_host_configs` 强制 `key_path` 与 `password_env` 互斥,杜绝"key 失败时静默降级到密码"——任何一台 host 凭据形态在配置阶段就已确定 |
| **密码模式特有**:凭据泄露面比 key 大 | 限制使用场景:仅推荐用于**封闭内网 / 老设备**;`ssh_hosts.json` 的 `description` 字段建议显式写"密码登录",方便后续审计;若可能,优先把这类 host 改造为 key 登录 |
| **密码模式特有**:`look_for_keys=False` 防绕路 | 显式禁用 pubkey 协商,paramiko 不会因为本机 `~/.ssh/` 有同名 key 而绕开密码字段 |
| **运行时模式特有**:`ssh_add_host` 入参的密码会进 LLM history、agent.log、`.transcripts/*.jsonl` | 用户已在选型阶段知情接受;**缓解措施**:① `LLMClient` 写日志前对 `password` 字段做正则脱敏(参考下方 6.11);② `ssh_add_host` 的 `tool_result` 不回显密码(避免下一轮 LLM 再次输出);③ 进程退出立刻丢弃,不写文件 |
| **运行时模式特有**:工具范围管控 | `ssh_add_host` / `ssh_remove_host` **只在 Lead 工具表**——Subagent 和 Teammate 都没有,凭据只能从"用户与 Lead 的对话"这一条链路进入 |
| **运行时模式特有**:JSON 守门 | `_load_host_configs` 检测到 `password_plain` 字段直接 `ValueError`,防止有人手抄 ssh_add_host 入参写进 JSON 落盘 |

---

## 五、实施步骤(建议合并顺序)

| 步骤 | 文件 | 大致改动 | 验收 |
|------|------|---------|------|
| 1 | `requirements.txt` | 加 `paramiko>=3.4` | `pip install -r requirements.txt` 无报错 |
| 2 | `utils/ssh_client.py` | 新建(~300 行,含 `_runtime_hosts` 与 `register_runtime_host`) | 单测 / 手测:`ssh_pool.exec("localhost", "echo hi")` 返回 `(True, "hi\n")`;`register_runtime_host` 后能立刻 exec |
| 3 | `tools/ssh_tools.py` | 新建(~120 行),全部回 `ToolResult`,含 `ssh_add_host` / `ssh_remove_host` | `ssh_exec("localhost", "ls /tmp")` 返回 `ToolResult(content="...", error=False)` |
| 4 | `tools/tool_dispatcher.py` | `get_handler` + `get_tools` 各加 9 项(7 操作 + 2 注册) | REPL 里跑"连 X 用户 Y 密码 Z 看 /tmp",模型调 `ssh_add_host` → `ssh_exec` |
| 5 | `managers/background_manager.py` | 加 `run_ssh` + `_exec_ssh` | `background_ssh_exec("build-box", "sleep 5 && date")` 立即返回 tid,5s 后 `<background-results>` 注入 |
| 6 | `agents/subagent.py` | sub_tools / sub_handlers 加 6 项(**不含 add/remove**);system prompt 末尾加一句 | `task(prompt="在 staging 上找 nginx 配置", agent_type="Explore")` 子 agent 能完成 |
| 7 | `managers/teammate_manager.py` | tools / 分发分支各加 2 项(只读子集,**不含 add/remove**) | `spawn_teammate name=ops` + 给 ops 派远程巡检任务能跑通 |
| 8 | `main.py` + `managers/shutdown_manager.py` | 退出钩子调 `ssh_pool.close_all()`(内部已清运行时 host) | `python main.py` → 触发一次 SSH → `q`;运行时 host 不残留,远端 `who` 不留长时间僵尸 |
| 9 | `core/llm_client.py` | 写日志层加 `password` 字段正则脱敏 | `agent.log` 里 `ssh_add_host` 调用看到 `"password": "***"` |
| 10 | 文档 | 新建 `.team/ssh_hosts.example.json`;更新 `README.md` 增加 SSH 章节(含"运行时模式 vs 预登记"对比);更新 `CLAUDE.md` "新加工具的约定"小节里把 `ssh_*` 当作正例提到 | 新人按 README 配 host 能跑通 |
| 11 | 冒烟 | `python main.py` → `>>> 连 X 用户 Y 密码 Z 看 /tmp` → 退出 → 重启 → 主机不存在 | 全链路无报错,运行时 host 退出即丢 |

---

## 六、风险与待确认事项

1. **`paramiko` 在 Windows 安装的 OpenSSL 依赖**:近期 `cryptography` 版本切到 Rust 后端,有少数老 Windows 环境 wheel 缺失。建议在 README 标注 Python 3.9+ 且 pip ≥ 21,无效时手动 `pip install cryptography --prefer-binary`。
2. **`known_hosts` 路径在 Windows**:`load_system_host_keys()` 默认读 `~/.ssh/known_hosts`;OpenSSH for Windows 默认位置一致,但部分组策略机器需要手动建。文档要写一句"首次配 host 后,先在终端 `ssh user@host` 一次,接受指纹"。
3. **远端 shell 长输出回填**:`stdout.read()` 在 paramiko 里是阻塞读直到 EOF;如果远端命令一直滚日志(`tail -f`)会卡死。对策:`ssh_pool.exec` 内部加 `channel.settimeout(timeout)`,超时抛 `socket.timeout` 走 except 路径,**与本地 120s 超时语义对齐**。
4. **path_utils.safe_path 与 `_remote_safe_path` 双套实现**:这是有意为之——本地走 `Path` API,远端走 `PurePosixPath` 字符串归一;不要复用 `Path`,否则 Windows 上跑会插入反斜杠。但要在两边各加一组单测,避免哪天有人重构其中一处忘了另一处。
5. **多 host 并发认领任务**:Teammate 是多线程的,如果两个队友同时去同一 host 上跑 `ssh_exec`,`paramiko.SSHClient` 内部 channel 是多路复用的,理论上 OK;但 `SFTPClient` 单实例并发 `stat/open` 在某些 sshd 实现下会乱序。**对策**:`SSHPool` 的 `_get_conn` 已经加锁,但只保护"取/建连接",并发**执行**未加锁。如果出问题再升级为"每 host 一把 RLock"。
6. **远端 `sudo` 黑名单 vs. 现实需求**:很多运维任务确实需要 `sudo systemctl restart`。建议第一版严守"禁 sudo",有真实需求再开"sudo 白名单"(只允许列在 `ssh_hosts.json::sudo_allowed_commands` 里的几条)。
7. **日志脱敏**:`ssh_exec` 的入参 command 会进 `agent.log`;如果运维命令里含密码(`mysql -uroot -p<pwd>`),日志就泄了。建议在 `LLMClient` 写日志那一层加一个正则脱敏规则(`-p\S+` 之类),或者明确告诉用户**禁止把密码写进 `ssh_exec` 入参**,改用远端配置文件。
8. **`MAX_TOOL_CALLS_PER_ROUND=5` 对 SSH 偏紧**:一次"部署"动作可能 ssh_exec 跑 3-4 条,再加 1 个 read_file 就到上限;可以在文档里建议把这个值调到 8,但不强改默认。
9. **密码模式适用边界**:密码模式只用于"无法配 key 的 legacy 设备 / 临时接管的老机器"。一旦目标 host 有改造空间,优先迁到 key 模式——密码进内存、进日志、进堆栈的面比 passphrase 大得多(passphrase 至少要配合那个私钥文件才能用)。建议团队定期审计 `ssh_hosts.json` 里 `password_env` 的条目,推动改造。
10. **密码模式与堡垒机 / MFA**:若目标 host 走双因子(动态码 + 密码),`password=` 参数只能填静态部分,动态码需要走交互式输入——paramiko 的 `interactive` 认证流程比较复杂,本方案不覆盖。这类场景建议改用"先在堡垒机侧用 key 登录,登录后再调内网命令"的方案。
11. **运行时密码模式的脱敏建议**(已在选型阶段被接受,这里列具体缓解动作):
    - **`LLMClient` 日志层脱敏**:写入 `agent.log` 之前对入参做 `re.sub(r'("password"\s*:\s*)"[^"]+"', r'\1"***"', s)`;否则日志里的每次 `ssh_add_host` 调用都明文留底。
    - **transcript 落盘脱敏**:同上,`auto_compact` 之前的 transcript 快照里也含密码。建议在 `_dump_transcript` 之前应用同样的正则。
    - **告知用户**:首次启动时打印一行 banner——"提示:运行时 ssh_add_host 的密码会进入 history,如不接受请改用 password_env 预登记"。
    - **退出清理**:`close_all()` 已经在最后一步 `self._runtime_hosts.clear()`——`main.py` 退出钩子调一次即可,运行时 host 与连接一起释放。
12. **运行时 host 的 LLM 误用**:模型有可能把对话中出现过的"看起来像密码的字符串"再次塞给 `ssh_add_host`。系统 prompt 里要明确"`ssh_add_host` 只在用户主动告知 IP+用户+密码时调用,不要从过往对话推断"。
13. **运行时 host 与 Teammate 隔离**:`SSHPool` 是进程级单例,Teammate 线程能看到 `_runtime_hosts`——这其实**是想要的**(Lead 注册之后,派 Teammate 干活时能用)。但要意识到:Teammate 工具表里没有 `ssh_add_host`,所以它只能"使用",不能"注册",方向是对的。

---

## 七、用户使用流程示例

```text
# 第一次配置
$ vim .team/ssh_hosts.json     # 抄 3.2 范例填主机(每台二选一:key 或 password)
$ vim .env                     # 加 SSH_PASSPHRASE_STAGING=xxx;若有密码主机再加 SSH_PASSWORD_LEGACY=xxx
$ ssh deploy@10.0.1.42 exit    # 让 OpenSSH 把 host key 写进 known_hosts(必做,key/密码模式都需要)
$ python main.py

# 列出可用主机
>> 我有哪些远程主机可以用?
[模型调 ssh_list_hosts]
staging | deploy@10.0.1.42:22 | /home/deploy/app/  | 预发布环境...
build-box | ci@build.internal:2222 | /srv/ci-workspace/ | 构建机...

# 远程执行
>> 看下 staging 上 app 目录的磁盘占用
[模型调 ssh_exec(host="staging", command="du -sh /home/deploy/app/*")]
[DONE]
1.2G  /home/deploy/app/dist
340M  /home/deploy/app/logs
...

# 远程 → 本地拉日志分析
>> 把 staging 最新的 error.log 拉下来,grep 一下昨天的 5xx
[模型调:
  1. ssh_exec(host="staging", command="ls -t /home/deploy/app/logs/error.log*")
  2. ssh_download(host="staging", remote_path="/home/deploy/app/logs/error.log",
                  local_path="downloads/error.log")
  3. bash(command="grep '2026-05-20' downloads/error.log | grep ' 5[0-9][0-9] '")
]

# 长任务后台
>> 在 build-box 上跑一次完整构建,完了告诉我
[模型调 background_ssh_exec(host="build-box", command="cd /srv/ci-workspace && make all")]
Background SSH task ssh-a3f2c1 started on build-box: cd /srv/ci-workspace && make all
... (用户继续别的对话,5 分钟后某轮预处理时 <background-results> 注入)
<background-results>
- ssh-a3f2c1: completed
  Build succeeded in 4m23s. Artifacts in /srv/ci-workspace/dist/
</background-results>

# 运行时直接告诉 agent 新主机(零配置,密码进 history)
>> 连一下 192.168.10.5 用户 admin 密码 P@ssw0rd! 只让操作 /tmp,看下磁盘
[模型调:
  1. ssh_add_host(name="tmp-1", hostname="192.168.10.5", username="admin",
                  password="P@ssw0rd!", allowed_prefix="/tmp",
                  description="临时,用户运行时给的")
     → 已注册运行时主机 'tmp-1' (admin@192.168.10.5:22, prefix=/tmp)
  2. ssh_exec(host="tmp-1", command="df -h /tmp")
]
... (REPL 退出后 tmp-1 自动丢失,下次启动要重新告诉)
```

---

## 八、附:与图片支持方案的对照

| 维度 | 图片方案 | SSH 方案 |
|---|---|---|
| 新模块 | `utils/image_utils.py` | `utils/ssh_client.py`(更大,带池子和锁) |
| 工具数 | 1(`read_image`) | 9(`ssh_exec/read/write/upload/download/list_hosts/add_host/remove_host` + `background_ssh_exec`) |
| 改 `ToolResult` 协议 | 是(支持 list content) | 否(纯字符串够用) |
| 改压缩管道 | 是(图占 token 巨大) | 否(输出已截到 50KB) |
| 改 REPL 输入 | 是(`/img` 语法) | 否(LLM 自己调工具即可) |
| 子 agent 同步 | 是 | 是(分 Explore/general 两档) |
| 队友同步 | 否(明确 v2 再说) | **是,只读子集** |
| 新配置 | 无 | `.team/ssh_hosts.json`(预登记) + `.env` 追加 passphrase / 密码;**或**运行时 `ssh_add_host` 注册(零配置) |
| 新依赖 | 无 | `paramiko>=3.4` |
| 主要风险点 | token 预算、网关兼容性 | 凭据安全、host 白名单、远端路径越界 |

---

## 九、不在本方案范围(可作为 v2)

- **交互式 shell**:`ssh_exec` 是一发一收,不支持 `vim` / `top` / `sudo -i` 这类需要 PTY 的命令。如要做,加 `ssh_shell_open / send / close` 三件套并独立管理 PTY 生命周期。
- **端口转发(LocalForward / RemoteForward)**:很少用,且容易被滥用打洞,暂不开。
- **多跳跳板机(ProxyJump)**:`paramiko.SSHClient` 不原生支持,需要手动套 `Transport`;真有需要时单独写。
- **`ssh_*` 工具的审计旁路**:把所有 ssh 调用单独落到 `.transcripts/ssh_audit.jsonl`,方便事后回溯哪个 agent 在哪台主机跑过什么。建议第二版加,与 `agent.log` 区分。
- **凭据托管到操作系统 keyring**:`keyring` 库可以把 passphrase 移出 `.env`,适合多人共享开发机的场景;第一版不引,理由是又加一个依赖、Windows/macOS/Linux 行为差异不小。
