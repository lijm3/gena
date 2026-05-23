"""
探测 ANTHROPIC_BASE_URL=https://api.gemai.cc / MODEL_NAME=gpt-5.4
是否支持 Anthropic Messages 协议的 image content block。

测试三种常见图片传入方式：
1. base64 内联（image / source.type=base64）
2. URL 引用（image / source.type=url）
3. 仅文本对照（验证 token 可用）
"""
import base64
import json
import os
import sys

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

# 一张 2x2 红色 PNG 的最小 base64（合法 PNG，肉眼是个红方块）
RED_2x2_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR42mP8z8DwHwAFBQIA"
    "X8jx0gAAAABJRU5ErkJggg=="
)

# 另一张公网可达的小图（用 httpbin 的 100x100 png）作为 URL 测试
PUBLIC_PNG_URL = "https://upload.wikimedia.org/wikipedia/commons/thumb/4/47/PNG_transparency_demonstration_1.png/120px-PNG_transparency_demonstration_1.png"


def call(payload, label):
    print(f"\n===== {label} =====")
    try:
        r = requests.post(
            f"{BASE_URL}/v1/messages",
            headers=HEADERS,
            json=payload,
            timeout=60,
        )
        r.encoding = "utf-8"
        print(f"HTTP {r.status_code}")
        try:
            data = r.json()
        except Exception:
            print(r.text[:1000])
            return None
        # 紧凑打印关键字段
        if "content" in data:
            for c in data["content"]:
                if c.get("type") == "text":
                    print(f"[text] {c['text'][:400]}")
                else:
                    print(f"[{c.get('type')}] {json.dumps(c, ensure_ascii=False)[:200]}")
            print(f"stop_reason={data.get('stop_reason')} usage={data.get('usage')}")
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2)[:1200])
        return data
    except Exception as e:
        print(f"!! 异常: {e}")
        return None


def test_text_only():
    payload = {
        "model": MODEL,
        "max_tokens": 200,
        "stream": False,
        "messages": [
            {"role": "user", "content": "回复一句中文：你好"},
        ],
    }
    return call(payload, "测试 1：纯文本对照")


def test_image_base64():
    payload = {
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
                            "data": RED_2x2_PNG_B64,
                        },
                    },
                    {"type": "text", "text": "用一句话描述这张图片的颜色和大致内容。"},
                ],
            }
        ],
    }
    return call(payload, "测试 2：image / source.type=base64")


def test_image_url():
    payload = {
        "model": MODEL,
        "max_tokens": 300,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "url", "url": PUBLIC_PNG_URL},
                    },
                    {"type": "text", "text": "用一句话描述图片内容。"},
                ],
            }
        ],
    }
    return call(payload, "测试 3：image / source.type=url")


if __name__ == "__main__":
    test_text_only()
    test_image_base64()
    test_image_url()
