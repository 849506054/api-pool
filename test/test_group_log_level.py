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


if __name__ == "__main__":
    unittest.main()
