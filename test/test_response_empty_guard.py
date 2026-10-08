"""Regression: empty-stream guard preserves protocol-valid non-text output."""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "api_pool_server.py"


def frame(delta=None, finish=None):
    return b"data: " + json.dumps({"choices": [{"delta": delta or {}, "finish_reason": finish}]}).encode() + b"\n\n"


class ResponseGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, old)
        spec = importlib.util.spec_from_file_location("response_guard_test_pool", SOURCE)
        self.m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.m
        with mock.patch.object(threading.Thread, "start"):
            spec.loader.exec_module(self.m)
        self.ep = self.m.Endpoint(id="a", name="a", model="m", in_pool=True)
        self.other = self.m.Endpoint(id="b", name="b", model="m", in_pool=True)
        self.p = self.m.APIPool([self.ep, self.other])
        self.p._set_current("main", self.ep.id)

    def run_guard(self, chunks, group="main", epoch=None):
        if epoch is None:
            epoch = self.p._get_route_epoch(group)
        return list(self.p._guard_empty_stream(iter(chunks), self.ep, group, "guard-test", epoch))

    def test_empty_and_thinking_only_rotate_before_error(self):
        for delta in ({}, {"content": " \n"}, {"reasoning_content": "thinking"}):
            with self.subTest(delta=delta):
                self.ep._cooldown_until = 0
                chunks = [frame(delta), frame(finish="stop"), b"data: [DONE]\n\n"]
                out = self.run_guard(chunks)
                error = next(self.m._parse_chat_completion_chunk(c)["error"] for c in out if b'"error"' in c)
                self.assertIn("returned an empty response", error["message"])
                self.assertEqual(error["code"], "empty_content")
                self.assertEqual(out[-1], b"data: [DONE]\n\n")
                self.assertNotIn(frame(finish="stop"), out)
                self.assertGreater(self.ep._cooldown_until, 0)
                self.assertEqual(self.p._get_current("main"), self.other.id)

    def test_normal_tool_refusal_length_and_error_are_preserved(self):
        cases = [
            [frame({"content": "ok"}), frame(finish="stop")],
            [frame({"content": "ok"}).replace(b"data: ", b"data:"), frame(finish="stop")],
            [frame({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "f", "arguments": "{}"}}]}), frame(finish="tool_calls")],
            [frame({"tool_calls": [{"index": 0, "function": {"arguments": ""}}]}), frame(finish="stop")],
            [frame({"function_call": {"name": "f", "arguments": "{}"}}), frame(finish="function_call")],
            [frame({"refusal": "Cannot help"}), frame(finish="stop")],
            [frame({"reasoning_content": "thinking"}), frame(finish="length")],
            [frame(finish="content_filter")],
            [frame(finish="error")],
            [b'data: {"error":{"message":"upstream failed"}}\n\n'],
        ]
        for chunks in cases:
            with self.subTest(chunks=chunks):
                chunks = chunks + [b"data: [DONE]\n\n"]
                self.assertEqual(self.run_guard(chunks), chunks)
                self.assertEqual(self.ep._cooldown_until, 0)
                self.assertEqual(self.p._get_current("main"), self.ep.id)

    def test_direct_probe_and_concurrent_switch_boundaries(self):
        chunks = [frame(), frame(finish="stop"), b"data: [DONE]\n\n"]
        original = dict(self.p._current_endpoint_by_group)
        self.assertTrue(any(b'"empty_content"' in c for c in self.run_guard(chunks, "endpoint:a")))
        self.assertEqual(self.p._current_endpoint_by_group, original)
        self.assertEqual(self.ep._cooldown_until, 0)
        epoch = self.p._get_route_epoch("main")
        self.p._set_current("main", self.other.id)
        self.run_guard(chunks, epoch=epoch)
        self.assertEqual(self.p._get_current("main"), self.other.id)

    def test_consumer_close_releases_without_penalty(self):
        closed = []

        def stream():
            try:
                yield frame({"reasoning_content": "thinking"})
                yield frame(finish="stop")
            finally:
                closed.append(True)

        guarded = self.p._guard_empty_stream(stream(), self.ep, "main", "closed", 0)
        next(guarded)
        guarded.close()
        self.assertEqual(closed, [True])
        self.assertEqual(self.ep._cooldown_until, 0)

    def test_public_responses_error_is_failed_not_completed(self):
        chunks = [b'data: {"error":{"message":"Provider returned an empty response","type":"server_error","code":"empty_content"}}\n\n']
        out = b"".join(self.m._responses_stream_generator(iter(chunks), {}))
        self.assertIn(b"response.failed", out)
        self.assertNotIn(b"response.completed", out)

    def test_http_bridge_and_hermes_sdk_retry_contract(self):
        m = self.m
        mode = {"value": "text"}
        captured = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                captured.append(body)
                responses = self.path.endswith("/responses")
                if responses:
                    for item in body["input"]:
                        for part in item.get("content", []):
                            if part.get("type") == "input_image" and not isinstance(part.get("image_url"), str):
                                self.send_response(400)
                                self.end_headers()
                                return
                kind = mode["value"]
                if responses:
                    output = []
                    events = []
                    if kind == "text":
                        events.append({"type": "response.output_text.delta", "delta": "OK"})
                    elif kind in ("tools", "full_tools"):
                        tool = {"type": "function_call", "id": "fc1", "call_id": "call1", "name": "f", "arguments": "{}"}
                        output.append(tool)
                        if kind == "tools":
                            events.append({"type": "response.output_item.added", "item": tool, "output_index": 0})
                    elif kind in ("thinking", "length"):
                        events.append({"type": "response.reasoning_summary_text.delta", "delta": "think"})
                    elif kind == "refusal":
                        events.append({"type": "response.refusal.delta", "delta": "declined"})
                    if kind == "length":
                        events.append({"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}})
                    else:
                        events.append({"type": "response.completed", "response": {"output": output, "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}}})
                    wire = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)
                else:
                    delta = {"content": "OK"} if kind == "text" else {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "f", "arguments": "{}"}}]} if kind == "tools" else {"reasoning_content": "think"} if kind == "thinking" else {}
                    wire = frame(delta) + frame(finish="tool_calls" if kind == "tools" else "stop")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(wire + b"data: [DONE]\n\n")

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
        for server in (upstream, proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.ep.base_url = f"http://127.0.0.1:{upstream.server_port}/v1"
        self.ep.api_key = "isolated"
        self.ep.protocol = "responses"
        self.ep.is_vision = True
        self.ep.use_proxy = False
        self.ep.max_retries = 0
        self.other.base_url = self.ep.base_url
        self.other.api_key = "isolated"
        self.other.use_proxy = False
        m.pool = self.p
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f"http://127.0.0.1:{proxy.server_port}"
        content = [{"type": "text", "text": "image"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA", "detail": "high"}}]
        payload = {"model": "api-pool", "messages": [{"role": "user", "content": content}], "stream": True}

        def request(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "User-Agent": "hermes-agent/test"})
            with opener.open(req, timeout=10) as response:
                return response.read()

        for protocol in ("responses", "openai"):
            self.ep.protocol = protocol
            for kind in (("text", "tools", "full_tools", "refusal", "length") if protocol == "responses" else ("text", "tools")):
                mode["value"] = kind
                wire = request("/v1/chat/completions", payload)
                self.assertNotIn(b'"empty_content"', wire, (protocol, kind, wire))
                self.assertEqual(self.ep._cooldown_until, 0, (protocol, kind))
                self.assertEqual(self.p._get_current("main"), self.ep.id)
        self.ep.protocol = "responses"
        image = captured[0]["input"][0]["content"][1]
        self.assertEqual(image, {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "high"})
        for kind in ("empty", "thinking"):
            mode["value"] = kind
            self.ep._cooldown_until = 0
            self.p._set_current("main", self.ep.id)
            wire = request("/v1/responses", {"model": "api-pool", "input": "ping", "stream": True})
            self.assertIn(b"response.failed", wire)
            self.assertNotIn(b"response.completed", wire)
            self.assertGreater(self.ep._cooldown_until, 0)
            self.assertEqual(self.p._inflight_owner, {})
        self.ep._cooldown_until = 0
        self.p._set_current("main", self.ep.id)
        mode["value"] = "thinking"
        try:
            import openai
        except ImportError:
            return  # stdlib HTTP checks above remain runnable on the pool host
        client = openai.OpenAI(base_url=base + "/v1", api_key="isolated", max_retries=0)
        self.addCleanup(client.close)
        with self.assertRaises(openai.APIError) as raised:
            list(client.chat.completions.create(**payload))
        self.assertIn("returned an empty response", str(raised.exception))
        if Path("/opt/hermes/agent/error_classifier.py").exists():
            sys.path.insert(0, "/opt/hermes")
            from agent.error_classifier import classify_api_error
            verdict = classify_api_error(raised.exception, provider="custom", model="m")
            self.assertTrue(verdict.retryable, verdict)
        mode["value"] = "text"
        answer = list(client.chat.completions.create(**payload))
        self.assertTrue(any(c.choices and c.choices[0].delta.content == "OK" for c in answer))
        self.assertEqual(self.p._get_current("main"), self.other.id)

    def test_image_url_and_detail_shape_roundtrip(self):
        for url in ("https://example.com/image.png", "data:image/png;base64,AAAA"):
            for raw in (url, {"url": url, "detail": "high"}):
                content = [{"type": "text", "text": "picture"}, {"type": "image_url", "image_url": raw}]
                expected = {"type": "input_image", "image_url": url}
                if isinstance(raw, dict):
                    expected["detail"] = "high"
                actual = self.m._chat_content_to_responses_content(content)
                self.assertEqual(actual[1], expected)
                self.assertEqual(self.m._responses_content_to_chat(actual)[1]["image_url"]["url"], url)


if __name__ == "__main__":
    unittest.main()
