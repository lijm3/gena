"""
图片支持端到端冒烟测试

不依赖 pytest，直接用 assert，便于在没有测试框架的项目里跑。

测试覆盖：
  1) image_utils.file_to_image_block：合法 PNG → 合法 block，边界（过小/过大）抛错
  2) ToolResult.to_llm_format：content=list 时透传含状态 text 块
  3) run_read_image：返回 ToolResult，结构正确
  4) parse_user_input：/img 语法解析
  5) compression：image block 经 strip / microcompact 后被替换成占位符
  6) estimate_tokens：含图与不含图差距合理（不会因 base64 爆掉）
  7) 端到端：把 read_image 工具调用与 LLMClient 串起来，模型能"看到"图（依赖环境变量）
"""
import base64
import os
import struct
import sys
import zlib
from pathlib import Path

# 确保以仓库根为 cwd 运行（main.py / config.settings.WORKDIR 依赖）
ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


def make_png(width=64, height=64, rgb=(255, 60, 60)) -> bytes:
    def chunk(tag, data):
        out = struct.pack(">I", len(data)) + tag + data
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return out + struct.pack(">I", crc)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b""
    for _ in range(height):
        raw += b"\x00" + bytes(rgb) * width
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# 准备一张测试图
TMP_DIR = ROOT / ".transcripts"
TMP_DIR.mkdir(exist_ok=True)
PNG_PATH = TMP_DIR / "smoke_red.png"
PNG_PATH.write_bytes(make_png(64, 64, (220, 30, 30)))
TINY_PATH = TMP_DIR / "smoke_tiny.png"
TINY_PATH.write_bytes(make_png(2, 2, (0, 0, 0)))


def t1_image_utils():
    from utils.image_utils import (
        file_to_image_block, is_image_block, block_short_desc,
        count_images, strip_images_for_text,
    )

    rel = str(PNG_PATH.relative_to(ROOT)).replace("\\", "/")
    block = file_to_image_block(rel)
    assert is_image_block(block), f"未识别为 image block: {block}"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/png"
    assert block["source"]["data"]
    # 反解 base64 必须能还原 PNG 头
    raw = base64.b64decode(block["source"]["data"])
    assert raw.startswith(b"\x89PNG"), "base64 解码不是合法 PNG"

    # 过小图必须抛错
    tiny_rel = str(TINY_PATH.relative_to(ROOT)).replace("\\", "/")
    try:
        file_to_image_block(tiny_rel)
        raise AssertionError("过小图未抛错")
    except ValueError:
        pass

    # 不存在
    try:
        file_to_image_block("nonexistent_xxx.png")
        raise AssertionError("不存在文件未抛错")
    except FileNotFoundError:
        pass

    # 占位符 / 计数 / 剥离
    msgs = [{"role": "user", "content": [block, {"type": "text", "text": "hi"}]}]
    assert count_images(msgs) == 1
    desc = block_short_desc(block)
    assert "image cleared" in desc and "image/png" in desc
    cleaned = strip_images_for_text(msgs)
    assert cleaned[0]["content"][0]["type"] == "text"
    assert "image cleared" in cleaned[0]["content"][0]["text"]
    print("[t1] image_utils OK")


def t2_tool_result_list():
    from utils.loop_control import ToolResult
    block = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}}
    r = ToolResult(content=[block, {"type": "text", "text": "ok"}], changed=False, done=True)
    out = r.to_llm_format()
    assert isinstance(out, list), f"list 输入未透传: {out!r}"
    # 第一个应是状态前缀 text，第二个是 image
    assert out[0]["type"] == "text" and "DONE" in out[0]["text"]
    assert out[1]["type"] == "image"
    # 字符串路径保持原状
    r2 = ToolResult(content="hello", changed=True, done=True)
    out2 = r2.to_llm_format()
    assert isinstance(out2, str) and "DONE" in out2 and "CHANGED" in out2
    print("[t2] ToolResult list 透传 OK")


def t3_run_read_image():
    from tools.base_tools import run_read_image
    from utils.loop_control import ToolResult
    rel = str(PNG_PATH.relative_to(ROOT)).replace("\\", "/")
    out = run_read_image(rel)
    assert isinstance(out, ToolResult)
    assert isinstance(out.content, list)
    assert out.content[0]["type"] == "image"
    fmt = out.to_llm_format()
    assert isinstance(fmt, list) and any(b.get("type") == "image" for b in fmt)
    print("[t3] run_read_image OK")


