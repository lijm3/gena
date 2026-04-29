"""
关闭协议管理器 - s10: 优雅关闭和计划审批
"""
import uuid
from typing import Dict, Any

from managers.message_bus import MessageBus

# 全局跟踪字典
shutdown_requests: Dict[str, Dict[str, Any]] = {}
plan_requests: Dict[str, Dict[str, Any]] = {}


def handle_shutdown_request(bus: MessageBus, teammate: str) -> str:
    """
    请求队友 Agent 关闭
    
    Args:
        teammate: 要关闭的队友名称
        
    Returns:
        操作结果消息
    """
    req_id = str(uuid.uuid4())[:8]
    shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    bus.send("lead", teammate, "Please shut down.", "shutdown_request", {"request_id": req_id})
    return f"Shutdown request {req_id} sent to '{teammate}'"


def handle_plan_review(
    bus: MessageBus,
    request_id: str,
    approve: bool,
    feedback: str = "",
) -> str:
    """
    审批队友的计划
    
    Args:
        request_id: 计划请求 ID
        approve: 是否批准
        feedback: 反馈意见
        
    Returns:
        操作结果消息
    """
    req = plan_requests.get(request_id)
    if not req:
        return f"Error: Unknown plan request_id '{request_id}'"
    
    req["status"] = "approved" if approve else "rejected"
    bus.send(
        "lead",
        req["from"],
        feedback,
        "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback}
    )
    return f"Plan {req['status']} for '{req['from']}'"
