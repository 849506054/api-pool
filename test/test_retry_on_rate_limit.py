"""端点级「限流优先重试」开关（retry_on_rate_limit）行为契约。

开关开启：限流类错误（429 / too_many_requests / exceeded rate limit）按 max_retries 在原端点
退避重试，耗尽才返回错误交给 _rotate；开关关闭（默认）保持「限流即轮转」，只发 1 次。
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")

RATE_LIMIT_BODY = (
    b'{"error":{"type":"too_many_requests","message":"Your requests to gpt-6-astra '
    b'for gpt-6-astra in eastus2 have exceeded rate limit."}}'
)


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_retry_on_rate_limit_{id(tmp_path)}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        module._client_baseline.update({"User-Agent": "pytest-client/1.0"})
        module.CONFIG_FILE = os.path.join(tmp_path, "api_config.json")
        module.RUNTIME_STATE_FILE = os.path.join(tmp_path, "api_runtime_state.json")
        return module
    finally:
        os.chdir(previous_cwd)


class FakeStreamResponse:
    """最小 SSE 流替身：首包预读只用 headers / readline / close。"""

    def __init__(self, lines):
        self._lines = list(lines)
        self.status = 200
        self.headers = {}
        self.closed = False

    def readline(self, limit=-1):
        return self._lines.pop(0) if self._lines else b""

    def read(self, size=-1):
        return b""

    def close(self):
        self.closed = True


def stream_error_frame(error_type, message):
    payload = json.dumps({"error": {"type": error_type, "message": message}}).encode()
    return b"data: " + payload + b"\n\n"


def http_error(code, body):
    return urllib.error.HTTPError(
        "http://example/v1/chat/completions", code, "error", {}, io.BytesIO(body)
    )


class RetryOnRateLimitTests(unittest.TestCase):
    def _endpoint(self, module, **kwargs):
        options = {
            "id": "rl", "name": "rl", "base_url": "http://example/v1", "api_key": "x",
            "model": "m", "timeout": 60, "use_proxy": True,
        }
        options.update(kwargs)
        return module.Endpoint(**options)

    def _run(self, module, endpoint, payload, responder, sleeps=None):
        """用 mock urlopen 跑一次 _try_endpoint。

        responder(index) 返回响应对象（正常返回）或 Exception 实例（抛出）。
        """
        calls = []

        def fake_urlopen(_request, timeout):
            calls.append(timeout)
            nxt = responder(len(calls) - 1)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        sleep_hook = (lambda d: sleeps.append(d)) if sleeps is not None else (lambda d: None)
        with mock.patch.object(module.urllib.request, "urlopen", side_effect=fake_urlopen), \
                mock.patch.object(module.time, "sleep", side_effect=sleep_hook):
            result, error = module.APIPool()._try_endpoint(
                endpoint, payload, 60, log_usage=False,
            )
        return result, error, calls

    # ---------- 开关关闭：保持现状（1 次尝试） ----------

    def test_switch_off_429_single_attempt(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            endpoint = self._endpoint(module, max_retries=5, retry_on_rate_limit=False)
            sleeps = []
            result, error, calls = self._run(
                module, endpoint, {"model": "m", "messages": [], "stream": False},
                lambda _i: http_error(429, RATE_LIMIT_BODY), sleeps,
            )
            self.assertIsNone(result)
            self.assertEqual(len(calls), 1)
            self.assertEqual(sleeps, [])
            self.assertIn("429 rate-limited", error)

    def test_switch_off_stream_first_packet_rate_limit_single_attempt(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            endpoint = self._endpoint(module, max_retries=5, retry_on_rate_limit=False)
            frame = stream_error_frame(
                "too_many_requests",
                "Your requests to gpt-6-astra for gpt-6-astra in eastus2 have exceeded rate limit.",
            )
            result, error, calls = self._run(
                module, endpoint, {"model": "m", "messages": [], "stream": True},
                lambda _i: FakeStreamResponse([frame]),
            )
            self.assertIsNone(result)
            self.assertEqual(len(calls), 1)
            self.assertIn("too_many_requests", error)

    # ---------- 开关开启：跑完 max_retries 才交出错误 ----------

    def test_switch_on_429_retries_per_max_retries(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            endpoint = self._endpoint(module, max_retries=2, retry_on_rate_limit=True)
            sleeps = []
            result, error, calls = self._run(
                module, endpoint, {"model": "m", "messages": [], "stream": False},
                lambda _i: http_error(429, RATE_LIMIT_BODY), sleeps,
            )
            self.assertIsNone(result)
            self.assertEqual(len(calls), 3)          # 首次 + max_retries 次重试
            self.assertEqual(sleeps, [3, 6])         # 3*(2^attempt)
            self.assertIn("429 rate-limited", error)

    def test_switch_on_stream_first_packet_rate_limit_retries_then_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            endpoint = self._endpoint(module, max_retries=2, retry_on_rate_limit=True)
            business_chunk = json.dumps({
                "choices": [{"delta": {"content": "hello"}, "finish_reason": None}]
            }).encode()

            def responder(index):
                if index == 0:
                    return FakeStreamResponse([stream_error_frame(
                        "too_many_requests",
                        "Your requests to gpt-6-astra in eastus2 have exceeded rate limit.",
                    )])
                return FakeStreamResponse([b"data: " + business_chunk + b"\n\n"])

            result, error, calls = self._run(
                module, endpoint, {"model": "m", "messages": [], "stream": True}, responder,
            )
            self.assertEqual(error, "")
            self.assertIsNotNone(result)      # 第二次尝试拿到业务首包 → 返回流式 generator
            self.assertEqual(len(calls), 2)   # 首包限流重试一次，未触发冷却

    # ---------- 开关开启但不该重试的类目（A 口径边界） ----------

    def test_switch_on_quota_429_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            endpoint = self._endpoint(module, max_retries=3, retry_on_rate_limit=True)
            result, error, calls = self._run(
                module, endpoint, {"model": "m", "messages": [], "stream": False},
                lambda _i: http_error(429, b'{"error":{"message":"quota exceeded for this key"}}'),
            )
            self.assertIsNone(result)
            self.assertEqual(len(calls), 1)  # 配额类不走限流重试
            self.assertIn("429 rate-limited", error)

    def test_switch_on_non_rate_limit_stream_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            endpoint = self._endpoint(module, max_retries=3, retry_on_rate_limit=True)
            frame = stream_error_frame(
                "upstream_error", "upstream stream ended before first business chunk",
            )
            result, error, calls = self._run(
                module, endpoint, {"model": "m", "messages": [], "stream": True},
                lambda _i: FakeStreamResponse([frame]),
            )
            self.assertIsNone(result)
            self.assertEqual(len(calls), 1)  # 非限流首包错误不在开关范围内
            self.assertIn("upstream stream error", error)

    def test_switch_on_skips_retry_when_request_budget_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            endpoint = self._endpoint(module, max_retries=3, retry_on_rate_limit=True)
            calls = []

            def fake_urlopen(_request, timeout):
                calls.append(timeout)
                raise http_error(429, RATE_LIMIT_BODY)

            with mock.patch.object(module.urllib.request, "urlopen", side_effect=fake_urlopen), \
                    mock.patch.object(module.time, "sleep", side_effect=lambda d: None):
                result, error = module.APIPool()._try_endpoint(
                    endpoint, {"model": "m", "messages": [], "stream": False}, 60,
                    log_usage=False, request_deadline=time.time() + 1,
                )
            self.assertIsNone(result)
            self.assertEqual(len(calls), 1)
            self.assertIn("request budget exhausted", error)

    # ---------- 判定器与字段契约 ----------

    def test_rate_limit_detector_markers(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            detect = module.APIPool()._is_rate_limit_error
            for text in (
                "HTTP 502: upstream stream error: too_many_requests: exceeded rate limit",
                "HTTP 502: upstream stream error: too_many_requests: slow down",
                "HTTP 429: rate limited",
                "HTTP 503: rate_limit_exceeded",
                "HTTP 502: upstream busy; Retry-After: 30",
            ):
                self.assertTrue(detect(text), text)
            for text in (
                "HTTP 502: upstream stream ended before first business chunk",
                "HTTP 500: 后端故障",
                "",
            ):
                self.assertFalse(detect(text), text)

    def test_endpoint_field_default_and_config_load_path(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool()
            pool.add_endpoint({"id": "off", "name": "off"})
            pool.add_endpoint({"id": "on", "name": "on", "retry_on_rate_limit": True})
            by_id = {ep.id: ep for ep in pool._endpoints}
            self.assertFalse(by_id["off"].retry_on_rate_limit)
            self.assertTrue(by_id["on"].retry_on_rate_limit)
            now = time.time()
            self.assertTrue(pool._ep_to_dict(by_id["on"], False, now)["retry_on_rate_limit"])
            self.assertFalse(pool._ep_to_dict(by_id["off"], False, now)["retry_on_rate_limit"])


if __name__ == "__main__":
    unittest.main()
