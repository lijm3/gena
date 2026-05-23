"""测试 managers/task_manager（SQLite 后端）"""
import json
import threading

import pytest

from managers.task_manager import TaskManager


class TestTaskCRUD:
    def test_create_and_get(self, tmp_db):
        tm = TaskManager()
        out = tm.create("task A", "desc A")
        t = json.loads(out)
        assert t["subject"] == "task A"
        assert t["description"] == "desc A"
        assert t["status"] == "pending"
        assert t["owner"] is None
        assert t["blockedBy"] == []

        got = json.loads(tm.get(t["id"]))
        assert got == t

    def test_get_missing_raises(self, tmp_db):
        tm = TaskManager()
        with pytest.raises(ValueError, match="not found"):
            tm.get(999)

    def test_update_status(self, tmp_db):
        tm = TaskManager()
        tid = json.loads(tm.create("x"))["id"]
        out = tm.update(tid, status="in_progress")
        assert json.loads(out)["status"] == "in_progress"

    def test_delete_removes(self, tmp_db):
        tm = TaskManager()
        tid = json.loads(tm.create("x"))["id"]
        tm.update(tid, status="deleted")
        with pytest.raises(ValueError):
            tm.get(tid)


class TestDependencies:
    def test_add_blocked_by(self, tmp_db):
        tm = TaskManager()
        a = json.loads(tm.create("A"))["id"]
        b = json.loads(tm.create("B"))["id"]
        tm.update(b, add_blocked_by=[a])
        assert json.loads(tm.get(b))["blockedBy"] == [a]

    def test_remove_blocked_by(self, tmp_db):
        tm = TaskManager()
        a = json.loads(tm.create("A"))["id"]
        b = json.loads(tm.create("B"))["id"]
        c = json.loads(tm.create("C"))["id"]
        tm.update(c, add_blocked_by=[a, b])
        assert sorted(json.loads(tm.get(c))["blockedBy"]) == sorted([a, b])
        tm.update(c, remove_blocked_by=[a])
        assert json.loads(tm.get(c))["blockedBy"] == [b]

    def test_completion_unlocks_downstream(self, tmp_db):
        tm = TaskManager()
        a = json.loads(tm.create("A"))["id"]
        b = json.loads(tm.create("B"))["id"]
        tm.update(b, add_blocked_by=[a])
        tm.update(a, status="completed")
        assert json.loads(tm.get(b))["blockedBy"] == []


class TestClaim:
    def test_claim_sets_owner(self, tmp_db):
        tm = TaskManager()
        tid = json.loads(tm.create("x"))["id"]
        tm.claim(tid, "alice")
        t = json.loads(tm.get(tid))
        assert t["owner"] == "alice"
        assert t["status"] == "in_progress"

    def test_try_claim_first_wins(self, tmp_db):
        tm = TaskManager()
        tid = json.loads(tm.create("x"))["id"]
        assert tm.try_claim(tid, "alice") is True
        # 第二次试图认领同一任务，因为已经 in_progress 不再 pending
        assert tm.try_claim(tid, "bob") is False
        # 状态没被 bob 覆盖
        assert json.loads(tm.get(tid))["owner"] == "alice"


class TestConcurrency:
    def test_concurrent_create_no_id_collision(self, tmp_db):
        tm = TaskManager()
        results = []
        lock = threading.Lock()

        def worker(i):
            r = tm.create(f"t-{i}")
            with lock:
                results.append(json.loads(r)["id"])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        assert len(set(results)) == 10, f"duplicate IDs: {sorted(results)}"

    def test_concurrent_try_claim_only_one_wins(self, tmp_db):
        tm = TaskManager()
        tid = json.loads(tm.create("hot"))["id"]
        winners = []
        lock = threading.Lock()

        def worker(i):
            if tm.try_claim(tid, f"w-{i}"):
                with lock:
                    winners.append(i)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(winners) == 1

    def test_deleted_id_not_reused(self, tmp_db):
        tm = TaskManager()
        a = json.loads(tm.create("a"))["id"]
        tm.update(a, status="deleted")
        b = json.loads(tm.create("b"))["id"]
        assert b > a


class TestFindClaimable:
    def test_filters_correctly(self, tmp_db):
        tm = TaskManager()
        a = json.loads(tm.create("a"))["id"]
        b = json.loads(tm.create("b"))["id"]
        c = json.loads(tm.create("c"))["id"]
        tm.claim(a, "owner")  # a 不可认领（已 in_progress + 有 owner）
        tm.update(c, add_blocked_by=[b])  # c 被 b 阻塞，不可认领

        claimable = tm.find_claimable()
        ids = [t["id"] for t in claimable]
        assert b in ids
        assert a not in ids
        assert c not in ids


class TestListAll:
    def test_empty(self, tmp_db):
        tm = TaskManager()
        assert tm.list_all() == "No tasks."

    def test_renders_with_owner_and_blocks(self, tmp_db):
        tm = TaskManager()
        a = json.loads(tm.create("A"))["id"]
        b = json.loads(tm.create("B"))["id"]
        tm.claim(a, "alice")
        tm.update(b, add_blocked_by=[a])
        out = tm.list_all()
        assert "@alice" in out
        assert f"blocked by: [{a}]" in out
