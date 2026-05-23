"""测试 utils/compression：estimate_tokens / microcompact"""
from utils.compression import estimate_tokens, microcompact


class TestEstimateTokens:
    def test_empty(self):
        n = estimate_tokens([])
        assert n >= 0

    def test_grows_with_content(self):
        small = estimate_tokens([{"role": "user", "content": "hi"}])
        big = estimate_tokens([{"role": "user", "content": "hello world " * 100}])
        assert big > small

    def test_chinese_content(self):
        # 验证不会因为中文 crash；结果应该 > 0
        n = estimate_tokens([{"role": "user", "content": "你好世界" * 50}])
        assert n > 10


class TestMicrocompact:
    def _tool_result_msg(self, tid: str, content: str):
        return {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tid, "content": content}],
        }

    def test_few_tool_results_unchanged(self):
        msgs = [
            self._tool_result_msg("t1", "x" * 200),
            self._tool_result_msg("t2", "y" * 200),
        ]
        before = msgs[0]["content"][0]["content"]
        microcompact(msgs)
        # 不足 3 个，不动
        assert msgs[0]["content"][0]["content"] == before

    def test_keeps_last_three(self):
        msgs = [self._tool_result_msg(f"t{i}", "x" * 200) for i in range(5)]
        microcompact(msgs)
        # 前 2 个被清，后 3 个保留
        assert msgs[0]["content"][0]["content"] == "[cleared]"
        assert msgs[1]["content"][0]["content"] == "[cleared]"
        assert msgs[2]["content"][0]["content"] != "[cleared]"
        assert msgs[3]["content"][0]["content"] != "[cleared]"
        assert msgs[4]["content"][0]["content"] != "[cleared]"

    def test_short_content_not_cleared(self):
        msgs = [
            self._tool_result_msg("t1", "short"),  # < 100 char
            self._tool_result_msg("t2", "x" * 200),
            self._tool_result_msg("t3", "x" * 200),
            self._tool_result_msg("t4", "x" * 200),
        ]
        microcompact(msgs)
        # t1 虽然在 "前面"（不在最后 3 个），但内容 < 100 不清
        assert msgs[0]["content"][0]["content"] == "short"
