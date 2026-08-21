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
        spec = importlib.util.spec_from_file_location("api_pool_server_anthropic_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class AnthropicStreamHandler(BaseHTTPRequestHandler):
    calls: ClassVar[list] = []

    def log_message(self, format, *args):
        del format, args

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls.append((self.path, body))
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "checking"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"city":"Beijing"}'}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 8}},
            {"type": "message_stop"},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for event in events:
            self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class OpenAIStreamHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        del format, args

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        chunks = [
            {
                "id": "chatcmpl-openai",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": "openai-ok"}}],
            },
            {
                "id": "chatcmpl-openai",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class AnthropicStreamingTests(unittest.TestCase):
    def test_prompt_cache_control_and_usage_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, format, *args):
                    del format, args

                def do_POST(self):
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length) or b"{}")
                    self.server.body = body
                    response = {
                        "id": "msg_cache",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}],
                        "stop_reason": "end_turn",
                        "usage": {
                            "input_tokens": 12,
                            "cache_creation_input_tokens": 1000,
                            "cache_read_input_tokens": 9000,
                            "output_tokens": 2,
                        },
                    }
                    encoded = json.dumps(response).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                ep = module.Endpoint(
                    id="anthropic-cache",
                    name="anthropic-cache-test",
                    base_url=f"http://127.0.0.1:{server.server_port}",
                    api_key="test",
                    model="claude-test",
                    protocol="anthropic",
                    use_proxy=False,
                    timeout=10,
                )
                pool = module.APIPool([ep])
                result, error = pool._try_endpoint(
                    ep,
                    {
                        "model": ep.model,
                        "messages": [
                            {"role": "system", "content": "stable system"},
                            {"role": "user", "content": "hello"},
                        ],
                        "stream": False,
                        "max_tokens": 16,
                    },
                    timeout=10,
                    log_usage=False,
                )
                self.assertEqual(error, "")
                self.assertEqual(result["usage"]["prompt_tokens"], 10012)
                self.assertEqual(result["usage"]["prompt_tokens_details"]["cached_tokens"], 9000)
                self.assertEqual(
                    server.body["cache_control"],
                    {"type": "ephemeral"},
                )
                self.assertEqual(server.body["system"], "stable system")
                self.assertNotIn("cache_control", server.body["messages"][-1])
            finally:
                server.shutdown()
                server.server_close()

    def test_stream_text_tool_finish_usage_and_done(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            AnthropicStreamHandler.calls.clear()
            server = ThreadingHTTPServer(("127.0.0.1", 0), AnthropicStreamHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                ep = module.Endpoint(
                    id="anthropic",
                    name="anthropic-test",
                    base_url=f"http://127.0.0.1:{server.server_port}",
                    api_key="test",
                    model="claude-opus-4-8",
                    protocol="anthropic",
                    in_pool=True,
                    use_proxy=False,
                    timeout=10,
                )
                pool = module.APIPool([ep])
                stream, error = pool._try_endpoint(
                    ep,
                    {
                        "model": ep.model,
                        "messages": [{"role": "user", "content": "weather"}],
                        "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}],
                        "stream": True,
                        "max_tokens": 32,
                    },
                    timeout=10,
                    log_usage=False,
                )
                self.assertEqual(error, "")
                raw = b"".join(stream).decode()
                payloads = []
                done_count = 0
                for line in raw.splitlines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        done_count += 1
                    else:
                        payloads.append(json.loads(data))

                self.assertEqual(AnthropicStreamHandler.calls[0][0], "/messages")
                self.assertTrue(AnthropicStreamHandler.calls[0][1]["stream"])
                self.assertTrue(any((p.get("choices") or [{}])[0].get("delta", {}).get("content") == "checking" for p in payloads))
                tool_chunks = [p for p in payloads if (p.get("choices") or [{}])[0].get("delta", {}).get("tool_calls")]
                self.assertEqual(len(tool_chunks), 1)
                tool = tool_chunks[0]["choices"][0]["delta"]["tool_calls"][0]
                self.assertEqual(tool["id"], "toolu_1")
                self.assertEqual(tool["function"]["name"], "get_weather")
                self.assertEqual(tool["function"]["arguments"], '{"city":"Beijing"}')
                finishes = [(p.get("choices") or [{}])[0].get("finish_reason") for p in payloads if p.get("choices")]
                self.assertIn("tool_calls", finishes)
                usage = [p["usage"] for p in payloads if p.get("usage")]
                self.assertEqual(usage[-1], {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18})
                self.assertEqual(done_count, 1)
            finally:
                server.shutdown()
                server.server_close()

    def test_openai_stream_remains_passthrough(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            server = ThreadingHTTPServer(("127.0.0.1", 0), OpenAIStreamHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                ep = module.Endpoint(
                    id="openai",
                    name="openai-test",
                    base_url=f"http://127.0.0.1:{server.server_port}",
                    api_key="test",
                    model="gpt-test",
                    protocol="openai",
                    in_pool=True,
                    use_proxy=False,
                    timeout=10,
                )
                pool = module.APIPool([ep])
                stream, error = pool._try_endpoint(
                    ep,
                    {
                        "model": ep.model,
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True,
                    },
                    timeout=10,
                    log_usage=False,
                )
                self.assertEqual(error, "")
                raw = b"".join(stream).decode()
                self.assertIn('"content": "openai-ok"', raw)
                self.assertIn('"finish_reason": "stop"', raw)
                self.assertEqual(raw.count("data: [DONE]"), 1)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
