"""
上下文压缩 - s06: 两级压缩机制
"""
import json
import time
from pathlib import Path
from typing import List, Dict, Any

from config.settings import TRANSCRIPT_DIR, TOKEN_THRESHOLD
from core.llm_client import LLMClient


def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    """
    估算消息列表的 token 数
    
    Args:
        messages: 消息历史列表
        
    Returns:
        估算的 token 数
    """
    return len(json.dumps(messages, default=str)) // 4


def microcompact(messages: List[Dict[str, Any]]):
    """
    微压缩：清理旧的工具结果
    
    Args:
        messages: 消息历史列表（原地修改）
    """
    indices = []
    for msg in messages:
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part in msg["content"]:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    indices.append(part)
    
    if len(indices) <= 3:
        return
    
    for part in indices[:-3]:
        if isinstance(part.get("content"), str) and len(part["content"]) > 100:
            part["content"] = "[cleared]"


def auto_compact(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    自动压缩：生成历史摘要
    
    Args:
        messages: 原始消息历史
        
    Returns:
        压缩后的消息列表
    """
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    
    # 保存完整历史
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    
    # 提取最近的对话内容
    conv_text = json.dumps(messages, default=str)[-80000:]
    
    # 调用 LLM 生成摘要
    client = LLMClient()
    resp = client.create_message(
        messages=[{"role": "user", "content": f"Summarize for continuity:\n{conv_text}"}],
        max_tokens=2000
    )
    summary = resp["content"][0]["text"]
    
    return [
        {"role": "user", "content": f"[Compressed. Transcript: {path}]\n{summary}"}
    ]
