"""
后台任务管理器 - s08: 异步命令执行
"""
import threading
import time
from queue import Queue
from typing import Dict, Any, Optional

from config.settings import WORKDIR
from utils.encoding_utils import safe_subprocess_run


class BackgroundManager:
    """
    后台任务管理器 - 异步命令执行
    
    特性:
        - 异步执行长时间命令
        - 通知队列
        - 状态跟踪
    """
    
    def __init__(self):
        """初始化后台任务管理器"""
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.notifications: Queue = Queue()
    
    def run(self, command: str, timeout: int = 120) -> str:
        """
        在后台线程中运行命令
        
        Args:
            command: 要执行的 shell 命令
            timeout: 超时时间（秒）
            
        Returns:
            任务 ID
        """
        import uuid
        tid = str(uuid.uuid4())[:8]
        
        self.tasks[tid] = {"status": "running", "command": command, "result": None}
        
        threading.Thread(
            target=self._exec,
            args=(tid, command, timeout),
            daemon=True
        ).start()
        
        return f"Background task {tid} started: {command[:80]}"
    
    def _exec(self, tid: str, command: str, timeout: int):
        """后台线程执行函数"""
        try:
            r = safe_subprocess_run(
                command,
                cwd=WORKDIR,
                timeout=timeout
            )
            output = (r.stdout + r.stderr).strip()[:50000]
            
            self.tasks[tid].update({
                "status": "completed",
                "result": output or "(no output)"
            })
        except Exception as e:
            self.tasks[tid].update({
                "status": "error",
                "result": str(e)
            })
        
        self.notifications.put({
            "task_id": tid,
            "status": self.tasks[tid]["status"],
            "result": self.tasks[tid]["result"][:500]
        })
    
    def check(self, tid: Optional[str] = None) -> str:
        """
        检查后台任务状态
        
        Args:
            tid: 任务 ID（可选）
            
        Returns:
            任务状态字符串
        """
        if tid:
            t = self.tasks.get(tid)
            return f"[{t['status']}] {t.get('result') or '(running)'}" if t else f"Unknown: {tid}"
        
        return "\n".join(
            f"{k}: [{v['status']}] {v['command'][:60]}"
            for k, v in self.tasks.items()
        ) or "No bg tasks."
    
    def drain(self) -> list:
        """排空通知队列"""
        notifs = []
        while not self.notifications.empty():
            notifs.append(self.notifications.get_nowait())
        return notifs

    # —— SSH 远程长任务 ——

    def run_ssh(self, host: str, command: str, timeout: int = 600) -> str:
        """在远程主机后台执行长任务,结果通过同一个 notifications 队列回填。

        与本地 run() 同构:主循环 _preprocess 排空通知时不区分本地/远端,模型看到的格式一致。
        """
        import uuid
        tid = f"ssh-{str(uuid.uuid4())[:6]}"
        self.tasks[tid] = {
            "status": "running",
            "command": f"[{host}] {command}",
            "result": None,
        }
        threading.Thread(
            target=self._exec_ssh,
            args=(tid, host, command, timeout),
            daemon=True,
        ).start()
        return f"Background SSH task {tid} started on {host}: {command[:60]}"

    def _exec_ssh(self, tid: str, host: str, command: str, timeout: int):
        """SSH 后台线程执行函数。"""
        # 局部 import,避免 background_manager 模块加载就拉 paramiko
        from utils.ssh_client import ssh_pool
        try:
            ok, out = ssh_pool.exec(host, command, timeout=timeout)
            self.tasks[tid].update({
                "status": "completed" if ok else "error",
                "result": (out or "(no output)")[:50000],
            })
        except Exception as e:
            self.tasks[tid].update({
                "status": "error",
                "result": f"{type(e).__name__}: {e}",
            })
        self.notifications.put({
            "task_id": tid,
            "status": self.tasks[tid]["status"],
            "result": self.tasks[tid]["result"][:500],
        })
