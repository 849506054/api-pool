import importlib.util
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_client_error_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class ClientErrorClassificationTests(unittest.TestCase):
    """_classify_client_error 纯函数行为。"""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp_path:
            cls.module = load_module(tmp_path)
        cls.classify = staticmethod(cls.module.APIPool._classify_client_error)

    def test_client_codes_detected(self):
        for code in (400, 404, 413, 422):
            self.assertTrue(self.classify(f"HTTP {code}: {{\"error\": \"bad request shape\"}}"))

    def test_non_client_codes_rejected(self):
        for code in (401, 403, 429, 500, 502, 503):
            self.assertFalse(self.classify(f"HTTP {code}: something"))

    def test_transient_markers_rejected(self):
        for body in (
            "HTTP 400: rate limit exceeded",
            "HTTP 422: quota exceeded for today",
            "HTTP 400: insufficient balance",
            "HTTP 413: request temporarily too large",
            "HTTP 400: server overloaded, retry later",
        ):
            self.assertFalse(self.classify(body), body)

    def test_non_http_prefix_rejected(self):
        for msg in ("连接/超时错误: timeout", "fake-success: xxx", "未知错误: boom", ""):
            self.assertFalse(self.classify(msg), msg)

    def test_auth_error_suffix_still_classified(self):
        # 401/403 不在客户端类集合，本就不触发
        self.assertFalse(self.classify("HTTP 401: (auth error)"))
        # 400 带正常 body → 客户端类
        self.assertTrue(self.classify("HTTP 400: invalid request"))


