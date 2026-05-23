"""
图片处理工具：本地路径 → Anthropic image content block

只支持 base64 内联（探测确认 api.gemai.cc 的 URL 模式有 bug）。
详见 docs/图片支持方案.md。
"""
import base64
import mimetypes
from pathlib import Path
from typing import Any, Dict

from utils.path_utils import safe_path
from utils.logging_setup import get_logger

log = get_logger(__name__)

# 允许的 MIME 与文件大小硬上限（避免一张 50MB 截图直接挤爆 token 预算）
ALLOWED_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5MB
MIN_IMAGE_BYTES = 100              # 网关对极小图会 400（探测：2x2 PNG=82B 被拒，64x64 PNG=178B 通过）

# 图像 token 估算常量：探测时 64×64 PNG 实际计费 ~24 input_tokens，
# 这里取一个保守平均值，避免按 base64 字节估算导致 estimate_tokens 爆掉。
# 详见 utils/compression.py::estimate_tokens 调用处。
IMAGE_TOKEN_EST = 1500


def guess_media_type(path: Path) -> str:
    """根据文件扩展名猜测 MIME；不在白名单时退回 image/png。"""
    mt, _ = mimetypes.guess_type(str(path))
    if mt in ALLOWED_MIMES:
        return mt
    return "image/png"


def file_to_image_block(path: str) -> Dict[str, Any]:
    """
    本地图片 → Anthropic image content block。

    安全：经 safe_path 限制在 WORKDIR 内，不允许 .. 逃逸。
    校验：MIN/MAX 字节边界，越界直接抛 ValueError。

    Returns:
        形如 {"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}
    """
    p = safe_path(path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"图片不存在: {path}")
    size = p.stat().st_size
    if size < MIN_IMAGE_BYTES:
        raise ValueError(f"图片太小（{size}B < {MIN_IMAGE_BYTES}B），网关可能拒收")
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f"图片过大（{size}B > {MAX_IMAGE_BYTES}B）")
    data = p.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    log.info("[image-util] convert image to Base64 output: %s", b64)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": guess_media_type(p),
            "data": b64,
        },
    }


def is_image_block(block: Any) -> bool:
    """判断一个 content block 是否是 Anthropic image 类型。"""
    return isinstance(block, dict) and block.get("type") == "image"


def block_short_desc(block: Dict[str, Any]) -> str:
    """给压缩管道用：把 image block 变成可读占位符。"""
    src = block.get("source", {}) if isinstance(block, dict) else {}
    mt = src.get("media_type", "image/?")
    data = src.get("data") or ""
    # base64 长度 ≈ 原字节数 * 4/3，反推近似原始大小
    raw_bytes = (len(data) * 3) // 4
    return f"[image cleared: {mt} ~{raw_bytes}B]"


def count_images(messages: list) -> int:
    """统计 messages 里的 image block 数量；给 token 估算用。"""
    count = 0
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if is_image_block(part):
                count += 1
    return count


def strip_images_for_text(messages: list) -> list:
    """
    生成 messages 的浅拷贝，把所有 image block 替换成 text 占位符。

    用途：
      - auto_compact 摘要前剥离（避免把 base64 灌进摘要请求）
      - estimate_tokens 文本预估前剥离（避免按 base64 字节高估）
    """
    cleaned = []
    for msg in messages:
        if not isinstance(msg, dict):
            cleaned.append(msg)
            continue
        c = msg.get("content")
        if isinstance(c, list):
            new_c = [
                {"type": "text", "text": block_short_desc(p)} if is_image_block(p) else p
                for p in c
            ]
            cleaned.append({**msg, "content": new_c})
        else:
            cleaned.append(msg)
    return cleaned
