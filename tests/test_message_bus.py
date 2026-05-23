"""测试 managers/message_bus（SQLite 后端）"""
import threading
import time

from managers.message_bus import MessageBus


class TestSendReceive:
    def test_send_then_read(self, tmp_db):
        bus = MessageBus()
        bus.send("alice", "bob", "hello")
        msgs = bus.read_inbox("bob")
        assert len(msgs) == 1
        assert msgs[0]["from"] == "alice"
        assert msgs[0]["content"] == "hello"
        assert msgs[0]["type"] == "message"

    def test_read_drains_inbox(self, tmp_db):
        bus = MessageBus()
        bus.send("a", "b", "msg1")
        bus.send("a", "b", "msg2")
        first = bus.read_inbox("b")
        assert len(first) == 2
        # 第二次读应为空
        second = bus.read_inbox("b")
        assert second == []

    def test_read_other_inbox_does_not_affect(self, tmp_db):
        bus = MessageBus()
        bus.send("a", "bob", "to-bob")
        bus.send("a", "alice", "to-alice")
        bob_msgs = bus.read_inbox("bob")
        assert len(bob_msgs) == 1
        # alice 的收件箱还在
        alice_msgs = bus.read_inbox("alice")
        assert len(alice_msgs) == 1

    def test_msg_type_propagates(self, tmp_db):
        bus = MessageBus()
        bus.send("lead", "alice", "shut down please", msg_type="shutdown_request")
        msgs = bus.read_inbox("alice")
        assert msgs[0]["type"] == "shutdown_request"


class TestExtraSecurity:
    def test_extra_cannot_override_sender(self, tmp_db):
        bus = MessageBus()
        # 试图通过 extra 伪造 sender —— SQLite 版应阻止
        bus.send("real-sender", "victim", "msg", extra={"from": "fake-sender"})
        msgs = bus.read_inbox("victim")
        assert msgs[0]["from"] == "real-sender"

    def test_extra_carries_auxiliary_fields(self, tmp_db):
        bus = MessageBus()
        bus.send("a", "b", "msg", extra={"request_id": "abc123"})
        msgs = bus.read_inbox("b")
        assert msgs[0].get("request_id") == "abc123"


class TestBroadcast:
    def test_excludes_sender(self, tmp_db):
        bus = MessageBus()
        bus.broadcast("alice", "hi all", ["alice", "bob", "carol"])
        assert bus.read_inbox("alice") == []
        assert len(bus.read_inbox("bob")) == 1
        assert len(bus.read_inbox("carol")) == 1


class TestConcurrency:
    def test_no_message_loss_under_concurrent_send_and_read(self, tmp_db):
        bus = MessageBus()
        target = "recv"
        N = 200
        stop = threading.Event()
        sent = [0]
        sl = threading.Lock()

        def sender():
            i = 0
            while not stop.is_set() and i < N:
                bus.send("s", target, f"m-{i}")
                with sl:
                    sent[0] += 1
                i += 1

        received = []
        rl = threading.Lock()

        def reader():
            while not stop.is_set():
                got = bus.read_inbox(target)
                if got:
                    with rl:
                        received.extend(m["content"] for m in got)
                time.sleep(0.001)

        s = threading.Thread(target=sender)
        r = threading.Thread(target=reader)
        s.start()
        r.start()
        s.join()
        # 让 reader 多读几轮排空
        time.sleep(0.2)
        stop.set()
        r.join()
        received.extend(m["content"] for m in bus.read_inbox(target))

        assert len(received) == sent[0], f"loss: sent={sent[0]} recv={len(received)}"
        assert len(set(received)) == len(received), "duplicates detected"
