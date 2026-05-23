"""
日志脱敏:在把 messages / tool 入参写日志前,擦掉敏感字段。

主要解决的问题:
  运行时 ssh_add_host 的 password 入参会出现在 LLM history。
  开启 LLM_DEBUG_PRINT 时,main_agent / subagent / teammate 会用
  log.debug("input: %s", messages) 把整个 history 打到 agent.log。
  如果不脱敏,日志里每次 ssh_add_host 调用都明文留底。

设计:
  - 只处理已知的敏感键名(白名单更安全;黑名单容易漏)
  - 同时覆盖 dict 嵌套与文本嵌入(tool_use 的 input 是 dict,
    但 LLM 文本里也可能引述用户输入的"密码 P@ss..."字样,
    后者不在本模块的范围——那种泄露用户已在选型阶段接受)
  - 不修改原对象,返回深拷贝,避免污染 history
"""
import copy
import re
from typing import Any

# 敏感字段名(dict key 完全匹配,大小写不敏感)
_SENSITIVE_KEYS = frozenset({
    "password",
    "passphrase",
    "secret",
    "token",
    "api_key",
    "apikey",
})

_REDACTED = "***"


def _scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _SENSITIVE_KEYS and isinstance(v, str) and v:
                out[k] = _REDACTED
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_scrub(x) for x in obj)
    return obj


def sanitize_for_log(obj: Any) -> Any:
    """返回 obj 的深拷贝副本,敏感字段被替换为 ***。

    用法:
        log.debug("input: %s", sanitize_for_log(messages))
    """
    return _scrub(copy.deepcopy(obj))


# 也提供一个文本层脱敏,给已经序列化的 JSON 字符串用(transcript 落盘前)
_JSON_PASSWORD_RE = re.compile(
    r'("(?:password|passphrase|secret|token|api[_-]?key)"\s*:\s*)"[^"\\]*(?:\\.[^"\\]*)*"',
    re.IGNORECASE,
)


def sanitize_json_text(s: str) -> str:
    """对一段已经序列化的 JSON 文本做正则脱敏。"""
    return _JSON_PASSWORD_RE.sub(r'\1"***"', s)
