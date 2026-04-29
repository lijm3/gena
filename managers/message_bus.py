"""
消息总线 - s09: Agent 间通信
"""
import json
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

from config.settings import INBOX_DIR, VALID_MSG_TYPES


class MessageBus:
    """
    消息总线 - Agent 间通信
    
    特性:
        - 异步通信
        - 持久化
        - 读后清空
    """
    
    def __init__(self):
        """初始化消息总线"""
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
    
    def send(
        self,
        sender: str,
        to: str,
        content: str,
        msg_type: str = "message",
        extra: Optional[Dict] = None
    ) -> str:
        """
        发送消息
        
        Args:
            sender: 发送者名称
            to: 接收者名称
            content: 消息内容
            msg_type: 消息类型
            extra: 额外字段
            
        Returns:
            操作结果消息
        """
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time()
        }
        
        if extra:
            msg.update(extra)
        
        with open(INBOX_DIR / f"{to}.jsonl", "a") as f:
            f.write(json.dumps(msg) + "\n")
        
        return f"Sent {msg_type} to {to}"
    
    def read_inbox(self, name: str) -> List[Dict[str, Any]]:
        """
        读取并清空收件箱
        
        Args:
            name: Agent 名称
            
        Returns:
            消息列表
        """
        path = INBOX_DIR / f"{name}.jsonl"
        
        if not path.exists():
            return []
        
        msgs = [json.loads(l) for l in path.read_text().strip().splitlines() if l]
        path.write_text("")
        
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
