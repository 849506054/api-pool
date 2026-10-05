"""组级日志显示级别（2026-10-05）：log_level 校验、默认态不落键、加载往返。"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_group_loglevel_test_{os.getpid()}_{id(threading.current_thread())}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        module.__dict__["CONFIG_FILE"] = os.path.join(tmp_path, "api_config.json")
        module.__dict__["RUNTIME_STATE_FILE"] = os.path.join(tmp_path, "api_runtime_state.json")
        return module
    finally:
        os.chdir(previous_cwd)


class GroupLogLevelTests(unittest.TestCase):
    def make_pool(self, module):
        return module.APIPool([])

    def test_create_default_all_does_not_store_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool = self.make_pool(m)
            ok, msg = pool.create_group("bg", "mixed", "")
            self.assertTrue(ok, msg)
            self.assertNotIn("log_level", pool._group_defs["bg"])

    def test_create_with_level_and_invalid_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool = self.make_pool(m)
            ok, _ = pool.create_group("bg", "mixed", "", 0, 0, "error")
            self.assertTrue(ok)
            self.assertEqual(pool._group_defs["bg"]["log_level"], "error")
            ok, msg = pool.create_group("x", "mixed", "", 0, 0, "verbose")
            self.assertFalse(ok)
            self.assertIn("日志级别", msg)

    def test_update_set_and_clear_back_to_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool = self.make_pool(m)
            pool.create_group("bg", "mixed", "")
            ok, _ = pool.update_group("bg", {"log_level": "silent"})
            self.assertTrue(ok)
            self.assertEqual(pool._group_defs["bg"]["log_level"], "silent")
            ok, _ = pool.update_group("bg", {"log_level": "all"})
            self.assertTrue(ok)
            self.assertNotIn("log_level", pool._group_defs["bg"])
            ok, msg = pool.update_group("bg", {"log_level": "nope"})
            self.assertFalse(ok)
            self.assertIn("日志级别", msg)

    def test_main_group_log_level_editable(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool = self.make_pool(m)
            ok, msg = pool.update_group("main", {"log_level": "error"})
            self.assertTrue(ok, msg)
            self.assertEqual(pool._group_defs["main"]["log_level"], "error")

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            m.pool.create_group("bg", "mixed", "", 0, 0, "error")
            m._sync_to_config()
            saved = json.load(open(m.CONFIG_FILE, encoding="utf-8"))
            entry = next(d for d in saved["pool_group_defs"] if d["name"] == "bg")
            self.assertEqual(entry["log_level"], "error")
            # 重新加载：非法值当 all，合法值恢复
            pool2 = m.APIPool([])
            defs = pool2._load_group_defs(saved["pool_group_defs"])
            self.assertEqual(defs["bg"]["log_level"], "error")
            pool3 = m.APIPool([])
            bad = [dict(d) for d in saved["pool_group_defs"]]
            for d in bad:
                if d["name"] == "bg":
                    d["log_level"] = "garbage"
            defs3 = pool3._load_group_defs(bad)
            self.assertNotIn("log_level", defs3["bg"])


    def test_live_level_accepted_and_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool = self.make_pool(m)
            ok, msg = pool.create_group("bg", "mixed", "", 0, 0, "live")
            self.assertTrue(ok, msg)
            self.assertEqual(pool._group_defs["bg"]["log_level"], "live")
            self.assertIn("live", pool.GROUP_LOG_LEVELS)

    def test_ring_filtering_by_level(self):
        """写时过滤：silent 不进 ring，error 只进报错，all/live 照进；全局行不受影响。"""
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            m.pool.create_group("bg", "mixed", "")

            def ring():
                return [e["msg"] for e in m.sys_logger.get_logs_since(0) if e["msg"].startswith("T")]

            m.sys_log("T-all", group="bg")
            self.assertEqual(ring(), ["T-all"])
            m.pool.update_group("bg", {"log_level": "silent"})
            m.sys_log("T-silent", group="bg")
            self.assertEqual(ring(), ["T-all"])  # 不记
            m.pool.update_group("bg", {"log_level": "error"})
            m.sys_log("T-info", group="bg")
            self.assertEqual(ring(), ["T-all"])  # 非报错不记
            m.sys_log("T-warn", "WARN", group="bg")
            self.assertEqual(ring(), ["T-all", "T-warn"])
            m.pool.update_group("bg", {"log_level": "live"})
            m.sys_log("T-live", group="bg")
            self.assertEqual(ring(), ["T-all", "T-warn", "T-live"])
            m.pool.update_group("bg", {"log_level": "silent"})
            m.sys_log("T-global")
            self.assertEqual(ring(), ["T-all", "T-warn", "T-live", "T-global"])  # 无归属照记

    def test_thread_local_group_attribution(self):
        """请求路径的组归属：线程级上下文生效，显式 group 优先，清除后回无归属。"""
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            m.pool.create_group("bg", "mixed", "")
            m.set_log_group("bg")
            m.sys_log("T-tls")
            m.sys_log("T-explicit", group="other")
            m.clear_log_group()
            m.sys_log("T-plain")
            by_msg = {e["msg"]: e for e in m.sys_logger.get_logs_since(0)}
            self.assertEqual(by_msg["T-tls"]["group"], "bg")
            self.assertEqual(by_msg["T-explicit"]["group"], "other")
            self.assertIsNone(by_msg["T-plain"]["group"])

    def test_message_label_fallback_attribution(self):
        """背景线程无上下文时按消息兜底：`[组名]` 标签与 `组 'X'` 两种形态都归组。"""
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            m.pool.create_group("bg", "mixed", "")

            def ring():
                return [e["msg"] for e in m.sys_logger.get_logs_since(0) if e["msg"].startswith("F")]

            m.sys_log("F1 端点 '[bg]ep1' 后台探活异常")
            m.sys_log("F2 组 'bg' 轮转耗尽")
            self.assertEqual(ring(), ["F1 端点 '[bg]ep1' 后台探活异常", "F2 组 'bg' 轮转耗尽"])
            m.pool.update_group("bg", {"log_level": "silent"})
            m.sys_log("F3 端点 '[bg]ep1' 后台探活异常")
            m.sys_log("F4 组 'bg' 轮转耗尽")
            self.assertEqual(ring(), ["F1 端点 '[bg]ep1' 后台探活异常", "F2 组 'bg' 轮转耗尽"])  # silent 组不落 ring
            m.sys_log("F5 端点 '[unknown-grp]ep1' 探活异常")  # 未登记组名 → 不归属 → 照记
            self.assertEqual(len(ring()), 3)

    def test_chat_log_hidden_by_level(self):
        """对话日志隐藏判定：非 all 级别（error/live/silent）隐藏；未知组/空组名可见。"""
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            m.pool.create_group("bg", "mixed", "")
            self.assertFalse(m.chat_log_hidden("bg"))
            for lv in ("error", "live", "silent"):
                m.pool.update_group("bg", {"log_level": lv})
                self.assertTrue(m.chat_log_hidden("bg"), lv)
            self.assertFalse(m.chat_log_hidden("unknown-group"))
            self.assertFalse(m.chat_log_hidden(None))

    def test_chat_logs_query_excludes_hidden_groups(self):
        """/api/chat-logs 数据层排除：列表与 total 同步排除，detail 两个分支一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            logger = m.ChatLogger(db_path=os.path.join(tmp, "chat_logs.db"))
            conn = logger._connect()
            conn.executemany(
                "INSERT INTO chat_logs (endpoint_name, model, total_tokens, latency_ms, pool_group) VALUES (?, ?, ?, ?, ?)",
                [("e1", "mod", 10, 100, "agnes"), ("e2", "mod", 20, 200, "main"), ("e3", "mod", 30, 300, None)],
            )
            conn.commit()
            conn.close()
            plain = logger.get_logs(limit=10, offset=0, detail=False)
            self.assertEqual(plain["total"], 3)
            for detail in (False, True):
                got = logger.get_logs(limit=10, offset=0, detail=detail, exclude_groups=["agnes"])
                self.assertEqual(got["total"], 2, f"detail={detail} total 与列表同步排除")
                self.assertEqual({r["pool_group"] for r in got["logs"]}, {"main", None})
            # 空排除列表 = 不排除
            self.assertEqual(logger.get_logs(limit=10, offset=0, detail=False, exclude_groups=[])["total"], 3)


if __name__ == "__main__":
    unittest.main()
