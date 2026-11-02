"""Historical images stay out of text endpoints and the vision translator."""
import copy
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SOURCE = Path(os.environ.get("API_POOL_TEST_SOURCE", Path(__file__).resolve().parents[1] / "api_pool_server.py"))
OLD = "https://image.invalid/old.png"
NEW = "https://image.invalid/new.png"


def image(url):
    return {"type": "image_url", "image_url": {"url": url}}


def images(messages):
    return [part["image_url"]["url"] for msg in messages if isinstance(msg.get("content"), list)
            for part in msg["content"] if isinstance(part, dict) and part.get("type") == "image_url"]


def history(latest=None):
    return [
        {"role": "user", "content": [{"type": "text", "text": "old question"}, image(OLD)]},
        {"role": "assistant", "content": "Previous image interpretation"},
        {"role": "user", "content": latest or "text-only follow-up"},
    ]


class VisionHistoryWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="vision-history-")
        cls.old_cwd = os.getcwd()
        cls.old_db = os.environ.get("API_POOL_DB_DIR")
        os.chdir(cls.tmp.name)
        os.environ["API_POOL_DB_DIR"] = cls.tmp.name
        spec = importlib.util.spec_from_file_location("pool_vision_history_wire", SOURCE)
        assert spec is not None and spec.loader is not None
        cls.mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.mod
        spec.loader.exec_module(cls.mod)
        cls.calls = []
        cls.fail_first = False

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                cls.calls.append(body)
                if cls.fail_first and body["model"] == "vision-main":
                    data = b'{"error":{"message":"try text candidate"}}'
                    self.send_response(502)
                else:
                    text = "new image description" if body["model"] == "translator" else "business-ok"
                    if body.get("stream"):
                        chunk = {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
                        data = b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n"
                    else:
                        data = json.dumps({"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "usage": {}}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), cls.mod.Handler)
        for server in (cls.upstream, cls.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.proxy, cls.upstream):
            server.shutdown()
            server.server_close()
        os.chdir(cls.old_cwd)
        if cls.old_db is None:
            os.environ.pop("API_POOL_DB_DIR", None)
        else:
            os.environ["API_POOL_DB_DIR"] = cls.old_db
        cls.tmp.cleanup()

    def setUp(self):
        self.calls.clear()
        type(self).fail_first = False
        self.text = self.endpoint("text", False)
        self.translator = self.endpoint("translator", True, ["vision"])
        self.mod.pool = self.mod.APIPool([self.text, self.translator])

    def endpoint(self, name, vision, groups=None, priority=1):
        return self.mod.Endpoint(id=name, name=name, model=name, is_vision=vision, in_pool=True,
            pool_groups=groups or ["main"], priority=priority, base_url=f"http://127.0.0.1:{self.upstream.server_port}/v1",
            api_key="isolated", use_proxy=False, health_mode="none", max_retries=0)

    def request(self, messages, stream=False, direct=False, route="chat/completions"):
        prefix = f"/endpoints/{self.text.id}" if direct else ""
        body = {"model": "api-pool", "stream": stream}
        if route == "responses":
            input_items = copy.deepcopy(messages)
            for msg in input_items:
                if isinstance(msg.get("content"), list):
                    for part in msg["content"]:
                        if part["type"] == "image_url":
                            part["type"] = "input_image"
                            part["image_url"] = part["image_url"]["url"]
                        elif part["type"] == "text":
                            part["type"] = "input_text"
            body["input"] = input_items
        else:
            body["messages"] = messages
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=10)
        try:
            conn.request("POST", prefix + "/v1/" + route, body=json.dumps(body).encode(), headers={
                "User-Agent": "vision-history-sample/1", "Content-Type": "application/json"})
            resp = conn.getresponse()
            result = resp.read().decode()
            self.assertEqual(resp.status, 200, result)
            self.assertIn("business-ok", result)
            return result
        finally:
            conn.close()

    def test_history_only_never_calls_vision_json_sse_responses(self):
        for route in ("chat/completions", "responses"):
            for stream in (False, True):
                with self.subTest(route=route, stream=stream):
                    self.calls.clear()
                    self.mod.pool._vision_cache.clear()
                    result = self.request(history(), stream=stream, route=route)
                    self.assertEqual([b["model"] for b in self.calls], ["text"])
                    self.assertEqual(images(self.calls[-1]["messages"]), [])
                    self.assertIn("Previous image interpretation", json.dumps(self.calls[-1]))
                    self.assertIn("old question", json.dumps(self.calls[-1]))
                    self.assertNotIn("正在调用视觉模型", result)

    def test_mixed_history_and_new_image_translates_only_new(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.calls.clear()
                self.mod.pool._vision_cache.clear()
                self.request(history([{"type": "text", "text": "new question"}, image(NEW)]), stream=stream)
                self.assertEqual([b["model"] for b in self.calls], ["translator", "text"])
                self.assertEqual(images(self.calls[0]["messages"]), [NEW])
                self.assertEqual(images(self.calls[1]["messages"]), [])
                self.assertNotIn("new image description", json.dumps(self.calls[1]["messages"][0]))
                self.assertIn("new image description", json.dumps(self.calls[1]["messages"][-1]))

    def test_native_vision_keeps_all_images(self):
        self.text.is_vision = True
        self.request(history([image(NEW)]))
        self.assertEqual([b["model"] for b in self.calls], ["text"])
        self.assertEqual(images(self.calls[-1]["messages"]), [OLD, NEW])

    def test_no_vision_candidates_keeps_current_image_only(self):
        self.translator.enabled = False
        self.request(history([image(NEW)]))
        self.assertEqual([b["model"] for b in self.calls], ["text"])
        self.assertEqual(images(self.calls[-1]["messages"]), [NEW])

    def test_direct_endpoint_removes_history_preserves_exclusivity(self):
        self.request(history([image(NEW)]), direct=True)
        self.assertEqual([b["model"] for b in self.calls], ["text"])
        self.assertEqual(images(self.calls[-1]["messages"]), [NEW])

    def test_tool_continuation_and_new_tool_image(self):
        call = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "tool", "arguments": "{}"}}]}
        for tool_content, expected in (("done", ["text"]), ([image(NEW)], ["translator", "text"])):
            with self.subTest(tool_content=tool_content):
                self.calls.clear()
                self.mod.pool._vision_cache.clear()
                msgs = [{"role": "user", "content": [image(OLD)]}, call,
                        {"role": "tool", "tool_call_id": "c1", "content": tool_content}]
                self.request(msgs)
                self.assertEqual([b["model"] for b in self.calls], expected)
                self.assertNotIn(OLD, json.dumps(self.calls))
                self.assertIn("c1", json.dumps(self.calls[-1]))

    def test_failed_vision_endpoint_switches_to_text_with_no_translation(self):
        first = self.endpoint("vision-main", True, priority=1)
        self.text.priority = 2
        self.mod.pool = self.mod.APIPool([first, self.text, self.translator])
        type(self).fail_first = True
        self.request(history())
        self.assertEqual([b["model"] for b in self.calls], ["vision-main", "text"])
        self.assertEqual(images(self.calls[0]["messages"]), [OLD])
        self.assertEqual(images(self.calls[1]["messages"]), [])

    def test_message_source_unmodified_and_image_only_history_valid(self):
        msgs = [{"role": "user", "content": [image(OLD)]}, {"role": "assistant", "content": "understood"},
                {"role": "user", "content": [image(NEW)]}]
        original = copy.deepcopy(msgs)
        text_payload, _ = self.mod.pool._prepare_endpoint_payload(msgs, self.text)
        self.assertEqual(images(text_payload["messages"]), [NEW])
        self.assertTrue(text_payload["messages"][0]["content"])
        self.assertEqual(msgs, original)
        self.text.is_vision = True
        native_payload, _ = self.mod.pool._prepare_endpoint_payload(msgs, self.text)
        self.assertEqual(images(native_payload["messages"]), [OLD, NEW])

    def test_direct_translation_guard_skips_history(self):
        self.mod.set_client_headers({"User-Agent": "vision-history-sample/1"})
        try:
            msgs = history()
            result = self.mod.pool._translate_images_sync(msgs, [])
            self.assertEqual(self.calls, [])
            self.assertEqual(images(result), [])
            self.assertEqual(images(msgs), [OLD])
        finally:
            self.mod.clear_client_headers()


if __name__ == "__main__":
    unittest.main()
