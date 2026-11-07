"""对话日志 prompt 两档保留回归（2026-10-08）。

契约：超出全文窗口（FULL_PROMPT_HOURS）且长度超过预览阈值的行，滚动截断为
「首 PREVIEW_HEAD + 标记 + 尾 PREVIEW_TAIL」；窗口内与超短行不动；重复执行幂等。
"""
import importlib.util
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SOURCE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api_pool_server.py")


def load_module():
    spec = importlib.util.spec_from_file_location("chat_log_trim_pool", SOURCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # ChatLogger 在模块导入时实例化并启动两个守护线程；测试内不启动任何线程
    with mock.patch.object(threading.Thread, "start"):
        spec.loader.exec_module(module)
    return module


class PromptTrimTests(unittest.TestCase):
    def setUp(self):
        self.m = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with mock.patch.object(threading.Thread, "start"):
            self.logger = self.m.ChatLogger(db_path=os.path.join(self.tmp.name, "chat_logs.db"))

    def insert(self, prompt, hours_ago):
        stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
        conn = self.logger._connect()
        conn.execute(
            "INSERT INTO chat_logs (timestamp, endpoint_name, model, prompt, completion) VALUES (?,?,?,?,?)",
            (stamp, "ep", "m", prompt, "ok"),
        )
        conn.commit()
        conn.close()

    def rows(self):
        conn = self.logger._connect()
        out = {r[0]: r for r in conn.execute(
            "SELECT id, prompt, COALESCE(prompt_trimmed,0) FROM chat_logs ORDER BY id")}
        conn.close()
        return out

    def test_migration_adds_column_and_index(self):
        conn = self.logger._connect()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_logs)")}
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='chat_logs'")}
        conn.close()
        self.assertIn("prompt_trimmed", cols)
        self.assertIn("idx_chat_logs_timestamp", indexes)

    def test_two_tier_trim_is_scoped_and_idempotent(self):
        head = "H" * 9000
        tail = "T" * 9000
        long_prompt = head + ("M" * 40000) + tail
        self.insert(long_prompt, hours_ago=48)
        self.insert(long_prompt, hours_ago=1)
        self.insert("S" * 5000, hours_ago=48)

        trimmed = self.logger.trim_old_prompts()
        self.assertEqual(trimmed, 1, "只有超出窗口且超长的行应被截断")

        values = list(self.rows().values())
        oldest, recent, short = values
        self.assertEqual(oldest[2], 1)
        self.assertTrue(oldest[1].startswith("H" * 100))
        self.assertTrue(oldest[1].endswith("T" * 100))
        self.assertIn("已截断", oldest[1])
        self.assertLess(len(oldest[1]), len(long_prompt))
        self.assertEqual(recent[2], 0)
        self.assertEqual(recent[1], long_prompt)
        self.assertEqual(short[1], "S" * 5000)

        self.assertEqual(self.logger.trim_old_prompts(), 0, "重复执行必须幂等")

    def test_new_log_writes_while_trim_candidates_are_read(self):
        """A slow scan of old prompts must leave both write locks available."""
        self.insert("X" * 30000, hours_ago=48)
        reading = threading.Event()
        release = threading.Event()
        written = threading.Event()
        original_connect = self.logger._connect
        trims = []

        def connect(*args, **kwargs):
            conn = original_connect(*args, **kwargs)

            def trace(sql):
                # Pause the old UPDATE or the new candidate SELECT at the same
                # slow-read boundary, while a real SQLite insert runs alongside.
                if ("SELECT id" in sql and "prompt_trimmed" in sql
                        and threading.current_thread().name == "trim-test"):
                    reading.set()
                    release.wait(3)

            conn.set_trace_callback(trace)
            return conn

        def write():
            self.logger.add_log("live", "m", "new", "ok", 1, 1, "main")
            self.logger._write_batch([self.logger._log_queue.get_nowait()])
            written.set()

        with mock.patch.object(self.logger, "_connect", side_effect=connect):
            trimmer = threading.Thread(
                target=lambda: trims.append(self.logger.trim_old_prompts()), name="trim-test")
            writer = threading.Thread(target=write)
            trimmer.start()
            try:
                self.assertTrue(reading.wait(2), "trim did not reach its candidate read")
                writer.start()
                self.assertTrue(written.wait(1), "old-prompt scan blocked a new log insert")
                self.assertFalse(release.is_set(), "writer only ran after the scan ended")
            finally:
                release.set()
                trimmer.join(4)
                if writer.ident is not None:
                    writer.join(4)
        self.assertFalse(trimmer.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(trims, [1])
        self.assertEqual(len(self.rows()), 2)

    def test_queued_log_keeps_event_time_after_delayed_insert(self):
        event_time = datetime(2026, 11, 7, 13, 2, 48, tzinfo=timezone.utc)
        with mock.patch.object(self.m.time, "time", return_value=event_time.timestamp()):
            self.logger.add_log("live", "m", "p", "c", 102435, 12831, "telegram")
        # SQLite's current clock is intentionally different at flush time.
        self.logger._write_batch([self.logger._log_queue.get_nowait()])
        conn = self.logger._connect()
        try:
            row = conn.execute(
                "SELECT timestamp, pool_group, total_tokens, latency_ms FROM chat_logs"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ("2026-11-07 13:02:48", "telegram", 102435, 12831))

    def test_trim_is_batched_and_resumable(self):
        """单次调用受批数上限约束：积压分多次调用清完，不出现一次性重写整库。"""
        self.logger.TRIM_BATCH_ROWS = 2
        self.logger.TRIM_MAX_BATCHES = 1
        self.logger.TRIM_SLEEP_SECONDS = 0
        for _ in range(3):
            self.insert("X" * 30000, hours_ago=48)
        self.assertEqual(self.logger.trim_old_prompts(), 2, "单次调用只清一批")
        self.assertEqual(self.logger.trim_old_prompts(), 1, "剩余积压由下一次调用继续")
        self.assertEqual(self.logger.trim_old_prompts(), 0)


if __name__ == "__main__":
    unittest.main()
