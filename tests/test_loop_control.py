"""测试 utils/loop_control：LoopDetector / ProgressTracker / BudgetController / ToolResult"""
import time

import pytest

from utils.loop_control import LoopDetector, ProgressTracker, BudgetController, ToolResult


# === ToolResult ===

class TestToolResult:
    def test_default_changed_done(self):
        r = ToolResult(content="hello")
        out = r.to_llm_format()
        assert "[CHANGED]" in out
        assert "hello" in out

    def test_no_change_flag(self):
        r = ToolResult(content="same", changed=False)
        out = r.to_llm_format()
        assert "NO_CHANGE" in out

    def test_done_changed_combo(self):
        r = ToolResult(content="x", changed=True, done=True)
        out = r.to_llm_format()
        assert "DONE" in out and "CHANGED" in out

    def test_error_flag(self):
        r = ToolResult(content="bad", error=True, changed=False)
        out = r.to_llm_format()
        assert "ERROR" in out
        # error 状态下不再额外标 NO_CHANGE
        assert "NO_CHANGE" not in out


# === LoopDetector ===

class TestLoopDetector:
    def test_no_repeat_no_loop(self):
        d = LoopDetector(window_size=3, threshold=2)
        assert d.add_call("bash", {"command": "ls"}) is False
        assert d.add_call("bash", {"command": "pwd"}) is False
        assert d.add_call("read_file", {"path": "a.py"}) is False

    def test_threshold_2_fires_on_second_same_call(self):
        d = LoopDetector(window_size=3, threshold=2)
        d.add_call("bash", {"command": "ls"})
        # 第二次相同入参 → 命中
        assert d.add_call("bash", {"command": "ls"}) is True

    def test_input_normalization_strips_strings(self):
        d = LoopDetector(window_size=3, threshold=2)
        d.add_call("bash", {"command": "ls"})
        # 加空格也算同一个调用
        assert d.add_call("bash", {"command": "  ls  "}) is True

    def test_ignored_keys_dont_affect_fingerprint(self):
        d = LoopDetector(window_size=3, threshold=2)
        d.add_call("bash", {"command": "ls", "timestamp": 1.0})
        # timestamp 字段是 ignored，仍算重复
        assert d.add_call("bash", {"command": "ls", "timestamp": 2.0}) is True

    def test_abab_alternating_detected(self):
        d = LoopDetector(window_size=4, threshold=99)  # threshold 故意拉高，让 ABAB 触发
        d.add_call("bash", {"command": "ls"})
        d.add_call("read_file", {"path": "a"})
        d.add_call("bash", {"command": "pwd"})  # 不同 command 但同 tool name
        # 第 4 次构成 ABAB 模式
        assert d.add_call("read_file", {"path": "b"}) is True

    def test_different_tools_not_loop(self):
        d = LoopDetector(window_size=3, threshold=2)
        for tool in ["bash", "read_file", "write_file", "bash"]:
            d.add_call(tool, {"x": 1})
        # bash 出现两次但中间隔得远（window 内 ≤1），不触发


# === ProgressTracker ===

class TestProgressTracker:
    def _result(self, content: str):
        return [{"content": content}]

    def test_not_enough_history_no_judgment(self):
        p = ProgressTracker(window_size=3, similarity_threshold=0.9)
        assert p.add_result(self._result("first")) is False
        assert p.add_result(self._result("second")) is False
        # window 未满，不判定

    def test_identical_results_trigger(self):
        p = ProgressTracker(window_size=3, similarity_threshold=0.9)
        for _ in range(3):
            res = p.add_result(self._result("same content always"))
        # 三轮内容完全相同 → 触发
        assert res is True

    def test_diverse_results_no_trigger(self):
        p = ProgressTracker(window_size=3, similarity_threshold=0.9)
        a = p.add_result(self._result("apple banana cherry"))
        b = p.add_result(self._result("dog elephant frog"))
        c = p.add_result(self._result("xylophone yacht zebra"))
        assert c is False


# === BudgetController ===

class TestBudgetController:
    def test_timeout_not_triggered_early(self):
        b = BudgetController(wall_clock_timeout=60, token_budget=1000)
        triggered, msg = b.check_timeout()
        assert triggered is False
        assert msg == ""

    def test_timeout_triggers_after_threshold(self):
        b = BudgetController(wall_clock_timeout=0, token_budget=1000)
        time.sleep(0.05)
        triggered, msg = b.check_timeout()
        assert triggered is True
        assert "timeout" in msg.lower()

    def test_token_budget_under(self):
        b = BudgetController(wall_clock_timeout=60, token_budget=1000)
        over, _ = b.check_token_budget(500)
        assert over is False

    def test_token_budget_over(self):
        b = BudgetController(wall_clock_timeout=60, token_budget=1000)
        over, msg = b.check_token_budget(2000)
        assert over is True
        assert "budget" in msg.lower()
