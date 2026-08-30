"""缓存统计按 API Key 账户边界修正。"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_cache_account_test_{os.getpid()}_{id(tmp_path)}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class CachedUsageHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        del format, args

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "total_tokens": 105,
                "prompt_tokens_details": {"cached_tokens": 99},
            },
        }
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class CacheAccountBoundaryTests(unittest.TestCase):
    @staticmethod
    def endpoint(module, endpoint_id, api_key, priority):
        return module.Endpoint(
            id=endpoint_id,
            name=endpoint_id,
            base_url="http://127.0.0.1:1",
            api_key=api_key,
            model="same-model",
            priority=priority,
            in_pool=True,
            use_proxy=False,
        )

    def test_chat_marks_only_different_key_failover_for_stats_reset(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            first = self.endpoint(module, "first", "key-a", 1)
            second = self.endpoint(module, "second", "key-b", 2)
            pool = module.APIPool([first, second])
            calls = []

            def fake_try(ep, payload, timeout, **kwargs):
                del payload, timeout
                calls.append((ep.name, kwargs.get("reset_cached_stats")))
                if ep is first:
                    return None, "HTTP 502: down"
                return {"choices": [{"message": {"content": "ok"}}]}, ""

            pool._try_endpoint = fake_try
            pool.chat([{"role": "user", "content": "hello"}])
            self.assertEqual(calls, [("first", False), ("second", True)])

    def test_same_key_failover_does_not_reset_cached_stats(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            first = self.endpoint(module, "first", "same-key", 1)
            second = self.endpoint(module, "second", "same-key", 2)
            pool = module.APIPool([first, second])
            calls = []

            def fake_try(ep, payload, timeout, **kwargs):
                del payload, timeout
                calls.append((ep.name, kwargs.get("reset_cached_stats")))
                return (None, "HTTP 502: down") if ep is first else ({"choices": [{"message": {"content": "ok"}}]}, "")

            pool._try_endpoint = fake_try
            pool.chat([{"role": "user", "content": "hello"}])
            self.assertEqual(calls, [("first", False), ("second", False)])

    def test_stats_reset_does_not_modify_upstream_usage_response(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            server = ThreadingHTTPServer(("127.0.0.1", 0), CachedUsageHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            usage_calls = []
            log_calls = []
            module.token_tracker.add_usage = lambda *args: usage_calls.append(args)
            module.chat_logger.add_log = lambda *args: log_calls.append(args)
            try:
                ep = module.Endpoint(
                    id="target", name="target", base_url=f"http://127.0.0.1:{server.server_port}",
                    api_key="key-b", model="same-model", in_pool=True, use_proxy=False,
                )
                result, error = module.APIPool([ep])._try_endpoint(
                    ep,
                    {"model": ep.model, "messages": [{"role": "user", "content": "hello"}]},
                    timeout=10,
                    reset_cached_stats=True,
                )
                self.assertEqual(error, "")
                self.assertEqual(result["usage"]["prompt_tokens_details"]["cached_tokens"], 99)
                self.assertEqual(usage_calls[0][-1], 0)
                self.assertEqual(log_calls[0][-1], 0)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