def t4_parse_user_input():
    from main import parse_user_input
    rel = str(PNG_PATH.relative_to(ROOT)).replace("\\", "/")

    # 纯文本
    blocks = parse_user_input("你好")
    assert blocks == [{"type": "text", "text": "你好"}]

    # /img 单图
    blocks = parse_user_input(f"/img {rel} | 描述这张图")
    types = [b["type"] for b in blocks]
    assert types == ["image", "text"], types
    assert blocks[-1]["text"] == "描述这张图"

    # /img 双图，省略文本
    blocks = parse_user_input(f"/img {rel}, {rel}")
    types = [b["type"] for b in blocks]
    assert types == ["image", "image", "text"], types

    # 无效路径
    try:
        parse_user_input("/img nope.png")
        raise AssertionError("无效路径未抛错")
    except (FileNotFoundError, ValueError):
        pass

    print("[t4] parse_user_input OK")


def t5_microcompact_images():
    from utils.compression import microcompact
    from utils.image_utils import is_image_block, file_to_image_block
    rel = str(PNG_PATH.relative_to(ROOT)).replace("\\", "/")
    msgs = []
    for _ in range(5):
        msgs.append({"role": "user", "content": [file_to_image_block(rel), {"type": "text", "text": "q"}]})
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "a"}]})
    microcompact(msgs)
    # 应该只剩最后 2 张图
    image_count = sum(
        1 for m in msgs if isinstance(m.get("content"), list)
        for p in m["content"] if is_image_block(p)
    )
    assert image_count == 2, f"microcompact 后图数量={image_count} 应=2"
    # 被剥的位置变成 text 占位符
    placeholders = [
        p for m in msgs if isinstance(m.get("content"), list)
        for p in m["content"]
        if isinstance(p, dict) and p.get("type") == "text" and "image cleared" in p.get("text", "")
    ]
    assert len(placeholders) == 3, f"占位符数量={len(placeholders)} 应=3"
    print("[t5] microcompact 图像剥离 OK")


def t6_estimate_tokens_image_safe():
    from utils.compression import estimate_tokens
    from utils.image_utils import file_to_image_block, IMAGE_TOKEN_EST
    rel = str(PNG_PATH.relative_to(ROOT)).replace("\\", "/")
    plain = [{"role": "user", "content": "hi"}]
    with_img = [{"role": "user", "content": [file_to_image_block(rel), {"type": "text", "text": "hi"}]}]
    a = estimate_tokens(plain)
    b = estimate_tokens(with_img)
    # 加一张图，token 估算的差应大约等于 IMAGE_TOKEN_EST（允许一定文本侧波动）
    diff = b - a
    assert IMAGE_TOKEN_EST - 200 <= diff <= IMAGE_TOKEN_EST + 800, f"图片 token 估算异常: diff={diff}"
    print(f"[t6] estimate_tokens 图像安全：plain={a}, with_img={b}, diff={diff}")


def t7_e2e_llm():
    """端到端：read_image -> ToolResult -> tool_result content -> 真实 LLM 调用 -> 模型描述。"""
    if not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print("[t7] 跳过（未设置 ANTHROPIC_AUTH_TOKEN）")
        return
    from core.llm_client import LLMClient
    from tools.base_tools import run_read_image
    from utils.loop_control import ToolResult

    rel = str(PNG_PATH.relative_to(ROOT)).replace("\\", "/")
    tool_result_content = run_read_image(rel).to_llm_format()
    # 模拟一次"模型先要求 read_image，工具返回，再问模型这是什么"
    messages = [
        {"role": "user", "content": "看一下这张图，告诉我主色调和大致分辨率。"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "read_image", "input": {"path": rel}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": tool_result_content},
            ],
        },
    ]
    client = LLMClient()
    resp = client.call_api(messages=messages, stream=False, max_tokens=300)
    texts = [b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"]
    answer = "".join(texts)
    print(f"[t7] 模型回答：{answer[:300]}")
    # 至少包含颜色或尺寸相关字眼，证明它"看到了"图
    keys = ["红", "red", "64", "像素", "px"]
    assert any(k in answer for k in keys), f"模型回答未体现视觉特征: {answer!r}"
    print("[t7] 端到端 OK")


if __name__ == "__main__":
    t1_image_utils()
    t2_tool_result_list()
    t3_run_read_image()
    t4_parse_user_input()
    t5_microcompact_images()
    t6_estimate_tokens_image_safe()
    t7_e2e_llm()
    print("\nALL SMOKE TESTS PASSED")
