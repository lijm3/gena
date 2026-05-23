"""
SSH 连接池与远程 IO

设计:
  - SSHPool 进程级单例,按 host 名缓存 (SSHClient, SFTPClient) 二元组
  - 维护两组 host:
      _hosts:        预登记,来自 .team/ssh_hosts.json(进程重启重新加载)
      _runtime_hosts: 运行时通过 ssh_add_host 注册,只活在内存,REPL 退出即丢
    查找顺序:运行时 > 预登记;同名时运行时覆盖
  - 所有公开方法返回 (ok: bool, output: str),由上层 ssh_tools 包成 ToolResult
  - 远程命令默认 UTF-8 → latin-1 回退解码,与 utils/encoding_utils 行为一致
  - 不暴露 SSHClient/SFTPClient 给外部,避免 paramiko 类型泄漏到上层

凭据三档(_load_host_configs / register_runtime_host 强校验):
  ① key_path + passphrase_env(预登记,推荐)
  ② password_env(预登记密码,legacy 兜底)
  ③ password_plain(运行时,LLM 直接给的明文,仅活在内存)
"""
import json
import os
import threading
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Dict, Optional, Tuple

import paramiko

from config.settings import TEAM_DIR


SSH_HOSTS_PATH = TEAM_DIR / "ssh_hosts.json"
KNOWN_HOSTS_PATH = TEAM_DIR / "known_hosts"   # 项目级 known_hosts,等价 OpenSSH accept-new
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
    key_path: Optional[str] = None          # ① 私钥文件路径(本机)
    passphrase_env: Optional[str] = None    # ① 私钥 passphrase 的环境变量名
    password_env: Optional[str] = None      # ② 密码的环境变量名(预登记,legacy)
    password_plain: Optional[str] = None    # ③ 运行时明文密码(仅 register_runtime_host 写)
    # —— 其他 ——
    allowed_prefix: str = ""                # 远端路径白名单根
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
    - 不复用本地 Path 的原因:Windows 上 Path 会插反斜杠
    """
    p = PurePosixPath(path)
    if not p.is_absolute():
        p = PurePosixPath(host.allowed_prefix) / p
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


# 远程命令黑名单(与本地 run_bash 同步,且额外禁 sudo)
_REMOTE_DANGEROUS = ("rm -rf /", "sudo", "shutdown", "reboot", "> /dev/")


def _check_remote_command(command: str) -> Optional[str]:
    """命中黑名单返回错误说明,否则 None。"""
    for kw in _REMOTE_DANGEROUS:
        if kw in command:
            return f"远程命令被阻止(命中黑名单: {kw!r})"
    return None


class SSHPool:
    """进程级 SSH 连接池。线程安全。

    维护两组 host:
      - self._hosts:        预登记,来自 ssh_hosts.json,进程重启会重新加载
      - self._runtime_hosts: 运行时通过 ssh_add_host 注册,只活在内存
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
        # 项目级 known_hosts:不污染用户家目录,且仓库内多人协作时各自维护一份。
        # 首次连接 AutoAdd 写盘(等价 OpenSSH StrictHostKeyChecking=accept-new);
        # 已记录的主机若 key 变了,paramiko 内部仍会 BadHostKeyException——MITM 防护没丢。
        KNOWN_HOSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not KNOWN_HOSTS_PATH.exists():
            KNOWN_HOSTS_PATH.touch()
        cli.load_host_keys(str(KNOWN_HOSTS_PATH))
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())

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
        # AutoAddPolicy 只往内存 HostKeys 写,不自动落盘——必须显式 save。
        # 否则进程一退新指纹就丢,下次还得 AutoAdd 一遍(等于失去白名单语义)。
        try:
            cli.save_host_keys(str(KNOWN_HOSTS_PATH))
        except Exception:
            pass  # 落盘失败不阻断本次连接;最坏情况下次启动重新 AutoAdd
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
                # 存活检查:SFTP stat allowed_prefix(失败说明连接断了,重连)
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
        blocked = _check_remote_command(command)
        if blocked:
            return False, blocked
        cli, _, _ = self._get_conn(name)
        # get_pty=False:不分配伪终端;tty 会把 stdout/stderr 混着回来还带控制字符。
        _stdin, stdout, stderr = cli.exec_command(command, timeout=timeout, get_pty=False)
        # channel 超时,避免远端命令一直滚日志(tail -f)卡死整个池子
        stdout.channel.settimeout(timeout)
        try:
            out_b = stdout.read()
            err_b = stderr.read()
            code = stdout.channel.recv_exit_status()
        except Exception as e:
            return False, f"远程命令超时或读流失败: {type(e).__name__}: {e}"
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
        # SFTP 没有原生 mkdir -p,自己来
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
