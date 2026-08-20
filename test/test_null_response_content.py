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
        spec = importlib.util.spec_from_file_location("api_pool_server_null_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class NullContentHandler(BaseHTTPRequestHandler):
    mode: ClassVar[str] = "nonstream"

    def log_message(self, format, *args):
        del format, args

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if type(self).mode == "stream":
            events = [
                {"choices": [{"delta": {"content": None}}]},
                {"choices": [{"delta": {"content": "answer"}}]},
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "total_tokens": 12,
                    },
                },
            ]
            raw = b"".join(
                b"data: " + json.dumps(event).encode() + b"\n\n" for event in events
            ) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        body = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "reasoning only",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
            },
        }
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class NullContentTests(unittest.TestCase):
    def make_endpoint(self, module, port):
        return module.Endpoint(
            id="null-content",
            name="null-content",
            base_url=f"http://127.0.0.1:{port}",
            api_key="test",
            model="deepseek-v4-flash",
            protocol="openai",
            in_pool=True,
            use_proxy=False,
            timeout=10,
        )

    def test_nonstream_null_content_returns_success_and_logs_reasoning(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            NullContentHandler.mode = "nonstream"
            server = ThreadingHTTPServer(("127.0.0.1", 0), NullContentHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            usage_calls = []
            log_calls = []
            module.token_tracker.add_usage = lambda *args: usage_calls.append(args)
            module.chat_logger.add_log = lambda *args: log_calls.append(args)
            try:
                ep = self.make_endpoint(module, server.server_port)
                pool = module.APIPool([ep])
                result, error = pool._try_endpoint(
                    ep,
                    {
                        "model": ep.model,
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": False,
                    },
                    timeout=10,
                    log_usage=True,
                )
                self.assertEqual(error, "")
                self.assertIsNone(result["choices"][0]["message"]["content"])
                self.assertEqual(len(usage_calls), 1)
                self.assertEqual(len(log_calls), 1)
                self.assertEqual(log_calls[0][3], "reasoning only")
                self.assertEqual(ep._today_used, 12)
            finally:
                server.shutdown()
                server.server_close()

    def test_stream_null_content_does_not_break_completion_log(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            NullContentHandler.mode = "stream"
            server = ThreadingHTTPServer(("127.0.0.1", 0), NullContentHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            log_calls = []
            module.token_tracker.add_usage = lambda *args: None
            module.chat_logger.add_log = lambda *args: log_calls.append(args)
            try:
                ep = self.make_endpoint(module, server.server_port)
                pool = module.APIPool([ep])
                stream, error = pool._try_endpoint(
                    ep,
                    {
                        "model": ep.model,
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True,
                    },
                    timeout=10,
                    log_usage=True,
                )
                self.assertEqual(error, "")
                raw = b"".join(stream).decode()
                self.assertIn('"content": null', raw)
                self.assertIn('"content": "answer"', raw)
                self.assertEqual(len(log_calls), 1)
                self.assertEqual(log_calls[0][3], "answer")
            finally:
                server.shutdown()
                server.server_close()

    def test_image_translation_falls_back_to_reasoning_when_content_is_null(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = module.Endpoint(name="vision", model="vision", is_vision=True)
            pool = module.APIPool([ep])
            pool._try_endpoint = lambda *args, **kwargs: (
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "reasoning_content": "image description",
                            }
                        }
                    ]
                },
                "",
            )
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AA=="},
                        }
                    ],
                }
            ]
            translated = pool._translate_images_sync(messages, [ep])
            self.assertIn("image description", translated[0]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
