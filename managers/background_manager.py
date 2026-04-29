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