class ClientErrorRotationTests(unittest.TestCase):
    """A′ 方案：轮转但不记账、不冻结、不探活。"""

    @staticmethod
    def endpoint(module, endpoint_id, priority, model="test-model"):
        return module.Endpoint(
            id=endpoint_id,
            name=endpoint_id,
            base_url="http://127.0.0.1:1",
            api_key="test",
            model=model,
            priority=priority,
            in_pool=True,
            use_proxy=False,
        )

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.module = load_module(self.tmp_dir.name)

    def test_client_400_does_not_freeze_or_count(self):
        pool = self.module.APIPool()
        ep = self.endpoint(self.module, "ep-a", 1)
        pool._endpoints.append(ep)
        ep._fail_count = 0
        ep._total_failures = 0
        before_cooldown = ep._cooldown_until

        pool._rotate(ep, "HTTP 400: invalid request", health_impact=False)

        self.assertEqual(ep._fail_count, 0)
        self.assertEqual(ep._total_failures, 0)
        self.assertEqual(ep._cooldown_until, before_cooldown)
        self.assertEqual(ep._cooldown_reason, "")

    def test_health_impact_default_keeps_old_semantics(self):
        pool = self.module.APIPool()
        ep = self.endpoint(self.module, "ep-a", 1)
        pool._endpoints.append(ep)
        ep.cooldown_minutes = 5

        pool._rotate(ep, "HTTP 500: upstream boom")

        self.assertEqual(ep._fail_count, 1)
        self.assertEqual(ep._total_failures, 1)
        self.assertGreater(ep._cooldown_until, time.time())

    def test_all_client_errors_returns_error_not_infinite_loop(self):
        pool = self.module.APIPool()
        eps = [self.endpoint(self.module, f"ep-{i}", i + 1) for i in range(3)]
        for ep in eps:
            pool._endpoints.append(ep)
        calls = []

        def fake_try(ep, payload, timeout, **kwargs):
            calls.append(ep.id)
            return None, "HTTP 400: invalid request shape"

        pool._try_endpoint = fake_try
        with self.assertRaises(self.module.AllEndpointsFailed):
            pool.chat([{"role": "user", "content": "hi"}])
        # 每个端点恰好尝试一次，无死循环
        self.assertEqual(sorted(calls), ["ep-0", "ep-1", "ep-2"])
        for ep in eps:
            self.assertEqual(ep._fail_count, 0)
            self.assertEqual(ep._cooldown_until, 0)
            self.assertEqual(ep._total_failures, 0)

    def test_mixed_pool_healthy_endpoint_takes_over(self):
        pool = self.module.APIPool()
        bad = self.endpoint(self.module, "bad", 1)
        good = self.endpoint(self.module, "good", 2)
        pool._endpoints.extend([bad, good])
        pool._current_endpoint_id = bad.id

        def fake_try(ep, payload, timeout, **kwargs):
            if ep.id == "bad":
                return None, "HTTP 400: this model rejects the shape"
            return {"choices": [{"message": {"content": "ok"}}]}, ""

        pool._try_endpoint = fake_try
        result = pool.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        # bad 未被记账/冻结，good 拿到成功
        self.assertEqual(bad._fail_count, 0)
        self.assertEqual(bad._cooldown_until, 0)
        self.assertGreater(good._total_calls, 0)

    def test_client_error_keeps_current_endpoint_sticky(self):
        """客户端类错误不改路由指针：下一个请求仍从当前端点开始。"""
        pool = self.module.APIPool()
        bad = self.endpoint(self.module, "bad", 1)
        good = self.endpoint(self.module, "good", 2)
        pool._endpoints.extend([bad, good])
        pool._current_endpoint_id = bad.id

        def fake_try(ep, payload, timeout, **kwargs):
            if ep.id == "bad":
                return None, "HTTP 400: invalid"
            return {"choices": [{"message": {"content": "ok"}}]}, ""

        pool._try_endpoint = fake_try
        pool.chat([{"role": "user", "content": "one"}])
        # 路由指针未被 _rotate 的健康分支改写（good 成功后 _on_success 会设为 good，
        # 这是成功语义；这里验证的是 bad 没有因 400 被踢出 current）
        self.assertEqual(pool._current_endpoint_id, good.id)
        # bad 无冷却，下一请求按粘性逻辑仍可先试 bad
        self.assertEqual(pool._ordered_failover_candidates(good, [bad, good])[0].id, "bad")

    def test_probe_not_triggered_for_client_error(self):
        """客户端类错误不触发候选探活（探活必然通过，无信息量）。"""
        pool = self.module.APIPool()
        bad = self.endpoint(self.module, "bad", 1)
        good = self.endpoint(self.module, "good", 2)
        pool._endpoints.extend([bad, good])
        pool._current_endpoint_id = bad.id
        probe_calls = []

        def fake_try(ep, payload, timeout, **kwargs):
            if ep.id == "bad":
                return None, "HTTP 400: invalid"
            return {"choices": [{"message": {"content": "ok"}}]}, ""

        def fake_probe(ep):
            probe_calls.append(ep.id)
            return True, ""

        pool._try_endpoint = fake_try
        pool._probe_endpoint = fake_probe
        pool.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(probe_calls, [])

    def test_workaround_temperature_still_handled_first(self):
        """auto-strip temperature/top_p 特例仍在 _try_endpoint 内部消化（真实 HTTP 路径），
        剥离重试成功后不进入 chat() 失败分支，端点零损伤。"""
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                payload = _json.loads(self.rfile.read(length))
                seen.append(payload)
                if "temperature" in payload:
                    resp = _json.dumps({"error": {"message": "temperature not supported"}}).encode()
                    status = 400
                else:
                    resp = _json.dumps({
                        "id": "x", "object": "chat.completion", "created": 1, "model": "test-model",
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": "ok"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }).encode()
                    status = 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            def log_message(self, format, *args):  # noqa: A003
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            pool = self.module.APIPool()
            ep = self.endpoint(self.module, "ep-a", 1)
            ep.base_url = f"http://127.0.0.1:{port}"
            ep.use_proxy = False
            pool._endpoints.append(ep)

            result = pool.chat([{"role": "user", "content": "hi"}], extra_payload={"temperature": 0.7})

            self.assertEqual(result["choices"][0]["message"]["content"], "ok")
            self.assertEqual(len(seen), 2)  # 第一次带 temperature 被拒，剥离后重试成功
            self.assertNotIn("temperature", seen[1])
            self.assertEqual(ep._fail_count, 0)
            self.assertEqual(ep._cooldown_until, 0)
            self.assertEqual(ep._total_failures, 0)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
