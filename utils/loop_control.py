"""
Agent 循环控制工具集

提供以下能力：
1) 工具结果状态化（ToolResult）
2) 重复调用检测（LoopDetector）
3) 无进展检测（ProgressTracker）
4) 预算控制（BudgetController）
5) 工具调用的简短渲染（format_tool_calls）
"""
import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple, Union


# 工具入参里最优先展示的字段名（按工具类型常见关键字段）。
# 优先匹配第一个找到的键，找不到就 JSON 截断兜底。
_BRIEF_INPUT_KEYS = ("command", "path", "subject", "to", "prompt", "name", "task_id", "items")


def format_tool_calls(tool_calls: List[Dict[str, Any]], max_value_len: int = 60) -> str:
    """
    把 LLM 返回的 tool_use blocks 渲染成可读字符串，给日志/调试用。

    输出示例：
        bash(command='ls -la') | TodoWrite(items=[{...}, {...}]) | task(prompt='research...')

    设计原则：
    - 避免把巨型 content 字段（write_file 的全文）灌到日志里
    - 每个工具只挑一个最有代表性的字段展示
    - 字符串值统一截断，超长加 …
    """
    if not tool_calls:
        return ""
    parts: List[str] = []
    for tc in tool_calls:
        name = tc.get("name", "?")
        inp = tc.get("input") or {}
        brief = ""
        for key in _BRIEF_INPUT_KEYS:
            if key in inp:
                v = inp[key]
                if isinstance(v, str):
                    v = v if len(v) <= max_value_len else v[:max_value_len] + "…"
                    brief = f"{key}={v!r}"
                else:
                    s = json.dumps(v, ensure_ascii=False, default=str)
                    s = s if len(s) <= max_value_len else s[:max_value_len] + "…"
                    brief = f"{key}={s}"
                break
        if not brief:
            try:
                s = json.dumps(inp, ensure_ascii=False, default=str)
            except Exception:
                s = str(inp)
            brief = s if len(s) <= max_value_len else s[:max_value_len] + "…"
        parts.append(f"{name}({brief})")
    return " | ".join(parts)


@dataclass
class ToolResult:
    """
    标准化工具返回结构。

    通过 changed/done/error 标志给模型可读的“状态信号”，
    可以显著降低无效重复调用。
    """

    # content 既可以是纯文本（绝大多数工具），也可以是 Anthropic content block 列表
    # （read_image 等需要把 image block 透传给模型时使用）。
    content: Union[str, List[Dict[str, Any]]]
    changed: bool = True
    done: bool = False
    error: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def _status_line(self) -> str:
        flags: List[str] = []
        if self.done:
            flags.append("DONE")
        if self.changed:
            flags.append("CHANGED")
        if self.error:
            flags.append("ERROR")
        if not self.changed and not self.error:
            flags.append("NO_CHANGE")
        return f"[{' | '.join(flags)}]" if flags else ""

    def to_llm_format(self) -> Union[str, List[Dict[str, Any]]]:
        """
        生成模型可读输出。

        - content 为 str：返回 "[DONE | CHANGED]\\nxxx" 形式的文本。
        - content 为 list（含 image 等 block）：返回 [status_text_block, *content]，
          直接作为 tool_result.content 透传给 Anthropic Messages 协议。
        """
        status_line = self._status_line()
        if isinstance(self.content, list):
            prefix = [{"type": "text", "text": status_line}] if status_line else []
            return [*prefix, *self.content]
        return f"{status_line}\n{self.content}" if status_line else self.content


