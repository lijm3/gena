"""
Todo 管理器 - s03: 会话内任务清单
"""
from typing import List, Dict, Any

from utils.logging_setup import get_logger

log = get_logger(__name__)


class TodoManager:
    """
    Todo 管理器 - 会话内任务清单
    
    特性:
        - 最多 20 个 todo 项
        - 只允许一个 in_progress 状态
        - 三种状态: pending, in_progress, completed
        - 每个 todo 有 activeForm（当前正在做什么）
    """
    
    def __init__(self):
        """初始化 Todo 管理器"""
        self.items: List[Dict[str, Any]] = []
    
    def update(self, items: List[Dict[str, Any]]) -> str:
        """
        更新整个 todo 列表
        
        Args:
            items: todo 项列表
            
        Returns:
            渲染后的 todo 列表
            
        Raises:
            ValueError: 验证失败时
        """
        validated, ip = [], 0
        
        # 验证每个 todo 项
        for i, item in enumerate(items):
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).lower()
            af = str(item.get("activeForm", "")).strip()
            
            # 验证必需字段
            if not content:
                raise ValueError(f"项 {i}: content 必需")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"项 {i}: 无效状态 '{status}'")
            if not af:
                raise ValueError(f"项 {i}: activeForm 必需")
            
            # 统计 in_progress 数量
            if status == "in_progress":
                ip += 1
            
            validated.append({
                "content": content,
                "status": status,
                "activeForm": af
            })
        
        # 验证约束
        if len(validated) > 20:
            raise ValueError("最多 20 个 todos")
        if ip > 1:
            raise ValueError("只允许一个 in_progress")
        
        self.items = validated
        log.info(
            "todos updated: %d total (%d completed, %d in_progress, %d pending)",
            len(self.items),
            sum(1 for t in self.items if t["status"] == "completed"),
            sum(1 for t in self.items if t["status"] == "in_progress"),
            sum(1 for t in self.items if t["status"] == "pending"),
        )
        return self.render()
    
    def render(self) -> str:
        """
        渲染 todo 列表为可读文本
        
        Returns:
            格式化的 todo 列表字符串
        """
        if not self.items:
            return "无 todos。"
        
        lines = []
        for item in self.items:
            # 状态标记
            m = {
                "completed": "[x]",
                "in_progress": "[>]",
                "pending": "[ ]"
            }.get(item["status"], "[?]")
            
            # 进行中的任务显示 activeForm
            suffix = f" <- {item['activeForm']}" if item["status"] == "in_progress" else ""
            lines.append(f"{m} {item['content']}{suffix}")
        
        # 统计信息
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} 已完成)")
        return "\n".join(lines)
    
    def has_open_items(self) -> bool:
        """
        检查是否有未完成的 todo
        
        Returns:
            True 如果有未完成的项
        """
        return any(item.get("status") != "completed" for item in self.items)
