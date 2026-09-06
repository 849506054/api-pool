"""ChatLogger 批量写 + 读路径无锁行为测试（2026-09-01）。

覆盖：批量写落库（含跨批）、读不被写阻塞、队列满降级直写、滚动清理、清空、按 id 读。
"""
import importlib.util
import os
import queue
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "api_pool_server.py")


def load_module():
    spec = importlib.util.spec_from_file_location("api_pool_chat_logger_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = load_module()


class ChatLoggerBatchingTests(unittest.TestCase):
    def _make_logger(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return module.ChatLogger(db_path=os.path.join(td.name, "chat_logs.db"))

    def _wait_until(self, cond, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_add_log_eventually_persists(self):
        lg = self._make_logger()
        lg.add_log("ep1", "model-x", "hello", "world", 10, 123, "main", 5, 0)
        self.assertTrue(self._wait_until(lambda: lg.get_logs(limit=10, detail=False)["total"] >= 1))
        logs = lg.get_logs(limit=10, detail=False)["logs"]
        self.assertEqual(logs[0]["endpoint_name"], "ep1")
        self.assertEqual(logs[0]["model"], "model-x")
        self.assertEqual(logs[0]["total_tokens"], 10)
        self.assertEqual(logs[0]["latency_ms"], 123)
        self.assertEqual(logs[0]["pool_group"], "main")
        self.assertIsNone(logs[0]["reasoning_tokens"])

    def test_batch_write_persists_more_than_batch_size(self):
        lg = self._make_logger()
        n = 60  # > BATCH_SIZE(50)，验证跨批全部落库
        for i in range(n):
            lg.add_log(f"ep{i % 3}", "m", f"p{i}", f"c{i}", i, 1, "main", i, 0)
        self.assertTrue(self._wait_until(lambda: lg.get_logs(limit=200, detail=False)["total"] >= n, timeout=10))
        self.assertEqual(lg.get_logs(limit=200, detail=False)["total"], n)

    def test_reads_not_blocked_by_writes(self):
        lg = self._make_logger()
        stop = threading.Event()
        errors = []

        def writer():
            # 有界洪水（200 条 < 队列 512）：只走队列路径，不触发降级线程，避免测试后残留写
            for i in range(200):
                if stop.is_set():
                    break
                lg.add_log("w", "m", "p", "c", i, 1, "main", i, 0)

        def reader():
            while not stop.is_set():
                try:
                    lg.get_logs(limit=20, detail=False)
                    lg.get_log_by_id(1)
                except Exception as e:  # noqa: BLE001 — 测试断言读路径不抛异常
                    errors.append(e)

        tw = threading.Thread(target=writer, daemon=True)
        tr = threading.Thread(target=reader, daemon=True)
        tw.start()
        tr.start()
        time.sleep(1.5)
        stop.set()
        tw.join(timeout=3)
        tr.join(timeout=3)
        self.assertEqual(errors, [])
        self.assertTrue(self._wait_until(lambda: lg.get_logs(limit=10, detail=False)["total"] > 0))

    def test_prune_old_logs_deletes_only_old_rows(self):
        lg = self._make_logger()
        with sqlite3.connect(lg.db_path) as conn:
            conn.execute(
                "INSERT INTO chat_logs (endpoint_name, model, prompt, completion, total_tokens, latency_ms, timestamp) VALUES (?, ?, ?, ?, ?, ?, datetime('now', '-31 days'))",
                ("old", "m", "p", "c", 1, 1),
            )
            conn.commit()
        lg.add_log("new", "m", "p", "c", 2, 1, "main", 2, 0)

        def new_present():
            return any(r["endpoint_name"] == "new" for r in lg.get_logs(limit=10, detail=False)["logs"])

        self.assertTrue(self._wait_until(new_present, timeout=10))
        lg.prune_old_logs()
        logs = lg.get_logs(limit=10, detail=False)["logs"]
        # 终态断言：旧行被清、新行保留（启动期 retention 首轮 prune 可能已删旧行，故不依赖返回值）
        self.assertTrue(any(r["endpoint_name"] == "new" for r in logs))
        self.assertTrue(all(r["endpoint_name"] != "old" for r in logs))

    def test_clear_logs(self):
        lg = self._make_logger()
        lg.add_log("a", "m", "p", "c", 1, 1, "main", 1, 0)
        self.assertTrue(self._wait_until(lambda: lg.get_logs(limit=10, detail=False)["total"] >= 1))
        lg.clear_logs()
        self.assertEqual(lg.get_logs(limit=10, detail=False)["total"], 0)

    def test_get_log_by_id_after_batch_write(self):
        lg = self._make_logger()
        lg.add_log("ep9", "m", "prompt-abc", "completion-xyz", 42, 7, "bg", 42, 0)
        self.assertTrue(self._wait_until(lambda: lg.get_logs(limit=10, detail=False)["total"] >= 1))
        row = lg.get_logs(limit=1, detail=True)["logs"][0]
        got = lg.get_log_by_id(row["id"])
        self.assertIsNotNone(got)
        self.assertEqual(got["prompt"], "prompt-abc")
        self.assertEqual(got["completion"], "completion-xyz")

    def test_reasoning_tokens_roundtrip(self):
        lg = self._make_logger()
        lg.add_log("ep", "m", "p", "c", 10, 1, "main", 4, 0, 7)
        self.assertTrue(self._wait_until(lambda: lg.get_logs(limit=10, detail=False)["total"] >= 1))
        row = lg.get_logs(limit=1, detail=False)["logs"][0]
        self.assertEqual(row["reasoning_tokens"], 7)

    def test_queue_full_fallback_persists(self):
        lg = self._make_logger()

        class _FullQueue:
            """put_nowait 恒抛 Full，模拟队列打满；get 阻塞（本测试不走批量写线程）。"""

            def put_nowait(self, item):
                raise queue.Full

            def get(self):
                while True:
                    time.sleep(60)

            def get_nowait(self):
                raise queue.Empty

            def qsize(self):
                return 1

        lg._log_queue = _FullQueue()
        lg.add_log("fallback", "m", "p", "c", 1, 1, "main", 1, 0)

        def fallback_present():
            return any(r["endpoint_name"] == "fallback" for r in lg.get_logs(limit=10, detail=False)["logs"])

        self.assertTrue(self._wait_until(fallback_present, timeout=10))


if __name__ == "__main__":
    unittest.main()
