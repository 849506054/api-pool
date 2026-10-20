"""出站私有键隔离：`_responses_response_format` 只留在 Responses 桥接里。

2026-10-20 生产事故：/v1/chat/completions 入站带 response_format 时，池把该字段改名成
私有键 `_responses_response_format` 塞进 extra_payload；openai 协议分支把 payload 原样
序列化出站，严格上游（Tokenrhythm-dsf）以 `UNKNOWN_FIELD` 400 拒绝，被记为客户端类错误
轮转。修复点：_prepare_endpoint_payload 对非 responses 协议摘掉该私有键。
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_private_key_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module._client_baseline.update({"User-Agent": "pytest-client/1.0"})
        return module
    finally:
        os.chdir(previous_cwd)


class StrictChatUpstream(BaseHTTPRequestHandler):
    """模仿 Tokenrhythm-dsf：见到未知字段直接 400，否则正常回包。"""

    bodies: ClassVar[list] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).bodies.append(body)
        if "_responses_response_format" in body:
            payload = {
                "code": "UNKNOWN_FIELD",
                "message": "未知请求字段：_responses_response_format",
                "data": {"field": "_responses_response_format"},
            }
            encoded = json.dumps(payload).encode()
            self.send_response(400)
        else:
            payload = {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model", "test"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            encoded = json.dumps(payload).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        pass


class PrivateKeyIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.module = load_module(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _endpoint(self, protocol):
        return self.module.Endpoint(
            id=f"ep-{protocol}",
            name=f"strict-{protocol}",
            base_url="http://127.0.0.1:1",
            api_key="test",
            model="deepseek-flash",
            protocol=protocol,
            use_proxy=False,
            timeout=10,
            in_pool=True,
            pool_groups=["main"],
        )

    def test_openai_payload_drops_private_key(self):
        ep = self._endpoint("openai")
        pool = self.module.APIPool([ep])
        payload, _ = pool._prepare_endpoint_payload(
            [{"role": "user", "content": "hi"}], ep,
            {"_responses_response_format": {"type": "json_object"}},
        )
        self.assertNotIn("_responses_response_format", payload)
        self.assertNotIn("response_format", payload)

    def test_responses_payload_keeps_private_key_for_bridge(self):
        ep = self._endpoint("responses")
        pool = self.module.APIPool([ep])
        payload, _ = pool._prepare_endpoint_payload(
            [{"role": "user", "content": "hi"}], ep,
            {"_responses_response_format": {"type": "json_object"}},
        )
        self.assertIn("_responses_response_format", payload)
        bridged = self.module._responses_body_from_chat(payload)
        self.assertEqual(bridged["text"]["format"]["type"], "json_object")

    def test_strict_upstream_accepts_outbound_body(self):
        StrictChatUpstream.bodies = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), StrictChatUpstream)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            ep = self._endpoint("openai")
            ep.base_url = f"http://127.0.0.1:{server.server_port}"
            pool = self.module.APIPool([ep])
            payload, _ = pool._prepare_endpoint_payload(
                [{"role": "user", "content": "hi"}], ep,
                {"_responses_response_format": {"type": "json_object"}},
            )
            result, error = pool._try_endpoint(ep, payload, 10, pool_group="main", log_usage=False)
            self.assertEqual(error, "")
            self.assertIsNotNone(result)
            self.assertEqual(len(StrictChatUpstream.bodies), 1)
            self.assertNotIn("_responses_response_format", StrictChatUpstream.bodies[0])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
