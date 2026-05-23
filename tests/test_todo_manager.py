"""测试 managers/todo_manager"""
import pytest

from managers.todo_manager import TodoManager


class TestTodoManager:
    def test_empty_render(self):
        m = TodoManager()
        assert m.render() == "无 todos。"
        assert m.has_open_items() is False

    def test_update_basic(self):
        m = TodoManager()
        out = m.update([
            {"content": "task A", "status": "pending", "activeForm": "Doing A"},
        ])
        assert "task A" in out
        assert m.has_open_items() is True

    def test_in_progress_shows_active_form(self):
        m = TodoManager()
        out = m.update([
            {"content": "task A", "status": "in_progress", "activeForm": "Doing A"},
        ])
        assert "<- Doing A" in out

    def test_completed_no_open(self):
        m = TodoManager()
        m.update([
            {"content": "done", "status": "completed", "activeForm": "Did it"},
        ])
        assert m.has_open_items() is False

    def test_rejects_invalid_status(self):
        m = TodoManager()
        with pytest.raises(ValueError, match="无效状态"):
            m.update([
                {"content": "x", "status": "weird", "activeForm": "x"},
            ])

    def test_rejects_more_than_one_in_progress(self):
        m = TodoManager()
        with pytest.raises(ValueError, match="一个 in_progress"):
            m.update([
                {"content": "A", "status": "in_progress", "activeForm": "A"},
                {"content": "B", "status": "in_progress", "activeForm": "B"},
            ])

    def test_rejects_more_than_20(self):
        m = TodoManager()
        with pytest.raises(ValueError, match="20"):
            m.update([
                {"content": f"t{i}", "status": "pending", "activeForm": f"a{i}"}
                for i in range(21)
            ])

    def test_rejects_missing_content(self):
        m = TodoManager()
        with pytest.raises(ValueError, match="content"):
            m.update([{"content": "  ", "status": "pending", "activeForm": "x"}])

    def test_rejects_missing_active_form(self):
        m = TodoManager()
        with pytest.raises(ValueError, match="activeForm"):
            m.update([{"content": "x", "status": "pending", "activeForm": ""}])

    def test_counter_renders(self):
        m = TodoManager()
        out = m.update([
            {"content": "A", "status": "completed", "activeForm": "A"},
            {"content": "B", "status": "pending", "activeForm": "B"},
        ])
        assert "1/2" in out
