"""端点级 thinking_disabled 配置保存与加载契约。

端点对象的配置字段自动进入配置文件，关闭思考开关在保存、加载后
保持设置值。默认态显式保存为 false。
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")

_load_seq = 0


@contextmanager
def loaded_module(tmp_path):
    global _load_seq
    _load_seq += 1
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_thinking_persist_{_load_seq}_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        os.chdir(previous_cwd)


def endpoint_config(thinking_disabled=None):
    entry = {
        "id": "ep1", "name": "ep1", "site_name": "", "site_id": "s1",
        "base_url": "http://127.0.0.1:1/v1", "api_key": "test", "model": "deepseek-flash",
        "priority": 1, "priority_by_group": {"main": 1}, "timeout": 10, "max_retries": 0,
        "enabled": True, "cooldown_minutes": 5, "use_proxy": False, "protocol": "openai",
        "extra_headers": {}, "default_headers": {}, "client_profile": "", "health_mode": "models",
        "billing_mode": "subscription", "manual_unlock_required": False, "is_vision": False,
        "in_pool": True, "pool_groups": ["main"],
    }
    if thinking_disabled is not None:
        entry["thinking_disabled"] = thinking_disabled
    return entry


def write_config(path, entry):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"api_endpoints": [entry],
                   "pool_group_defs": [{"name": "main", "type": "mixed", "model": "api-pool"},
                                       {"name": "vision", "type": "mixed", "model": "api-pool-vision"}],
                   "client_profiles": {}, "probe_client_profile": ""}, fh)


def read_saved(config_file):
    with open(config_file, encoding="utf-8") as fh:
        doc = json.load(fh)
    eps = doc.get("api_endpoints") or []
    return eps[0] if eps else {}


class ThinkingDisabledPersistenceTests(unittest.TestCase):
    def test_field_is_covered_by_derived_record(self):
        """thinking_disabled 由端点定义自动进入配置记录。"""
        with tempfile.TemporaryDirectory() as tmp:
            write_config(os.path.join(tmp, "api_config.json"), endpoint_config())
            with loaded_module(tmp) as module:
                ep = module.pool._endpoints[0]
                ep.thinking_disabled = True
                record = module._endpoint_config_record(ep)
                self.assertIn("thinking_disabled", record)
                self.assertTrue(record["thinking_disabled"])
                self.assertNotIn("_fail_count", record)

    def test_loaded_flag_survives_sync(self):
        """配置里带 thinking_disabled=true → 加载 → 写回仍是 true。"""
        with tempfile.TemporaryDirectory() as tmp:
            write_config(os.path.join(tmp, "api_config.json"), endpoint_config(True))
            with loaded_module(tmp) as module:
                ep = next(iter(module.pool.list_endpoints()))
                self.assertTrue(ep["thinking_disabled"])
                module._sync_to_config()
                self.assertIs(read_saved(module.CONFIG_FILE).get("thinking_disabled"), True)

    def test_runtime_set_persists(self):
        """运行期置上（等价于 PUT /api/endpoints/<id>）→ sync 落盘带该键。"""
        with tempfile.TemporaryDirectory() as tmp:
            write_config(os.path.join(tmp, "api_config.json"), endpoint_config())
            with loaded_module(tmp) as module:
                ep_id = next(iter(module.pool.list_endpoints()))["id"]
                module.pool.update_endpoint(ep_id, {"thinking_disabled": True})
                module._sync_to_config()
                self.assertIs(read_saved(module.CONFIG_FILE).get("thinking_disabled"), True)

    def test_default_is_false_not_absent_only_after_sync(self):
        """未设置时落盘为 false（与 preserved_thinking 同口径：默认态显式落 False）。"""
        with tempfile.TemporaryDirectory() as tmp:
            write_config(os.path.join(tmp, "api_config.json"), endpoint_config())
            with loaded_module(tmp) as module:
                module._sync_to_config()
                self.assertIs(read_saved(module.CONFIG_FILE).get("thinking_disabled"), False)


if __name__ == "__main__":
    unittest.main()
