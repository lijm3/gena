"""第二轮：用真实大小的 PNG 排除"图片太小被拒"的可能。"""
import base64
import io
import json
import struct
import zlib

import requests

BASE_URL = "https://api.gemai.cc"
TOKEN = "sk-7MVo4m4m5OiEc4LTyDu0frFEMyqLjyE9Irm5gL4bcTNQS3HS"
MODEL = "gpt-5.4"

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {TOKEN}",
    "x-api-key": TOKEN,
    "anthropic-version": "2023-06-01",
}


def make_png(width=64, height=64, rgb=(255, 64, 64)):
    """手搓一个合法的纯色 PNG。"""
    def chunk(tag, data):
        out = struct.pack(">I", len(data)) + tag + data
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return out + struct.pack(">I", crc)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    raw = b""
    for _ in range(height):
        raw += b"\x00" + bytes(rgb) * width
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


png_bytes = make_png(64, 64, (255, 60, 60))
png_b64 = base64.b64encode(png_bytes).decode("ascii")
print(f"PNG size: {len(png_bytes)} bytes, base64 len: {len(png_b64)}")


def call(payload, label):
    print(f"\n===== {label} =====")
    try:
        r = requests.post(
            f"{BASE_URL}/v1/messages", headers=HEADERS, json=payload, timeout=60
        )
        r.encoding = "utf-8"
        print(f"HTTP {r.status_code}")
        try:
            data = r.json()
        except Exception:
            print(r.text[:1500])
            return None
        if "content" in data:
            for c in data["content"]:
                if c.get("type") == "text":
                    print(f"[text] {c['text'][:600]}")
                else:
                    print(f"[{c.get('type')}] {json.dumps(c, ensure_ascii=False)[:200]}")
            print(f"stop_reason={data.get('stop_reason')} usage={data.get('usage')}")
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2)[:1500])
        return data
    except Exception as e:
        print(f"!! 异常: {e}")
        return None


# 测试 A：Anthropic 协议 - base64 大图
payload_a = {
    "model": MODEL,
    "max_tokens": 300,
    "stream": False,
    "messages": [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": png_b64,
                    },
                },
                {"type": "text", "text": "请描述图片：分辨率多少？主色调？"},
            ],
        }
    ],
}
call(payload_a, "A: Anthropic image/base64 (64x64 PNG)")

# 测试 B：流式 + base64
payload_b = dict(payload_a, stream=True)
print("\n===== B: 流式 + base64 =====")
try:
    r = requests.post(
        f"{BASE_URL}/v1/messages",
        headers=HEADERS,
        json=payload_b,
        timeout=60,
        stream=True,
    )
    print(f"HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:1500])
    else:
        text_acc = ""
        for line in r.iter_lines(decode_unicode=False):
            if not line:
                continue
            s = line.decode("utf-8", "replace")
            if not s.startswith("data:"):
                continue
            raw = s[5:].strip()
            if raw == "[DONE]":
                break
            try:
                evt = json.loads(raw)
            except Exception:
                continue
            if evt.get("type") == "content_block_delta":
                d = evt.get("delta") or {}
                if d.get("type") == "text_delta":
                    text_acc += d.get("text") or ""
            elif evt.get("type") == "message_delta":
                print(f"  stop_reason={evt.get('delta', {}).get('stop_reason')}")
        print(f"[text] {text_acc[:600]}")
    r.close()
except Exception as e:
    print(f"!! 异常: {e}")

# 测试 C：尝试 OpenAI 兼容格式（image_url）——有些"Anthropic 协议网关"
# 在底层是 OpenAI 后端，会附带支持这种 content block
payload_c = {
    "model": MODEL,
    "max_tokens": 300,
    "stream": False,
    "messages": [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{png_b64}"},
                },
                {"type": "text", "text": "请描述图片。"},
            ],
        }
    ],
}
call(payload_c, "C: OpenAI image_url + data URI (兼容性兜底)")
