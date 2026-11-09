"""token_stats 写路径与测试端点重试口径（2026-11-09）。

覆盖：
- `endpoint_name` 建索引（改名不再全表扫描）；
- 端点改名分批提交，单批行数受限且改名完整；
- 写锁冲突瞬时命中时按短退避重试，重试耗尽才记 WARN；
- 端点卡片 🧪 测试单次出站（force_no_retry=True）。
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
import unittest

MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "api_pool_server.py")

# 导入 server 会立刻创建 TokenTracker；先把库目录指到临时目录，避免落到生产库路径。
DB_DIR = tempfile.mkdtemp(prefix="apipool-token-stats-")
os.environ["API_POOL_DB_DIR"] = DB_DIR


def load_module():
    spec = importlib.util.spec_from_file_location("api_pool_token_stats_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._client_baseline.update({"User-Agent": "pytest-client/1.0"})
    return module


module = load_module()


class TokenStatsWriteTests(unittest.TestCase):
    def _make_tracker(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return module.TokenTracker(db_path=os.path.join(td.name, "token_stats.db"))

    def _wait_until(self, cond, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def _seed(self, tracker, endpoint_name, count):
        with tracker._connect() as conn:
            conn.executemany(
                "INSERT INTO token_usage (endpoint_name, model, prompt_tokens, completion_tokens, total_tokens, cached_tokens) VALUES (?, ?, ?, ?, ?, ?)",
                [(endpoint_name, "m", 1, 2, 3, 0)] * count,
            )

    def test_endpoint_name_index_created(self):
        tracker = self._make_tracker()
        with tracker._connect() as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_endpoint_name", names)

    def test_rename_is_chunked_and_complete(self):
        tracker = self._make_tracker()
        tracker.RENAME_BATCH_SIZE = 100
        self._seed(tracker, "old-ep", 1250)
        batches = []
        original = tracker._rename_batch

        def spy(old, new):
            moved = original(old, new)
            batches.append(moved)
            return moved

        tracker._rename_batch = spy
        tracker.rename_endpoint("old-ep", "new-ep")
        with tracker._connect() as conn:
            old_left = conn.execute("SELECT COUNT(*) FROM token_usage WHERE endpoint_name='old-ep'").fetchone()[0]
            new_rows = conn.execute("SELECT COUNT(*) FROM token_usage WHERE endpoint_name='new-ep'").fetchone()[0]
        self.assertEqual(old_left, 0)
        self.assertEqual(new_rows, 1250)
        self.assertGreater(len(batches), 1)
        self.assertTrue(all(0 < n <= 100 for n in batches[:-1]))

    def test_locked_write_is_retried(self):
        tracker = self._make_tracker()
        calls = []

        def flaky(old, new):
            calls.append(1)
            if len(calls) < 3:
                raise sqlite3.OperationalError("database is locked")
            return 0

        tracker._rename_batch = flaky
        tracker.rename_endpoint("a", "b")
        self.assertEqual(len(calls), 3)

    def test_locked_write_logs_after_retries_exhausted(self):
        tracker = self._make_tracker()
        tracker.RENAME_BATCH_SIZE = 10
        warnings = []
        original_log = module.sys_log
        module.sys_log = lambda msg, level="INFO": warnings.append((level, msg))
        self.addCleanup(setattr, module, "sys_log", original_log)

        def always_locked(old, new):
            raise sqlite3.OperationalError("database is locked")

        tracker._rename_batch = always_locked
        tracker.rename_endpoint("a", "b")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0][0], "WARN")
        self.assertIn("重命名端点统计数据失败", warnings[0][1])

    def test_non_lock_error_is_not_retried(self):
        tracker = self._make_tracker()
        calls = []

        def broken(old, new):
            calls.append(1)
            raise sqlite3.OperationalError("no such table: token_usage")

        tracker._rename_batch = broken
        tracker.rename_endpoint("a", "b")
        self.assertEqual(len(calls), 1)

    def test_add_usage_persists(self):
        tracker = self._make_tracker()
        tracker.add_usage("ep1", "m1", 10, 20, 30, 5)
        self.assertTrue(self._wait_until(lambda: self._rows(tracker) == 1))
        with tracker._connect() as conn:
            row = conn.execute(
                "SELECT endpoint_name, model, prompt_tokens, completion_tokens, total_tokens, cached_tokens FROM token_usage"
            ).fetchone()
        self.assertEqual(row, ("ep1", "m1", 10, 20, 30, 5))

    @staticmethod
    def _rows(tracker):
        with tracker._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0]


class TestEndpointNoRetryTests(unittest.TestCase):
    def test_test_endpoint_requests_single_attempt(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            previous_cwd = os.getcwd()
            os.chdir(tmp_path)
            try:
                ep = module.Endpoint(
                    id="t1", name="t1", base_url="http://127.0.0.1:1", api_key="sk-test",
                    model="mdl", priority=1, in_pool=True, use_proxy=False,
                    cooldown_minutes=5, pool_groups=["main"],
                )
                pool = module.APIPool([ep])
                captured = {}

                def fake_try(target, payload, timeout, **kwargs):
                    captured.update(kwargs)
                    return None, "HTTP 502: simulated"

                pool._try_endpoint = fake_try
                result, error = pool.test_endpoint(ep, message="hi")
                self.assertIsNone(result)
                self.assertIn("502", error)
                self.assertTrue(captured.get("force_no_retry"))
            finally:
                os.chdir(previous_cwd)


if __name__ == "__main__":
    unittest.main()
