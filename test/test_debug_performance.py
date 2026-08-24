import importlib.util
import os
import tempfile
import unittest
from unittest import mock


MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_debug_perf_test", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(old_cwd)


class DebugPerformanceTests(unittest.TestCase):
    def test_debug_defaults_off_without_environment_variable(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("API_POOL_DEBUG", None)
                module = load_module(tmp_path)
        self.assertFalse(module._DEBUG_LOGGING)

    def test_try_endpoint_does_not_scan_cf_diagnostics_on_normal_path(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            module._set_debug_logging(False)
            pool = module.APIPool()
            ep = module.Endpoint(name="test-normal", base_url="http://127.0.0.1:1/v1", api_key="x", model="m")
            pool._cf_probe_count = mock.Mock(side_effect=AssertionError("diagnostic scan entered"))
            pool._cf_probe_paths = mock.Mock(side_effect=AssertionError("diagnostic scan entered"))
            with mock.patch.object(module.urllib.request, "urlopen", side_effect=OSError("offline")):
                result, error = pool._try_endpoint(ep, {"model": "m", "messages": []}, 1, log_usage=False, force_no_retry=True)
        self.assertIsNone(result)
        self.assertIn("连接/超时错误", error)
        pool._cf_probe_count.assert_not_called()
        pool._cf_probe_paths.assert_not_called()
        self.assertFalse(hasattr(pool, "_last_cf_diag"))

    def test_cf_diagnostic_is_local_and_only_built_when_called(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool()
            hyphen_form = "role" + "-" + "play"
            underscore_form = "role" + "_" + "play"
            first = {"messages": [{"content": hyphen_form}], "tools": []}
            second = {"messages": [{"content": underscore_form}], "tools": [{"name": "x"}]}
            first_diag = pool._build_cf_diag(first)
            second_diag = pool._build_cf_diag(second)
        self.assertEqual(first_diag["hyphen"], 1)
        self.assertEqual(first_diag["underscore"], 0)
        self.assertEqual(second_diag["hyphen"], 0)
        self.assertEqual(second_diag["underscore"], 1)
        self.assertEqual(second_diag["tools"], 1)
        self.assertFalse(hasattr(pool, "_last_cf_diag"))

    def test_filter_segment_log_only_when_debug_enabled(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            logs = []
            module.sys_log = lambda msg, level="INFO": logs.append((msg, level))
            module.pool.chat = lambda messages, extra_payload=None: {
                "model": "m",
                "choices": [{"message": {"content": "ok"}}],
            }
            body = {"model": "m", "messages": [{"role": "user", "content": "hello"}], "stream": False}

            module._set_debug_logging(False)
            code, _, _ = module.api_handler("POST", "/v1/chat/completions", body)
            self.assertEqual(code, 200)
            self.assertFalse(any("[DEBUG] 分段 filter_ms=" in msg for msg, _ in logs))

            logs.clear()
            module._set_debug_logging(True)
            code, _, _ = module.api_handler("POST", "/v1/chat/completions", body)
            self.assertEqual(code, 200)
            self.assertTrue(any("[DEBUG] 分段 filter_ms=" in msg for msg, _ in logs))


if __name__ == "__main__":
    unittest.main()
