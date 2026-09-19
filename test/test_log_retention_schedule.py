"""日志滚动清理：固定时刻检测 + 后台可配置（log_retention）回归（2026-09-19）。

原先清理线程按固定间隔（每小时）跑；改为每日本地 cleanup_at 时刻跑一次（当天错过补跑），
保留天数与清理时刻从 api_config.json 顶层键 `log_retention` 读取，无前端 UI 入口。

锁定：
- load_log_retention 默认 / 读取 / 非法回落；
- _log_cleanup_due 到点判定（未到点不跑、到点跑、同日只跑一次、跨天再跑）；
- save_config 透传 log_retention（UI 编辑端点时不得丢失该键）。
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_log_retention_test_{os.getpid()}_{id(threading.current_thread())}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        module.__dict__["CONFIG_FILE"] = os.path.join(tmp_path, "api_config.json")
        return module
    finally:
        os.chdir(previous_cwd)


def write_config(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def at(y, mo, d, h, mi):
    return time.struct_time((y, mo, d, h, mi, 0, 0, 0, -1))


class LogRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.module = load_module(self.tmp)

    def test_defaults_when_key_absent(self):
        write_config(self.module.CONFIG_FILE, {"api_endpoints": []})
        self.assertEqual(self.module.load_log_retention(), {"days": 7, "cleanup_at": "03:00"})

    def test_reads_days_and_cleanup_at(self):
        write_config(self.module.CONFIG_FILE, {"log_retention": {"days": 14, "cleanup_at": "2:30"}})
        self.assertEqual(self.module.load_log_retention(), {"days": 14, "cleanup_at": "02:30"})

    def test_invalid_values_fall_back(self):
        write_config(self.module.CONFIG_FILE, {"log_retention": {"days": 0, "cleanup_at": "25:99"}})
        self.assertEqual(self.module.load_log_retention(), {"days": 7, "cleanup_at": "03:00"})

    def test_cleanup_due_trigger(self):
        due = self.module._log_cleanup_due
        self.assertFalse(due(at(2026, 9, 19, 2, 59), "03:00", ""))       # 未到点
        self.assertTrue(due(at(2026, 9, 19, 3, 0), "03:00", ""))        # 正点
        self.assertTrue(due(at(2026, 9, 19, 10, 0), "03:00", ""))       # 当天补跑
        self.assertFalse(due(at(2026, 9, 19, 10, 1), "03:00", "2026-09-19"))  # 同日已跑
        self.assertTrue(due(at(2026, 9, 20, 3, 0), "03:00", "2026-09-19"))    # 跨天再跑

    def test_save_config_round_trips_log_retention(self):
        cfg = {"days": 3, "cleanup_at": "01:15"}
        self.module.save_config([], log_retention=cfg)
        with open(self.module.CONFIG_FILE, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["log_retention"], cfg)
        self.assertEqual(self.module.load_log_retention(), cfg)


if __name__ == "__main__":
    unittest.main()
