"""Cline 非流式信封解包回归（2026-09-19）。

上游 https://api.cline.bot/api/v1/chat/completions 非流式把 OpenAI 载荷整体包在
{"success": true, "data": {...}} 里（流式帧是标准 OpenAI 形态），池按顶层取 choices
会 KeyError 'choices'。本测试锁定：信封响应解包成功，标准响应/ Gemini 响应不受影响。
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

_HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = next(
    p for p in (os.path.join(os.path.dirname(_HERE), "api_pool_server.py"),
                os.path.join(_HERE, "api_pool_server.py"))
    if os.path.exists(p)
)


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_server_cline_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module._client_baseline.update({"User-Agent": "pytest-client/1.0"})
        return module
    finally:
        os.chdir(previous_cwd)


def cline_envelope():
    """真实上游响应形态（2026-09-19 直连抓取，字段裁剪）。"""
    return {
        "success": True,
        "data": {
            "id": "gen_01M2VYF47S1DAZZCWX4P02K272",
            "object": "chat.completion",
            "model": "deepseek/deepseek-v4.1-flash",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "Hi! How can I help you today?",
                        "reasoning": "We need answer to user...",
                    },
                }
            ],
            "usage": {"prompt_tokens": 32, "completion_tokens": 39, "total_tokens": 71},
        },
    }


def plain_chat():
    return {
        "id": "chatcmpl-plain",
        "object": "chat.completion",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "plain ok"}}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


class ShapeHandler(BaseHTTPRequestHandler):
    mode: ClassVar[str] = "cline"

    def log_message(self, format, *args):
        del format, args

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = {"cline": cline_envelope, "plain": plain_chat}[type(self).mode]()
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ClineEnvelopeTests(unittest.TestCase):
    def run_case(self, mode, protocol="openai"):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ShapeHandler.mode = mode
            server = ThreadingHTTPServer(("127.0.0.1", 0), ShapeHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            module.token_tracker.add_usage = lambda *args: None
            module.chat_logger.add_log = lambda *args: None
            try:
                ep = module.Endpoint(
                    id=f"shape-{mode}",
                    name=f"shape-{mode}",
                    base_url=f"http://127.0.0.1:{server.server_port}",
                    api_key="test",
                    model="deepseek/deepseek-v4.1-flash",
                    protocol=protocol,
                    in_pool=True,
                    use_proxy=False,
                    timeout=10,
                )
                pool = module.APIPool([ep])
                result, error = pool._try_endpoint(
                    ep,
                    {"model": ep.model, "messages": [{"role": "user", "content": "hi"}], "stream": False},
                    timeout=10,
                    log_usage=True,
                )
                return result, error
            finally:
                server.shutdown()
                server.server_close()

    def test_cline_envelope_is_unwrapped(self):
        result, error = self.run_case("cline")
        self.assertEqual(error, "")
        self.assertEqual(result["choices"][0]["message"]["content"], "Hi! How can I help you today?")
        self.assertEqual(result["usage"]["total_tokens"], 71)

    def test_plain_openai_response_untouched(self):
        result, error = self.run_case("plain")
        self.assertEqual(error, "")
        self.assertEqual(result["choices"][0]["message"]["content"], "plain ok")
        self.assertNotIn("data", result)


if __name__ == "__main__":
    unittest.main()
