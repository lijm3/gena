"""
任务管理器 - s07: 持久化任务管理
"""
import json
from pathlib import Path
from typing import List, Optional

from config.settings import TASKS_DIR


class TaskManager:
    """
    持久任务管理器 - Agent 的"项目管理系统"
    
    特性:
        - 自动递增 ID
        - 支持任务依赖（blockedBy）
        - 任务完成时自动解锁下游任务
        - 支持多 Agent 认领
    """
    
    def __init__(self):
        """初始化任务管理器"""
        TASKS_DIR.mkdir(exist_ok=True)
    
    def _next_id(self) -> int:
        """生成下一个任务 ID"""
        ids = [int(f.stem.split("_")[1]) for f in TASKS_DIR.glob("task_*.json")]
        return max(ids, default=0) + 1
    
    def _load(self, tid: int) -> dict:
        """加载任务数据"""
        p = TASKS_DIR / f"task_{tid}.json"
        if not p.exists():
            raise ValueError(f"Task {tid} not found")
        return json.loads(p.read_text())
    
    def _save(self, task: dict):
        """保存任务数据"""
        (TASKS_DIR / f"task_{task['id']}.json").write_text(json.dumps(task, indent=2))
    
    def create(self, subject: str, description: str = "") -> str:
        """创建新任务"""
        task = {
            "id": self._next_id(),
            "subject": subject,
            "description": description,
            "status": "pending",
            "owner": None,
            "blockedBy": []
        }
        self._save(task)
        return json.dumps(task, indent=2)
    
    def get(self, tid: int) -> str:
        """获取任务详情"""
        return json.dumps(self._load(tid), indent=2)
    
    def update(
        self,
        tid: int,
        status: Optional[str] = None,
        add_blocked_by: Optional[List[int]] = None,
        remove_blocked_by: Optional[List[int]] = None
    ) -> str:
        """更新任务"""
        task = self._load(tid)
        
        if status:
            task["status"] = status
            
            # 任务完成：解锁下游任务
            if status == "completed":
                for f in TASKS_DIR.glob("task_*.json"):
                    t = json.loads(f.read_text())
                    if tid in t.get("blockedBy", []):
                        t["blockedBy"].remove(tid)
                        self._save(t)
            
            # 删除任务
            if status == "deleted":
                (TASKS_DIR / f"task_{tid}.json").unlink(missing_ok=True)
                return f"Task {tid} deleted"
        
        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        
        if remove_blocked_by:
            task["blockedBy"] = [x for x in task["blockedBy"] if x not in remove_blocked_by]
        
        self._save(task)
        return json.dumps(task, indent=2)
    
    def list_all(self) -> str:
        """列出所有任务"""
        tasks = [json.loads(f.read_text()) for f in sorted(TASKS_DIR.glob("task_*.json"))]
        if not tasks:
            return "No tasks."
        
        lines = []
        for t in tasks:
            m = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            owner = f" @{t['owner']}" if t.get("owner") else ""
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{m} #{t['id']}: {t['subject']}{owner}{blocked}")
        
        return "\n".join(lines)
    
    def claim(self, tid: int, owner: str) -> str:
        """认领任务"""
        task = self._load(tid)
        task["owner"] = owner
        task["status"] = "in_progress"
        self._save(task)
        return f"Claimed task #{tid} for {owner}"