class LoopDetector:
    """检测重复工具调用（包括简单模式循环）。"""

    def __init__(self, window_size: int = 3, threshold: int = 2):
        self.window_size = window_size
        self.threshold = threshold
        self.call_history: deque[Tuple[str, str]] = deque(maxlen=window_size * 4)

    def _normalize_input(self, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        """规范化入参，过滤噪声字段，避免指纹受随机字段影响。"""
        ignored_keys = {"timestamp", "ts", "time", "nonce"}
        cleaned: Dict[str, Any] = {}
        for key, value in tool_input.items():
            if key in ignored_keys:
                continue
            cleaned[key] = value.strip() if isinstance(value, str) else value
        return cleaned

    def fingerprint(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        """为工具调用生成稳定短指纹。"""
        normalized_input = self._normalize_input(tool_input)
        raw = f"{tool_name}:{json.dumps(normalized_input, sort_keys=True, ensure_ascii=False)}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]

    def add_call(self, tool_name: str, tool_input: Dict[str, Any]) -> bool:
        """
        记录调用并检查循环。

        返回:
            True: 检测到高概率循环
            False: 正常
        """
        fp = self.fingerprint(tool_name, tool_input)
        self.call_history.append((tool_name, fp))

        recent = list(self.call_history)[-self.window_size :]
        repeated_count = sum(1 for _, cur_fp in recent if cur_fp == fp)
        if repeated_count >= self.threshold:
            return True

        # 检测 ABAB 之类的交替模式（窗口足够大时）
        if len(recent) >= 4:
            names = [name for name, _ in recent]
            if names[-4] == names[-2] and names[-3] == names[-1]:
                return True

        return False


class ProgressTracker:
    """检测多轮工具执行是否有实质进展。"""

    def __init__(self, window_size: int = 3, similarity_threshold: float = 0.9):
        self.window_size = window_size
        self.similarity_threshold = similarity_threshold
        self.result_history: deque[str] = deque(maxlen=window_size)

    def _extract_key_info(self, tool_results: List[Dict[str, Any]]) -> str:
        """
        从工具结果提取关键摘要，避免直接比较超长原文。
        """
        normalized_parts: List[str] = []
        for item in tool_results:
            content = str(item.get("content", ""))
            # 去除多余空白并截断，重点比较“趋势”而不是全文。
            compact = " ".join(content.split())[:200]
            normalized_parts.append(compact)
        return "|".join(normalized_parts)

    @staticmethod
    def _similarity_ratio(left: str, right: str) -> float:
        """基于哈希 token 的轻量相似度估算。"""
        if not left and not right:
            return 1.0
        if not left or not right:
            return 0.0

        left_tokens = set(left.split())
        right_tokens = set(right.split())
        if not left_tokens and not right_tokens:
            return 1.0

        intersection = len(left_tokens & right_tokens)
        union = len(left_tokens | right_tokens) or 1
        return intersection / union

    def add_result(self, tool_results: List[Dict[str, Any]]) -> bool:
        """
        记录本轮结果并判断是否“无进展”。
        """
        summary = self._extract_key_info(tool_results)
        self.result_history.append(summary)

        if len(self.result_history) < self.window_size:
            return False

        # 比较窗口内相邻轮次相似度，整体都很高则判断为无进展。
        history = list(self.result_history)
        similarities: List[float] = []
        for idx in range(1, len(history)):
            similarities.append(self._similarity_ratio(history[idx - 1], history[idx]))

        avg_similarity = sum(similarities) / len(similarities) if similarities else 0.0
        return avg_similarity >= self.similarity_threshold


class BudgetController:
    """管理 wall-clock 与 token 双预算。"""

    def __init__(self, wall_clock_timeout: int = 60, token_budget: int = 150000):
        self.wall_clock_timeout = wall_clock_timeout
        self.token_budget = token_budget
        self.start_time = time.time()
        self.tokens_used = 0

    def check_timeout(self) -> Tuple[bool, str]:
        """检查是否超时。"""
        elapsed = time.time() - self.start_time
        if elapsed > self.wall_clock_timeout:
            return True, f"Wall-clock timeout ({elapsed:.1f}s > {self.wall_clock_timeout}s)"
        return False, ""

    def check_token_budget(self, current_tokens: int) -> Tuple[bool, str]:
        """检查 token 是否超预算。"""
        self.tokens_used = current_tokens
        if current_tokens > self.token_budget:
            return True, f"Token budget exceeded ({current_tokens} > {self.token_budget})"
        return False, ""
