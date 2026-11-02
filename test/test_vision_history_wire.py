"""Loopback wire contracts for per-image parsing reuse and intact history."""
import copy
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

SOURCE = Path(os.environ.get("API_POOL_TEST_SOURCE", Path(__file__).resolve().parents[1] / "api_pool_server.py"))
OLD = "https://image.invalid/old.png"
NEW = "https://image.invalid/new.png"


def image(url, detail=None):
    value = {"url": url}
    if detail is not None:
        value["detail"] = detail
    return {"type": "image_url", "image_url": value}


def images(messages):
    return [part["image_url"]["url"] for msg in messages if isinstance(msg.get("content"), list)
            for part in msg["content"] if isinstance(part, dict) and part.get("type") == "image_url"]


def history(latest=None):
    return [
        {"role": "user", "content": [{"type": "text", "text": "old question"}, image(OLD)]},
        {"role": "assistant", "content": "Previous image interpretation"},
        {"role": "user", "content": latest or "text-only follow-up"},
    ]


def description(url):
    return "description:" + url


class VisionHistoryWireTests(unittest.TestCase):
    target_errors: ClassVar[dict] = {}
    fail_first = False
    vision_modes: ClassVar[dict] = {}
    translation_entered = threading.Event()
    translation_gate = None

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

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                cls.calls.append(body)
                is_translator = body["model"].startswith("translator")
                url = images(body.get("messages", []))[0] if is_translator else ""
                mode = cls.vision_modes.get(url, "ok")
                if is_translator:
                    cls.translation_entered.set()
                    if cls.translation_gate is not None:
                        cls.translation_gate.wait(5)
                failures = cls.target_errors.get(body["model"], [])
                status = failures.pop(0) if failures else 0
                if status or (cls.fail_first and body["model"] == "vision-main") or (is_translator and mode == "error"):
                    error = "reasoning_content must be passed back" if status == 400 else 'upstream "failed"\nnext line'
                    data = json.dumps({"error": {"message": error}}).encode()
                    self.send_response(status or 502)
                else:
                    text = description(url) if is_translator else "business-ok"
                    if is_translator and mode in ("blank", "reasoning"):
                        text = " \n\t" if mode == "blank" else ""
                    if self.path.endswith("/responses"):
                        data = json.dumps({"id": "resp-test", "object": "response", "status": "completed",
                            "output": [{"type": "message", "role": "assistant", "content": [
                                {"type": "output_text", "text": text}]}], "usage": {}}).encode()
                    elif body.get("stream"):
                        chunk = {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
                        data = b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n"
                    else:
                        message = {"content": text}
                        if mode == "reasoning":
                            message["reasoning_content"] = "thinking without a description"
                        data = json.dumps({"choices": [{"message": message, "finish_reason": "stop"}], "usage": {}}).encode()
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
        type(self).target_errors = {}
        type(self).vision_modes = {}
        type(self).translation_entered = threading.Event()
        type(self).translation_gate = None
        self.logs = []
        logger = patch.object(self.mod, "sys_log", lambda msg, level="INFO", **kw: self.logs.append((level, msg)))
        logger.start()
        self.addCleanup(logger.stop)
        self.text = self.endpoint("text", False)
        self.translator = self.endpoint("translator", True, ["vision"])
        self.mod.pool = self.mod.APIPool([self.text, self.translator])
        debug = patch.object(self.mod, "_DEBUG_LOGGING", True)
        debug.start()
        self.addCleanup(debug.stop)

    def endpoint(self, name, vision, groups=None, priority=1):
        return self.mod.Endpoint(id=name, name=name, model=name, is_vision=vision, in_pool=True,
            pool_groups=groups or ["main"], priority=priority, base_url=f"http://127.0.0.1:{self.upstream.server_port}/v1",
            api_key="isolated", use_proxy=False, health_mode="none", max_retries=0)

    def request(self, messages, stream=False, direct=False, route="chat/completions", expected_status=200):
        prefix = f"/endpoints/{self.text.id}" if direct else ""
        body = {"model": "api-pool", "stream": stream}
        if route == "responses":
            input_items = copy.deepcopy(messages)
            for msg in input_items:
                if isinstance(msg.get("content"), list):
                    for part in msg["content"]:
                        if part["type"] == "image_url":
                            detail = part["image_url"].get("detail")
                            part["type"] = "input_image"
                            part["image_url"] = part["image_url"]["url"]
                            if detail is not None:
                                part["detail"] = detail
                        elif part["type"] == "text":
                            part["type"] = "input_text"
            body["input"] = input_items
        else:
            body["messages"] = messages
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=15)
        try:
            conn.request("POST", prefix + "/v1/" + route, body=json.dumps(body).encode(), headers={
                "User-Agent": "vision-history-sample/1", "Content-Type": "application/json"})
            resp = conn.getresponse()
            result = resp.read().decode()
            self.assertEqual(resp.status, expected_status, result)
            if expected_status != 200:
                self.assertIn("error", json.loads(result))
                self.assertNotIn("图片解析完成", result)
                self.assertNotIn("data: ", result)
                return result
            self.assertIn("business-ok", result)
            if stream:
                for line in result.splitlines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        json.loads(line[6:])
            else:
                json.loads(result)
            return result
        finally:
            conn.close()

    def translations(self):
        return [body for body in self.calls if body["model"].startswith("translator")]

    def warm(self, url=OLD):
        self.request([{"role": "user", "content": [image(url)]}])
        self.calls.clear()
        self.logs.clear()

    def assert_counts(self, reused, parsed, unresolved):
        self.assertTrue(any(level == ("INFO" if parsed else "DEBUG") and "[req=" in msg and
            f"复用={reused}" in msg and f"新解析={parsed}" in msg and f"未解析={unresolved}" in msg
            for level, msg in self.logs), self.logs)

    def routing_state(self, state):
        self.calls.clear()
        self.logs.clear()
        self.text = self.endpoint("text", False)
        self.second = self.endpoint("text-second", False, priority=2)
        self.translator = self.endpoint("translator", True, ["vision"])
        self.mod.pool = self.mod.APIPool([self.text, self.second, self.translator])
        type(self).target_errors = {}
        if state == "cache":
            self.mod.pool._vision_cache_set(self.mod.pool._vision_cache_key(image(OLD)), description(OLD))
        if state == "no-candidates":
            self.translator.enabled = False

    def routing_cases(self):
        for state in ("cold-history", "cache", "no-candidates"):
            for route in ("chat/completions", "responses"):
                for stream in (False, True):
                    yield state, route, stream

    def test_image_routing_502_failover(self):
        for state, route, stream in self.routing_cases():
            with self.subTest(state=state, route=route, stream=stream):
                self.routing_state(state)
                self.target_errors["text"] = [502]
                result = self.request(history(), stream=stream, route=route)
                self.assertEqual([b["model"] for b in self.calls if not b["model"].startswith("translator")],
                                 ["text", "text-second"])
                self.assertEqual(self.text._fail_count, 1)
                self.assertEqual(self.mod.pool._inflight_owner, {})
                self.assertNotIn("请求最终目标失败", result)
                self.assertEqual(len(self.translations()), int(state == "cold-history"))
                self.assertEqual("图片解析完成" in result, stream and state == "cold-history")

    def test_image_routing_strict400_retries_same_endpoint(self):
        for state, route, stream in self.routing_cases():
            with self.subTest(state=state, route=route, stream=stream):
                self.routing_state(state)
                self.text.strict400_retries = 1
                self.target_errors["text"] = [400]
                with patch.object(self.mod.pool, "_strict400_backoff", return_value=0):
                    self.request(history(), stream=stream, route=route)
                self.assertEqual([b["model"] for b in self.calls if not b["model"].startswith("translator")],
                                 ["text", "text"])
                self.assertEqual(self.text._fail_count, 0)
                self.assertEqual(self.mod.pool._inflight_owner, {})

    def test_image_routing_strict400_disabled_rotates(self):
        for state, route, stream in self.routing_cases():
            with self.subTest(state=state, route=route, stream=stream):
                self.routing_state(state)
                self.text.strict400_retries = 0
                self.target_errors["text"] = [400]
                self.request(history(), stream=stream, route=route)
                self.assertEqual([b["model"] for b in self.calls if not b["model"].startswith("translator")],
                                 ["text", "text-second"])
                self.assertEqual(self.text._fail_count, 1)
                self.assertEqual(self.mod.pool._inflight_owner, {})

    def test_image_routing_http_retry_budget(self):
        for state, route, stream in self.routing_cases():
            with self.subTest(state=state, route=route, stream=stream):
                self.routing_state(state)
                self.text.max_retries = 1
                self.target_errors["text"] = [502]
                with patch.object(self.mod.pool, "_retry_backoff", return_value=0):
                    self.request(history(), stream=stream, route=route)
                self.assertEqual([b["model"] for b in self.calls if not b["model"].startswith("translator")],
                                 ["text", "text"])
                self.assertEqual(self.text._fail_count, 0)
                self.assertEqual(self.mod.pool._inflight_owner, {})

    def test_image_routing_context_skip_releases_inflight(self):
        for state, route, stream in self.routing_cases():
            with self.subTest(state=state, route=route, stream=stream):
                self.routing_state(state)
                self.text.max_context_k = 1
                self.second.max_context_k = 4
                with patch.object(self.mod.pool, "_estimate_context_tokens", return_value=2001) as estimate:
                    self.request(history(), stream=stream, route=route)
                self.assertEqual(estimate.call_count, 2)
                self.assertEqual([b["model"] for b in self.calls if not b["model"].startswith("translator")],
                                 ["text-second"])
                self.assertEqual(self.text._fail_count, 0)
                self.assertEqual(self.mod.pool._inflight_owner, {})

    def test_image_routing_all_failed_is_http_error(self):
        for state, route, stream in self.routing_cases():
            with self.subTest(state=state, route=route, stream=stream):
                self.routing_state(state)
                self.target_errors.update(text=[502], **{"text-second": [502]})
                self.request(history(), stream=stream, route=route, expected_status=500)
                self.assertEqual(self.text._fail_count, 1)
                self.assertEqual(self.second._fail_count, 1)
                self.assertEqual(self.mod.pool._inflight_owner, {})

    def test_uncached_history_parsed_json_sse_chat_responses(self):
        for route in ("chat/completions", "responses"):
            for stream in (False, True):
                with self.subTest(route=route, stream=stream):
                    self.calls.clear()
                    self.mod.pool._vision_cache.clear()
                    self.request(history(), stream=stream, route=route)
                    self.assertEqual([b["model"] for b in self.calls], ["translator", "text"])
                    self.assertEqual(images(self.calls[0]["messages"]), [OLD])
                    self.assertEqual(images(self.calls[-1]["messages"]), [])
                    self.assertIn(description(OLD), json.dumps(self.calls[-1]["messages"][0]))
                    self.assertEqual(self.calls[-1]["messages"][1]["content"], "Previous image interpretation")

    def test_cached_history_no_translation_or_stream_notices(self):
        self.warm()
        for route in ("chat/completions", "responses"):
            for stream in (False, True):
                with self.subTest(route=route, stream=stream):
                    self.calls.clear()
                    result = self.request(history(), stream=stream, route=route)
                    self.assertEqual([b["model"] for b in self.calls], ["text"])
                    self.assertIn(description(OLD), json.dumps(self.calls[-1]))
                    self.assertNotIn("正在调用视觉模型", result)
                    self.assertNotIn("图片解析完成", result)
                    self.assert_counts(1, 0, 0)

    def test_mixed_history_and_new_image_replaced_in_place(self):
        self.warm()
        msgs = history([{"type": "text", "text": "before"}, image(NEW),
                        {"type": "text", "text": "between"}, image(OLD), {"type": "text", "text": "after"}])
        self.request(msgs)
        self.assertEqual(len(self.translations()), 1)
        self.assertEqual(images(self.translations()[0]["messages"]), [NEW])
        result = self.calls[-1]["messages"]
        expected = copy.deepcopy(msgs)
        expected[0]["content"][1] = result[0]["content"][1]
        expected[2]["content"][1] = result[2]["content"][1]
        expected[2]["content"][3] = result[2]["content"][3]
        self.assertEqual(result, expected)
        self.assertIn(description(OLD), result[0]["content"][1]["text"])
        self.assertIn(description(NEW), result[2]["content"][1]["text"])
        self.assertNotIn(description(OLD), result[2]["content"][1]["text"])
        self.assertIn(description(OLD), result[2]["content"][3]["text"])
        self.assert_counts(2, 1, 0)

    def test_tool_call_is_not_proof_of_interpretation(self):
        msgs = [{"role": "user", "content": [image(OLD)]},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                    "function": {"name": "tool", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": [image(NEW)]}]
        self.request(msgs)
        self.assertEqual([images(b["messages"]) for b in self.translations()], [[OLD], [NEW]])
        self.assertEqual(self.calls[-1]["messages"][2]["tool_call_id"], "c1")
        self.assertEqual(self.calls[-1]["messages"][1], msgs[1])
        self.assertIn(description(NEW), self.calls[-1]["messages"][2]["content"][0]["text"])

    def test_detail_identity_and_default_auto(self):
        for detail in (None, "auto", "low", "high", "low"):
            self.request([{"role": "user", "content": [image(OLD, detail)]}])
        self.assertEqual(len(self.translations()), 3)
        self.assertEqual([b["messages"][-1]["content"][0]["image_url"].get("detail", "auto")
                          for b in self.translations()], ["auto", "low", "high"])

    def test_cache_expiry_reparses_history(self):
        self.warm()
        cache = self.mod.pool._vision_cache
        for key, (_, desc) in list(cache.items()):
            cache[key] = (time.time() - self.mod.pool._VISION_CACHE_TTL - 1, desc)
        self.request(history())
        self.assertEqual(len(self.translations()), 1)
        self.assertIn(description(OLD), json.dumps(self.calls[-1]))

    def test_cache_available_without_vision_candidates(self):
        self.warm()
        self.translator.enabled = False
        self.request(history([image(NEW)]))
        self.assertEqual([b["model"] for b in self.calls], ["text"])
        self.assertEqual(images(self.calls[-1]["messages"]), [NEW])
        self.assertIn(description(OLD), json.dumps(self.calls[-1]["messages"][0]))
        self.assert_counts(1, 0, 1)
        self.assertTrue(any(level in ("WARN", "WARNING") and "原样" in msg for level, msg in self.logs))

    def test_no_candidates_keeps_all_images(self):
        self.translator.enabled = False
        msgs = history([image(NEW)])
        self.request(msgs)
        self.assertEqual(self.calls[-1]["messages"], msgs)
        self.assert_counts(0, 0, 2)

    def test_failure_blank_reasoning_keep_original_and_skip_cache(self):
        for mode in ("error", "blank", "reasoning"):
            with self.subTest(mode=mode):
                self.vision_modes[OLD] = mode
                self.translator._cooldown_until = 0
                result = self.request(history(), stream=True)
                self.assertNotIn("图片解析完成", result)
                self.assertEqual(images(self.calls[-1]["messages"]), [OLD])
                self.assertEqual(self.mod.pool._vision_cache, {})
                self.assert_counts(0, 0, 1)
                self.assertTrue(any(level in ("WARN", "WARNING") and "原样" in msg for level, msg in self.logs))

    def test_partial_success_reused_on_next_request(self):
        self.vision_modes[NEW] = "error"
        result = self.request(history([image(NEW)]), stream=True)
        self.assertEqual(images(self.calls[-1]["messages"]), [NEW])
        self.assertIn(description(OLD), json.dumps(self.calls[-1]))
        self.assertNotIn("图片解析完成", result)
        self.assert_counts(0, 1, 1)
        self.calls.clear()
        self.translator._cooldown_until = 0
        self.request(history([image(NEW)]))
        self.assertEqual([images(b["messages"]) for b in self.translations()], [[NEW]])
        self.assert_counts(1, 0, 1)

    def test_native_vision_keeps_all_images_even_with_cache(self):
        self.warm()
        self.text.is_vision = True
        msgs = history([image(NEW, "high")])
        self.request(msgs)
        self.assertEqual([b["model"] for b in self.calls], ["text"])
        self.assertEqual(self.calls[-1]["messages"], msgs)
        self.assertTrue(any("图片原样转发" in msg and "total=2" in msg and "history=1" in msg
                            for _, msg in self.logs), self.logs)

    def test_responses_upstream_shape_separate_from_forwarding(self):
        self.text.is_vision = True
        self.text.protocol = "responses"
        self.request(history([image(NEW, "high")]))
        items = self.calls[-1]["input"]
        self.assertEqual(items[0]["content"][1]["image_url"], OLD)
        self.assertEqual(items[-1]["content"][0]["image_url"], NEW)
        self.assertEqual(items[-1]["content"][0]["detail"], "high")
        self.assertTrue(any("图片原样转发" in msg for _, msg in self.logs))
        self.assertTrue(any("Responses" in msg and "shape" in msg for _, msg in self.logs))

    def test_direct_endpoint_only_reuses_cache(self):
        self.warm()
        for route in ("chat/completions", "responses"):
            for stream in (False, True):
                with self.subTest(route=route, stream=stream):
                    self.calls.clear()
                    self.request(history([image(NEW)]), direct=True, stream=stream, route=route)
                    self.assertEqual([b["model"] for b in self.calls], ["text"])
                    self.assertEqual(images(self.calls[-1]["messages"]), [NEW])
                    self.assertIn(description(OLD), json.dumps(self.calls[-1]["messages"][0]))
                    self.assert_counts(1, 0, 1)

    def test_direct_without_cache_keeps_entire_history(self):
        msgs = history([image(NEW)])
        self.request(msgs, direct=True)
        self.assertEqual(self.calls[-1]["messages"], msgs)
        self.assertEqual(self.translations(), [])

    def test_failed_native_vision_switches_to_text_and_parses_history(self):
        for route in ("chat/completions", "responses"):
            for stream in (False, True):
                with self.subTest(route=route, stream=stream):
                    self.routing_state("cold-history")
                    first = self.endpoint("vision-main", True, priority=1)
                    self.text.priority = 2
                    self.mod.pool = self.mod.APIPool([first, self.text, self.translator])
                    type(self).fail_first = True
                    self.request(history(), stream=stream, route=route)
                    self.assertEqual([b["model"] for b in self.calls], ["vision-main", "translator", "text"])
                    self.assertEqual(images(self.calls[0]["messages"]), [OLD])
                    self.assertIn(description(OLD), json.dumps(self.calls[-1]))
                    self.assertEqual(self.mod.pool._inflight_owner, {})

    def test_source_history_unmodified_after_prepare_and_translate(self):
        msgs = history([image(NEW)])
        original = copy.deepcopy(msgs)
        self.mod.set_client_headers({"User-Agent": "vision-history-sample/1"})
        try:
            payload, _ = self.mod.pool._prepare_endpoint_payload(msgs, self.text)
            self.assertEqual(payload["messages"], msgs)
            result = self.mod.pool._translate_images_sync(msgs, [])
            self.assertEqual(images(result), [])
            self.assertEqual(msgs, original)
            self.text.is_vision = True
            native, _ = self.mod.pool._prepare_endpoint_payload(msgs, self.text)
            self.assertEqual(native["messages"], original)
        finally:
            self.mod.clear_client_headers()

    def test_duplicate_images_in_one_request_and_consecutive_requests(self):
        msgs = history([image(OLD), image(OLD)])
        self.request(msgs)
        self.request(msgs)
        self.assertEqual(len(self.translations()), 1)
        self.assertEqual(images(self.calls[-1]["messages"]), [])
        self.assertEqual(json.dumps(self.calls[-1]).count(description(OLD)), 3)

    def test_concurrent_duplicates_singleflight(self):
        type(self).translation_gate = threading.Event()
        barrier = threading.Barrier(4)
        def invoke():
            barrier.wait(5)
            return self.request([{"role": "user", "content": [image(OLD)]}], stream=True)
        with ThreadPoolExecutor(max_workers=4) as workers:
            futures = [workers.submit(invoke) for _ in range(4)]
            try:
                self.assertTrue(self.translation_entered.wait(5))
                time.sleep(0.2)
            finally:
                self.translation_gate.set()
            results = [future.result(10) for future in futures]
        self.assertEqual(len(self.translations()), 1)
        self.assertEqual(sum("图片解析完成" in result for result in results), 1)
        self.assertFalse(any("正在调用视觉模型" in result for result in results))

    def test_translation_close_releases_image_lock(self):
        part = image(OLD)
        work = self.mod.pool._translate_images_iter([{"role": "user", "content": [part]}], [])
        self.assertIsInstance(next(work), bytes)
        work.close()
        key = self.mod.pool._vision_cache_key(part)
        lock = self.mod.pool._vision_image_locks[int(key, 16) % len(self.mod.pool._vision_image_locks)]
        acquired = lock.acquire(blocking=False)
        try:
            self.assertTrue(acquired)
            self.assertEqual(self.calls, [])
        finally:
            if acquired:
                lock.release()

    def test_cache_and_singleflight_locks_bounded(self):
        pool = self.mod.pool
        for index in range(pool._VISION_CACHE_MAX + 3):
            pool._vision_cache_set(str(index), "description")
        self.assertEqual(len(pool._vision_cache), pool._VISION_CACHE_MAX)
        self.assertEqual(len(pool._vision_image_locks), pool._VISION_CACHE_MAX)
        self.assertNotIn("0", pool._vision_cache)
        pool._vision_cache_set("empty", " \t\n")
        self.assertNotIn("empty", pool._vision_cache)

    def test_waiting_for_same_image_obeys_request_deadline(self):
        part = image(OLD)
        key = self.mod.pool._vision_cache_key(part)
        lock = self.mod.pool._vision_image_locks[int(key, 16) % len(self.mod.pool._vision_image_locks)]
        msgs = history()
        lock.acquire()
        started = time.time()
        try:
            output = self.mod.pool._translate_images_sync(msgs, [], request_deadline=started + 0.05)
        finally:
            lock.release()
        self.assertLess(time.time() - started, 0.5)
        self.assertIs(output, msgs)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.mod.pool._vision_cache, {})

    def test_expired_request_keeps_cached_parts_and_untouched_unknowns(self):
        self.warm()
        msgs = history([image(NEW)])
        output = self.mod.pool._translate_images_sync(msgs, [], request_deadline=time.time() - 1)
        self.assertIn(description(OLD), json.dumps(output[0]))
        self.assertEqual(images(output), [NEW])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.translator._fail_count, 0)

    def test_data_url_identity_and_detail(self):
        data = "data:image/png;base64,aGVsbG8="
        for detail in (None, "auto", "high", "high"):
            self.request([{"role": "user", "content": [image(data, detail)]}])
        self.assertEqual(len(self.translations()), 2)
        self.assertEqual(images(self.calls[-1]["messages"]), [])

    def test_native_and_hot_cache_logs_quiet_with_debug_off(self):
        self.warm()
        self.mod._DEBUG_LOGGING = False
        self.request(history())
        self.assertFalse(any(level == "INFO" and "图片" in msg for level, msg in self.logs))
        self.calls.clear()
        self.logs.clear()
        self.text.is_vision = True
        self.request(history())
        self.assertFalse(any("图片原样转发" in msg for _, msg in self.logs))

    def test_plain_text_descriptions_remain_text(self):
        msgs = [{"role": "user", "content": "[图片解析内容]: a user-written description"}]
        self.request(msgs)
        self.assertEqual(self.calls[-1]["messages"], msgs)
        self.assertEqual(self.translations(), [])


if __name__ == "__main__":
    unittest.main()
