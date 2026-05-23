"""
任务管理器 - s07: 持久化任务管理（SQLite 后端）

历史：原版用 .tasks/task_<id>.json 散文件存储，在多 teammate 线程并发时有 race：
  - _next_id 用 glob 找最大 ID，两线程同时 create 会撞 ID
  - claim 是 read→modify→write，两线程同时认领同一任务后写覆盖前写
  - _save 用 write_text 非原子，崩溃会留半截 JSON
本版改用单文件 SQLite，AUTOINCREMENT 解决 ID race，UPDATE...WHERE 解决 claim race，
事务保证写原子性。
"""
import json
import sqlite3
import threading
from typing import List, Optional

from config.settings import DB_PATH


class TaskManager:
    """
    持久任务管理器 - Agent 的"项目管理系统"

    特性:
        - SQLite 持久化，AUTOINCREMENT ID 不复用
        - 支持任务依赖（blockedBy）
        - 任务完成时自动解锁下游任务
        - 原子认领（try_claim 用 UPDATE...WHERE 抢占）
    """

    def __init__(self):
        """初始化任务管理器"""
        # check_same_thread=False:允许跨线程使用同一连接；用 RLock 串行化保证安全。
        # WAL 模式让读写不互相阻塞，对队友并发场景友好。
        self._conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject     TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status      TEXT NOT NULL DEFAULT 'pending',
                    owner       TEXT,
                    blocked_by  TEXT NOT NULL DEFAULT '[]'
                )
            """)

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        return {
            "id": row[0],
            "subject": row[1],
            "description": row[2],
            "status": row[3],
            "owner": row[4],
            "blockedBy": json.loads(row[5]) if row[5] else [],
        }

    def _load(self, tid: int) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, subject, description, status, owner, blocked_by FROM tasks WHERE id=?",
                (tid,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Task {tid} not found")
        return self._row_to_dict(row)

    def create(self, subject: str, description: str = "") -> str:
        """创建新任务"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tasks (subject, description, status, owner, blocked_by) "
                "VALUES (?, ?, 'pending', NULL, '[]')",
                (subject, description),
            )
            tid = cur.lastrowid
        return json.dumps(self._load(tid), indent=2)

    def get(self, tid: int) -> str:
        """获取任务详情"""
        return json.dumps(self._load(tid), indent=2)

    def update(
        self,
        tid: int,
        status: Optional[str] = None,
        add_blocked_by: Optional[List[int]] = None,
        remove_blocked_by: Optional[List[int]] = None,
    ) -> str:
        """更新任务"""
        with self._lock:
            task = self._load(tid)

            if status:
                # status=deleted：直接删行
                if status == "deleted":
                    self._conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
                    return f"Task {tid} deleted"

                self._conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))

                # 任务完成：解锁所有下游
                if status == "completed":
                    rows = self._conn.execute(
                        "SELECT id, blocked_by FROM tasks WHERE blocked_by LIKE ?",
                        (f"%{tid}%",),  # 粗筛，后面 JSON 解析精确过滤
                    ).fetchall()
                    for r_id, r_blocked in rows:
                        bl = json.loads(r_blocked) if r_blocked else []
                        if tid in bl:
                            bl.remove(tid)
                            self._conn.execute(
                                "UPDATE tasks SET blocked_by=? WHERE id=?",
                                (json.dumps(bl), r_id),
                            )

            if add_blocked_by:
                cur_bl = set(task["blockedBy"])
                cur_bl.update(add_blocked_by)
                self._conn.execute(
                    "UPDATE tasks SET blocked_by=? WHERE id=?",
                    (json.dumps(sorted(cur_bl)), tid),
                )

            if remove_blocked_by:
                cur_bl = [x for x in task["blockedBy"] if x not in remove_blocked_by]
                self._conn.execute(
                    "UPDATE tasks SET blocked_by=? WHERE id=?",
                    (json.dumps(cur_bl), tid),
                )

        return json.dumps(self._load(tid), indent=2)

    def list_all(self) -> str:
        """列出所有任务（给 LLM 用的渲染版本）"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, subject, description, status, owner, blocked_by FROM tasks ORDER BY id"
            ).fetchall()

        if not rows:
            return "No tasks."

        lines = []
        for row in rows:
            t = self._row_to_dict(row)
            m = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            owner = f" @{t['owner']}" if t.get("owner") else ""
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{m} #{t['id']}: {t['subject']}{owner}{blocked}")

        return "\n".join(lines)

    def claim(self, tid: int, owner: str) -> str:
        """
        认领任务（非抢占式：直接覆盖 owner）。
        当前 ToolDispatcher 注册的 claim_task 工具走这条路径，保留旧行为。
        多线程"竞争性"认领请用 try_claim。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET owner=?, status='in_progress' WHERE id=?",
                (owner, tid),
            )
            if cur.rowcount == 0:
                return f"Error: Task {tid} not found"
        return f"Claimed task #{tid} for {owner}"

    # === 给 teammate 用的并发安全接口 ===

    def find_claimable(self) -> List[dict]:
        """
        查找所有可被自动认领的任务：pending && owner is NULL && blocked_by == []。
        返回 dict 列表（按 ID 升序），供 teammate 空闲阶段挑选。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, subject, description, status, owner, blocked_by FROM tasks "
                "WHERE status='pending' AND owner IS NULL AND blocked_by='[]' "
                "ORDER BY id"
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def try_claim(self, tid: int, owner: str) -> bool:
        """
        原子认领：只有任务当前还是 pending && owner 为空时才成功。
        两个线程同时 try_claim 同一任务，只有一个返回 True。

        Returns:
            True 表示认领成功；False 表示已被别人抢走或任务状态变化。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET owner=?, status='in_progress' "
                "WHERE id=? AND owner IS NULL AND status='pending'",
                (owner, tid),
            )
            return cur.rowcount == 1
