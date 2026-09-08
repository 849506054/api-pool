"""Cross-protocol fallback matrix: inbound chat/responses x upstream openai/anthropic/responses.

Covers rotation from a failing endpoint of one protocol to a healthy endpoint of
another protocol, for both inbound wire protocols.
"""

import json
import sys
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_original_thread = threading.Thread


class _NoStartThread(_original_thread):
    def start(self):
        return None


threading.Thread = _NoStartThread
import api_pool_server as m
threading.Thread = _original_thread


def start_server(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def request(base, method, path, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def parse_sse(raw):
    events = []
    for block in raw.decode("utf-8").split("\n\n"):
        event_type = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                data = line[6:].strip()
        if event_type and data and data != "[DONE]":
            events.append((event_type, json.loads(data)))
    return events


class FailMock(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        body = b'{"error": {"message": "simulated outage"}}'
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ChatOkMock(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.calls.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}"))
        if self.calls[-1].get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"id": "chatcmpl-ok", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": "FALLBACK_CHAT_OK"}, "finish_reason": None}]},
                {"id": "chatcmpl-ok", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"id": "chatcmpl-ok", "object": "chat.completion.chunk", "choices": [],
                 "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6}},
            ]
            for chunk in chunks:
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        resp = {
            "id": "chatcmpl-ok", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "FALLBACK_CHAT_OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6},
        }
        body = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ResponsesOkMock(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.calls.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}"))
        resp = {
            "id": "resp_ok", "object": "response", "created_at": 1, "status": "completed",
            "output": [{"id": "msg_ok", "type": "message", "status": "completed", "role": "assistant",
                        "content": [{"type": "output_text", "text": "FALLBACK_RESPONSES_OK", "annotations": []}]}],
            "usage": {"input_tokens": 3, "output_tokens": 3, "total_tokens": 6},
        }
        body = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AnthropicOkMock(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.calls.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}"))
        resp = {
            "id": "msg_ok", "content": [{"type": "text", "text": "FALLBACK_ANTHROPIC_OK"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 3},
        }
        body = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def set_pool_failover(endpoints):
    """endpoints: list of (port, protocol, name) — first entry = priority 1 (tried first)."""
    m.pool = m.APIPool()
    for i, (port, protocol, name) in enumerate(endpoints):
        m.pool.add_endpoint({
            "name": name,
            "base_url": f"http://127.0.0.1:{port}",
            "api_key": "sk-test",
            "model": "mock-model",
            "priority": i + 1,
            "timeout": 10,
            "max_retries": 0,
            "enabled": True,
            "in_pool": True,
            "use_proxy": False,
            "protocol": protocol,
            "is_vision": True,
        })
    app_server = m.ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
    threading.Thread(target=app_server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{app_server.server_address[1]}"


def test_inbound_chat_fail_openai_fallback_responses():
    fail_srv, fail_port = start_server(FailMock)
    ok_srv, ok_port = start_server(ResponsesOkMock)
    base = set_pool_failover([(fail_port, "openai", "fail_openai"), (ok_port, "responses", "ok_responses")])
    status, body = request(base, "POST", "/v1/chat/completions",
                           {"messages": [{"role": "user", "content": "hi"}]})
    assert status == 200
    assert json.loads(body)["choices"][0]["message"]["content"] == "FALLBACK_RESPONSES_OK"
    # Upstream must have received a Responses-format body (cross-protocol conversion).
    sent = ResponsesOkMock.calls[-1]
    assert "input" in sent and "messages" not in sent


def test_inbound_chat_fail_openai_fallback_anthropic():
    fail_srv, fail_port = start_server(FailMock)
    ok_srv, ok_port = start_server(AnthropicOkMock)
    base = set_pool_failover([(fail_port, "openai", "fail_openai"), (ok_port, "anthropic", "ok_anthropic")])
    status, body = request(base, "POST", "/v1/chat/completions",
                           {"messages": [{"role": "user", "content": "hi"}]})
    assert status == 200
    assert json.loads(body)["choices"][0]["message"]["content"] == "FALLBACK_ANTHROPIC_OK"


def test_inbound_responses_fail_responses_fallback_openai():
    fail_srv, fail_port = start_server(FailMock)
    ok_srv, ok_port = start_server(ChatOkMock)
    base = set_pool_failover([(fail_port, "responses", "fail_responses"), (ok_port, "openai", "ok_openai")])
    status, body = request(base, "POST", "/v1/responses",
                           {"instructions": "be brief", "input": [{"role": "user", "content": "hi"}]})
    assert status == 200
    resp = json.loads(body)
    assert resp["object"] == "response"
    assert resp["output"][0]["content"][0]["text"] == "FALLBACK_CHAT_OK"
    # Upstream must have received chat-completions format.
    sent = ChatOkMock.calls[-1]
    assert "messages" in sent


def test_inbound_responses_fail_openai_fallback_anthropic():
    fail_srv, fail_port = start_server(FailMock)
    ok_srv, ok_port = start_server(AnthropicOkMock)
    base = set_pool_failover([(fail_port, "openai", "fail_openai"), (ok_port, "anthropic", "ok_anthropic")])
    status, body = request(base, "POST", "/v1/responses",
                           {"input": "hi"})
    assert status == 200
    resp = json.loads(body)
    assert resp["output"][0]["content"][0]["text"] == "FALLBACK_ANTHROPIC_OK"
    sent = AnthropicOkMock.calls[-1]
    assert "messages" in sent  # Anthropic native wire


def test_inbound_responses_stream_fail_responses_fallback_openai():
    fail_srv, fail_port = start_server(FailMock)
    ok_srv, ok_port = start_server(ChatOkMock)
    base = set_pool_failover([(fail_port, "responses", "fail_responses"), (ok_port, "openai", "ok_openai")])
    status, body = request(base, "POST", "/v1/responses",
                           {"input": "hi", "stream": True})
    assert status == 200
    events = parse_sse(body)
    names = [e for e, _ in events]
    assert "response.completed" in names
    text = "".join(d.get("delta", "") for e, d in events if e == "response.output_text.delta")
    assert text == "FALLBACK_CHAT_OK"
