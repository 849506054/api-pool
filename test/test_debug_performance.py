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

    def test_debug_trace_is_local_and_summarizes_failover(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            first = module.Endpoint(
                id="first", name="first", base_url="http://127.0.0.1:1", api_key="x",
                model="m", priority=1, max_retries=0, use_proxy=False, in_pool=True,
            )
            second = module.Endpoint(
                id="second", name="second", base_url="http://127.0.0.1:1", api_key="x",
                model="m", priority=2, max_retries=0, use_proxy=False, in_pool=True,
            )
            pool = module.APIPool([first, second])
            logs = []
            module.sys_log = lambda msg, level="INFO": logs.append((msg, level))

            def fake_try(ep, payload, timeout, **kwargs):
                if ep is first:
                    return None, "连接/超时错误: offline"
                return {"choices": [{"message": {"content": "ok"}}]}, ""

            pool._try_endpoint = fake_try
            pool._probe_endpoint = lambda ep: (True, "")
            module._set_debug_logging(True)
            result = pool.chat([{"role": "user", "content": "hello"}])

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        diagnostic = next(msg for msg, _ in logs if msg.startswith("[DEBUG] 请求诊断完成"))
        self.assertIn('switches=1', diagnostic)
        self.assertIn('"endpoint":"first","result":"error","kind":"timeout"', diagnostic)
        self.assertIn('"endpoint":"second","result":"success"', diagnostic)
        self.assertNotIn("hello", diagnostic)
        self.assertFalse(hasattr(pool, "_debug_trace"))

    def test_debug_trace_records_internal_retry(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = module.Endpoint(
                id="retry", name="retry", base_url="http://127.0.0.1:1", api_key="x",
                model="m", priority=1, max_retries=1, use_proxy=False, in_pool=True,
            )
            trace = []
            module._set_debug_logging(True)
            with mock.patch.object(module.urllib.request, "urlopen", side_effect=OSError("offline")):
                result, error = module.APIPool()._try_endpoint(
                    ep, {"model": "m", "messages": []}, 1, log_usage=False, debug_trace=trace
                )
            self.assertIsNone(result)
            self.assertIn("连接/超时错误", error)
            self.assertTrue(any(item.get("result") == "retry" for item in trace))

    def test_http_5xx_retry_waits_three_seconds_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = module.Endpoint(
                id="retry", name="retry", base_url="http://127.0.0.1:1", api_key="x",
                model="m", max_retries=1, use_proxy=True, in_pool=True,
            )
            response = mock.Mock()
            response.read.return_value = b'{"choices":[{"message":{"content":"ok"}}]}'
            response.__enter__ = mock.Mock(return_value=response)
            response.__exit__ = mock.Mock(return_value=False)
            http_error = module.urllib.error.HTTPError(
                "http://example/v1/chat/completions", 502, "bad gateway", {}, None,
            )
            logs = []
            module.sys_log = lambda msg, level="INFO": logs.append((msg, level))
            with mock.patch.object(module.urllib.request, "urlopen", side_effect=[http_error, response]), \
                    mock.patch.object(module.time, "sleep") as sleep:
                result, error = module.APIPool()._try_endpoint(
                    ep, {"model": "m", "messages": []}, 60, log_usage=False,
                )
            self.assertIsNotNone(result)
            self.assertEqual(error, "")
            sleep.assert_called_once_with(3)
            self.assertTrue(any("3 秒后进行第 1/1 次原端点重试（HTTP 502）" in msg for msg, _ in logs))

    def test_timeout_recent_success_skips_internal_replay(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = module.Endpoint(
                id="retry", name="retry", base_url="http://127.0.0.1:1", api_key="x",
                model="m", max_retries=1, use_proxy=True, in_pool=True,
            )
            ep._last_success_ts = module.time.time()
            with mock.patch.object(module.urllib.request, "urlopen", side_effect=TimeoutError("read timed out")) as call, \
                    mock.patch.object(module.time, "sleep") as sleep:
                result, error = module.APIPool()._try_endpoint(
                    ep, {"model": "m", "messages": []}, 60, log_usage=False,
                )
            self.assertIsNone(result)
            self.assertIn("recent endpoint success", error)
            self.assertEqual(call.call_count, 1)
            sleep.assert_not_called()

    def test_retry_skipped_when_request_budget_is_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = module.Endpoint(
                id="retry", name="retry", base_url="http://127.0.0.1:1", api_key="x",
                model="m", max_retries=1, use_proxy=True, in_pool=True,
            )
            http_error = module.urllib.error.HTTPError(
                "http://example/v1/chat/completions", 503, "unavailable", {}, None,
            )
            with mock.patch.object(module.urllib.request, "urlopen", side_effect=http_error) as call, \
                    mock.patch.object(module.time, "sleep") as sleep:
                result, error = module.APIPool()._try_endpoint(
                    ep, {"model": "m", "messages": []}, 60, log_usage=False,
                    request_deadline=module.time.time() + 1,
                )
            self.assertIsNone(result)
            self.assertIn("request budget exhausted", error)
            self.assertEqual(call.call_count, 1)
            sleep.assert_not_called()

    def test_debug_disabled_does_not_build_trace_or_debug_log(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = module.Endpoint(
                id="only", name="only", base_url="http://127.0.0.1:1", api_key="x",
                model="m", priority=1, max_retries=0, use_proxy=False, in_pool=True,
            )
            pool = module.APIPool([ep])
            logs = []
            module.sys_log = lambda msg, level="INFO": logs.append((msg, level))
            pool._try_endpoint = lambda ep, payload, timeout, **kwargs: (
                {"choices": [{"message": {"content": "ok"}}]}, ""
            )
            module._set_debug_logging(False)
            pool.chat([{"role": "user", "content": "hello"}])

        self.assertFalse(any(msg.startswith("[DEBUG]") for msg, _ in logs))
        self.assertFalse(hasattr(pool, "_debug_trace"))


if __name__ == "__main__":
    unittest.main()
