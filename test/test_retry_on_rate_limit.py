"""端点级 retry_on_rate_limit 开关：HTTP 429 与首包 SSE 分类重试。

首包覆盖限流和上游临时风控，按 max_retries 与请求预算原样重试。
容量与确定性内容拦截优先交给原有处置，开关关闭时首包错误直接返回。
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
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
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
            detect = module.APIPool()._classify_retryable_stream_error
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

    def test_temporary_policy_stream_retry_boundaries(self):
        message = (
            "Your resource has been temporarily blocked because we detected behavior "
            "that may violate our content policy. See https://aka.ms/aoaicodeofconduct"
        )
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            frame = stream_error_frame("forbidden", message)
            payload = {"model": "m", "messages": [], "stream": True}
            for enabled, retries, controls, expected_calls in (
                (True, 2, {}, 3),
                (False, 2, {}, 1),
                (True, 0, {}, 1),
                (True, 2, {"force_no_retry": True}, 1),
                (True, 2, {"request_deadline": time.time() + 1}, 1),
            ):
                with self.subTest(enabled=enabled, retries=retries, controls=controls):
                    ep = self._endpoint(module, retry_on_rate_limit=enabled, max_retries=retries)
                    responses = []

                    def respond(*args, responses=responses, **kwargs):
                        response = FakeStreamResponse([frame])
                        responses.append(response)
                        return response

                    with mock.patch.object(module.urllib.request, "urlopen", side_effect=respond), \
                            mock.patch.object(module.time, "sleep"):
                        result, error = module.APIPool()._try_endpoint(ep, payload, 60, log_usage=False, **controls)
                    self.assertIsNone(result)
                    self.assertIn("temporarily blocked", error)
                    self.assertEqual(len(responses), expected_calls)
                    self.assertTrue(all(r.closed for r in responses))
                    if "request_deadline" in controls:
                        self.assertIn("request budget exhausted", error)

            for suffix in ("insufficient balance", "quota exceeded", "sensitive_words_detected"):
                result, error, calls = self._run(
                    module, self._endpoint(module, retry_on_rate_limit=True, max_retries=2), payload,
                    lambda _i, suffix=suffix: FakeStreamResponse([stream_error_frame("forbidden", message + suffix)]),
                )
                self.assertIsNone(result)
                self.assertEqual(len(calls), 1, suffix)
                self.assertIn(suffix, error)

    def test_retryable_stream_error_categories(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            classify = module.APIPool._classify_retryable_stream_error
            temporary = ("forbidden: Your resource has been temporarily blocked because we detected "
                         "behavior that may violate our content policy.")
            self.assertEqual(classify(temporary.upper()), "temporary_policy_block")
            self.assertEqual(classify("too_many_requests: exceeded rate limit"), "rate_limit")
            for text in ("", None, "forbidden: request violates content policy",
                         "Your resource has been temporarily blocked", "HTTP 405: WAF block",
                         temporary + " insufficient balance", temporary + " quota exceeded",
                         temporary + " sensitive_words_detected", "HTTP 429 quota exceeded"):
                self.assertEqual(classify(text), "", text)

    def test_temporary_policy_retry_over_isolated_http(self):
        """Real HTTP ingress -> pool -> SSE upstream: retry once, keep the same route."""
        calls = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(body)
                if len(calls) % 2:
                    data = stream_error_frame("forbidden", "Your resource has been temporarily blocked because "
                                              "we detected behavior that may violate our content policy.")
                else:
                    chunk = {"model": "m", "choices": [{"index": 0, "delta": {"content": "RETRY_OK"},
                                                        "finish_reason": None}]}
                    data = b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
            threads = [Thread(target=s.serve_forever, daemon=True) for s in (upstream, proxy)]
            ep = self._endpoint(module, in_pool=True, use_proxy=False, retry_on_rate_limit=True,
                                max_retries=2, base_url=f"http://127.0.0.1:{upstream.server_port}/v1")
            module.pool = module.APIPool([ep])
            module.pool._set_current("main", ep.id)
            module.pool._set_persisted("main", ep.id)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for thread in threads:
                thread.start()
            try:
                for path, body in (
                    ("/v1/chat/completions", {"model": "api-pool", "messages": [{"role": "user", "content": "ping"}]}),
                    ("/v1/responses", {"model": "api-pool", "input": "ping"}),
                ):
                    with self.subTest(path=path):
                        request = urllib.request.Request(
                            f"http://127.0.0.1:{proxy.server_port}{path}",
                            data=json.dumps(dict(body, stream=True)).encode(),
                            headers={"Content-Type": "application/json", "User-Agent": "isolated-retry-check/1"},
                        )
                        with opener.open(request, timeout=15) as response:
                            data = response.read()
                            self.assertEqual(response.status, 200)
                        self.assertIn(b"RETRY_OK", data)
                        self.assertNotIn(b"temporarily blocked", data)
                        self.assertEqual(module.pool._get_current("main"), ep.id)
                        self.assertEqual(module.pool._get_persisted("main"), ep.id)
                        self.assertEqual(ep._cooldown_until, 0)
                        self.assertEqual(ep._fail_count, 0)
                self.assertEqual(len(calls), 4)
                self.assertEqual(calls[0], calls[1])
                self.assertEqual(calls[2], calls[3])
                logs = module.sys_logger.get_logs_since(0)
                self.assertTrue(any("temporary_policy_block" in str(row) for row in logs))
            finally:
                for server in (proxy, upstream):
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(timeout=2)

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
