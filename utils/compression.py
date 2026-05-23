"""
上下文压缩 - s06: 两级压缩机制
"""
import json
import time
from pathlib import Path
from typing import List, Dict, Any

from config.settings import TRANSCRIPT_DIR, TOKEN_THRESHOLD
from core.llm_client import LLMClient
from utils.image_utils import (
    IMAGE_TOKEN_EST,
    block_short_desc,
    count_images,
    is_image_block,
    strip_images_for_text,
)
from utils.logging_setup import get_logger

log = get_logger(__name__)

# tiktoken 是可选依赖：装了用它（更准），没装走字节估算回退。
# 注意 tiktoken 是 OpenAI 的 BPE，Claude 实际 tokenization 略有差异，但作为"是否触发压缩"的判定够用了。
try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENC = None


def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    """
    估算消息列表的 token 数

    主路径：tiktoken cl100k_base（对纯文本估算更准）
    回退：UTF-8 字节数 // 3

    图像处理：base64 数据按 BPE 编码会被严重高估（一张图可能算成 20K+ token，
    实际计费可能只有 1500 左右）。先把所有 image block 替换成短占位符再做文本估算，
    再按图片数量补上 IMAGE_TOKEN_EST。

    Args:
        messages: 消息历史列表

    Returns:
        估算的 token 数
    """
    cleaned = strip_images_for_text(messages)
    text = json.dumps(cleaned, default=str, ensure_ascii=False)

    if _ENC is not None:
        try:
            text_tokens = len(_ENC.encode(text))
        except Exception:
            text_tokens = len(text.encode("utf-8")) // 3
    else:
        text_tokens = len(text.encode("utf-8")) // 3

    return text_tokens + count_images(messages) * IMAGE_TOKEN_EST


def microcompact(messages: List[Dict[str, Any]]):
    """
    微压缩：清理旧的工具结果，以及非最近的 image block

    Args:
        messages: 消息历史列表（原地修改）
    """
    # 1) 清理旧 tool_result 文本
    indices = []
    for msg in messages:
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part in msg["content"]:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    indices.append(part)

    if len(indices) > 3:
        for part in indices[:-3]:
            if isinstance(part.get("content"), str) and len(part["content"]) > 100:
                part["content"] = "[cleared]"
            elif isinstance(part.get("content"), list):
                # tool_result.content 是 content block 列表时（如 read_image 输出），
                # 把整个 list 替换成占位字符串，单独保留 image 短描述
                desc_parts = [
                    block_short_desc(b) if is_image_block(b)
                    else b.get("text", "[block]")[:60]
                    for b in part["content"] if isinstance(b, dict)
                ]
                part["content"] = "[cleared] " + " | ".join(desc_parts)

    # 2) 清理旧 image block（保留最近 2 张图，其余替换为占位符）
    image_carriers = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for i, part in enumerate(content):
            if is_image_block(part):
                image_carriers.append((msg, i))

    if len(image_carriers) > 2:
        for msg, i in image_carriers[:-2]:
            original = msg["content"][i]
            msg["content"][i] = {"type": "text", "text": block_short_desc(original)}


def auto_compact(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    自动压缩：生成历史摘要

    任一环节失败都不抛到 agent_loop——压缩是恢复机制，自己不能成为新故障源。
    层次：
        1) transcript 落盘失败 → 仅记录，继续摘要
        2) LLM 摘要失败/返回空 → 走"保留最后 N 条原文"的截断兜底

    Args:
        messages: 原始消息历史

    Returns:
        压缩后的消息列表
    """
    TRANSCRIPT_DIR.mkdir(exist_ok=True)

    # 保存完整历史（落盘失败不影响压缩本身）
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    try:
        with open(path, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("transcript save failed: %s", e)
        path = None

    # 提取最近的对话内容（先剥离图像，避免把 base64 灌进摘要请求）
    summary_input = strip_images_for_text(messages)
    conv_text = json.dumps(summary_input, default=str, ensure_ascii=False)[-80000:]

    # 调用 LLM 生成摘要（失败走兜底）
    summary = ""
    try:
        client = LLMClient()
        resp = client.create_message(
            messages=[{"role": "user", "content": f"Summarize for continuity:\n{conv_text}"}],
            max_tokens=2000
        )
        texts = [
            b.get("text", "") for b in resp.get("content", [])
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        summary = "".join(texts).strip()
    except Exception as e:
        log.warning("LLM summarize failed: %s; falling back to truncation", e)

    if not summary:
        # 截断兜底:保留最后 6 条原始消息（覆盖最近一次工具调用对），保证对话能继续。
        # 比"什么都没有"或"无限重试"都更可靠——再差也比死循环强。
        tail = messages[-6:] if len(messages) > 6 else list(messages)
        prefix = (
            f"[Compressed. Transcript: {path}]\n"
            f"(summary unavailable, kept last {len(tail)} messages)"
        )
        return [{"role": "user", "content": prefix}, *tail]

    return [
        {"role": "user", "content": f"[Compressed. Transcript: {path}]\n{summary}"}
    ]
