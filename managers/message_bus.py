"""
消息总线 - s09: Agent 间通信（SQLite 后端）

历史：原版用 .team/inbox/<name>.jsonl 散文件，read_inbox 的"读后清空"是
  msgs = read_text().splitlines()
  write_text("")
两步之间到达的 send 会被直接清掉，**消息丢失**。
本版改成 SQLite 表 + 单事务 SELECT...DELETE 原子化"取走+删除"。

接口签名（send / read_inbox / broadcast）和旧版完全兼容。
"""
import json
import sqlite3
import threading
import time
from typing import List, Dict, Any, Optional

from config.settings import DB_PATH


class MessageBus:
    """
    消息总线 - Agent 间通信

    特性:
        - 异步通信（写入即返回，接收方主动读）
        - SQLite 持久化
        - 原子"读后清空"：SELECT + DELETE 在同一事务，期间到达的 send 不会丢
    """

    def __init__(self):
        """初始化消息总线"""
        self._conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    recipient TEXT NOT NULL,
                    sender    TEXT NOT NULL,
                    msg_type  TEXT NOT NULL DEFAULT 'message',
                    content   TEXT NOT NULL,
                    extra     TEXT NOT NULL DEFAULT '{}',
                    ts        REAL NOT NULL
                )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient, id)"
            )

    def send(
        self,
        sender: str,
        to: str,
        content: str,
        msg_type: str = "message",
        extra: Optional[Dict] = None,
    ) -> str:
        """
        发送消息

        Args:
            sender: 发送者名称
            to: 接收者名称
            content: 消息内容
            msg_type: 消息类型
            extra: 额外字段（不能覆盖核心字段 from/type/content）

        Returns:
            操作结果消息
        """
        extra_json = json.dumps(extra or {}, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (recipient, sender, msg_type, content, extra, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (to, sender, msg_type, content, extra_json, time.time()),
            )
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> List[Dict[str, Any]]:
        """
        读取并清空收件箱（原子事务）

        Args:
            name: Agent 名称

        Returns:
            消息列表。每条结构与旧版一致：
            {type, from, content, timestamp, ...extra}
        """
        with self._lock:
            # SELECT + DELETE 在 RLock 内串行化；同进程其他线程的 send 必然在前或后，
            # 不会落入两个 SQL 之间。SQLite 单连接也保证了事务隔离。
            rows = self._conn.execute(
                "SELECT id, sender, msg_type, content, extra, ts FROM messages "
                "WHERE recipient=? ORDER BY id",
                (name,),
            ).fetchall()

            if not rows:
                return []

            ids = [r[0] for r in rows]
            placeholders = ",".join("?" * len(ids))
            self._conn.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", ids)

        msgs = []
        for _, sender, msg_type, content, extra_json, ts in rows:
            try:
                extra = json.loads(extra_json) if extra_json else {}
            except json.JSONDecodeError:
                extra = {}
            msg = {
                "type": msg_type,
                "from": sender,
                "content": content,
                "timestamp": ts,
                # extra 在 send 时单独存储，这里铺平，但核心字段优先级最高
            }
            for k, v in extra.items():
                if k not in ("type", "from", "content", "timestamp"):
                    msg[k] = v
            msgs.append(msg)
        return msgs

    def broadcast(self, sender: str, content: str, names: List[str]) -> str:
        """
        广播消息

        Args:
            sender: 发送者名称
            content: 消息内容
            names: 接收者名称列表

        Returns:
            操作结果消息
        """
        count = 0
        for n in names:
            if n != sender:
                self.send(sender, n, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"
