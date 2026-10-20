"""端点配置完整落盘、字段独立保存及真实进程重启回读。"""
import copy
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

MODULE_PATH = os.environ.get("POOL_TEST_SOURCE", str(Path(__file__).resolve().parents[1] / "api_pool_server.py"))


def load_module(cwd):
    previous = os.getcwd()
    os.chdir(cwd)
    try:
        name = f"pool_config_test_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous)


def config():
    return {"api_endpoints": [{
        "id": "ep1", "name": "saved-endpoint", "site_id": "site1",
        "base_url": "http://127.0.0.1:1/v1", "api_key": "test-complete-key-1234",
        "model": "glm-5.3-flash", "in_pool": False,
        "priority": 999, "pool_groups": [], "health_mode": "models",
    }], "pool_group_defs": [{"name": "main", "type": "mixed", "model": "api-pool"}]}


class EndpointConfigFieldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "api_config.json"
        self.path.write_text(json.dumps(config()))
        self.module = load_module(self.tmp.name)
        self.module.CONFIG_FILE = str(self.path)
        self.ep = self.module.pool._endpoints[0]

    def saved(self):
        return json.loads(self.path.read_text())["api_endpoints"][0]

    def test_all_config_fields_are_saved_from_endpoint(self):
        self.module._sync_to_config()
        expected = {name for name in self.module.Endpoint.__dataclass_fields__ if not name.startswith("_")}
        self.assertEqual(set(self.saved()), expected | {"manual_unlock_required"})
        for name in expected:
            self.assertEqual(self.saved()[name], getattr(self.ep, name))

    def test_future_field_saves_without_display_export(self):
        original = self.module.Endpoint.__dataclass_fields__
        with mock.patch.object(self.module.Endpoint, "__dataclass_fields__", {**original, "future_config": object()}):
            self.ep.future_config = {"setting": [0, False, ""]}
            # 保存直接读取端点对象，展示接口的字段集合保持现状。
            with mock.patch.object(self.module.pool, "list_endpoints", side_effect=AssertionError("display export used")):
                self.module._sync_to_config()
        self.assertEqual(self.saved()["future_config"], {"setting": [0, False, ""]})

    def test_runtime_stays_separate_and_complete_key_is_saved(self):
        self.ep._fail_count = 7
        self.ep._cooldown_until = time.time() + 300
        self.ep._manual_unlock_required = True
        self.module._sync_to_config()
        saved = self.saved()
        self.assertFalse(any(name.startswith("_") for name in saved))
        self.assertNotIn("api_key_full", saved)
        self.assertEqual(saved["api_key"], "test-complete-key-1234")
        self.assertTrue(saved["manual_unlock_required"])

    def test_partial_update_keeps_other_fields_and_zero_values(self):
        before = copy.deepcopy(self.ep.__dict__)
        self.module.pool.update_endpoint("ep1", {"reasoning_effort_map": {"medium": "high", "xhigh": "max"}})
        self.module._sync_to_config()
        saved = self.saved()
        self.assertEqual(saved["reasoning_effort_map"], {"medium": "high", "xhigh": "max"})
        for name in ("name", "api_key", "model", "site_id", "priority", "pool_groups"):
            self.assertEqual(saved[name], before[name])
        self.module.pool.update_endpoint("ep1", {"reasoning_effort_map": None, "thinking_disabled": False, "stream_max_duration": 0})
        self.module._sync_to_config()
        self.assertIn("reasoning_effort_map", self.saved())
        self.assertIsNone(self.saved()["reasoning_effort_map"])
        self.assertFalse(self.saved()["thinking_disabled"])
        self.assertEqual(self.saved()["stream_max_duration"], 0)

    def test_loaded_map_round_trip(self):
        self.module.pool.update_endpoint("ep1", {"reasoning_effort_map": {"medium": "high"}})
        self.module._sync_to_config()
        reloaded = load_module(self.tmp.name)
        self.assertEqual(reloaded.pool._endpoints[0].reasoning_effort_map, {"medium": "high"})
        self.assertEqual(reloaded.pool._endpoints[0].id, "ep1")

    def test_real_http_save_disk_and_process_restart(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, API_POOL_PORT=str(port), PYTHONUNBUFFERED="1")
        log = subprocess.DEVNULL
        proc = None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def request(method, path, body=None):
            payload = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=payload, method=method, headers={"Content-Type": "application/json"})
            with opener.open(req, timeout=3) as response:
                return json.load(response)

        def start():
            process = subprocess.Popen([sys.executable, MODULE_PATH], cwd=self.tmp.name, env=env, stdout=log, stderr=log)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail("isolated server exited")
                try:
                    request("GET", "/api/endpoints")
                    return process
                except (OSError, ValueError):
                    time.sleep(0.1)
            process.terminate()
            process.wait(timeout=10)
            self.fail("isolated server readiness timeout")

        def stop(process):
            process.terminate()
            process.wait(timeout=10)

        try:
            proc = start()
            wanted = {"medium": "high", "xhigh": "max"}
            self.assertTrue(request("PUT", "/api/endpoints/ep1", {"reasoning_effort_map": wanted})["ok"])
            self.assertEqual(self.saved().get("reasoning_effort_map"), wanted)
            stop(proc)
            proc = start()
            endpoint = request("GET", "/api/endpoints")[0]
            self.assertEqual(endpoint["reasoning_effort_map"], wanted)
            self.assertEqual(endpoint["name"], "saved-endpoint")
            self.assertEqual(endpoint["api_key_full"], "test-complete-key-1234")
            self.assertTrue(request("PUT", "/api/endpoints/ep1", {"reasoning_effort_map": None})["ok"])
            stop(proc)
            proc = start()
            self.assertIsNone(request("GET", "/api/endpoints")[0]["reasoning_effort_map"])
        finally:
            if proc is not None and proc.poll() is None:
                stop(proc)


if __name__ == "__main__":
    unittest.main()
