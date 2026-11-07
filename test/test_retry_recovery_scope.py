"""Shared retry budget and group-scoped recovery contracts, isolated upstream."""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SOURCE = Path(os.environ.get("API_POOL_TEST_SOURCE", Path(__file__).resolve().parents[1] / "api_pool_server.py"))


def frame(delta=None, finish=None):
    return b"data: " + json.dumps({"choices": [{"delta": delta or {}, "finish_reason": finish}]}).encode() + b"\n\n"


class RetryRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="retry-recovery-")
        cls.old_cwd = os.getcwd()
        cls.old_db = os.environ.get("API_POOL_DB_DIR")
        os.chdir(cls.tmp.name)
        os.environ["API_POOL_DB_DIR"] = cls.tmp.name
        spec = importlib.util.spec_from_file_location("retry_recovery_test", SOURCE)
        assert spec is not None and spec.loader is not None
        cls.m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.m
        spec.loader.exec_module(cls.m)
        cls.calls = []
        cls.sequence = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                cls.calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                kind = cls.sequence.pop(0) if cls.sequence else "ok"
                code = 200
                if kind in ("http502", "auth"):
                    code = 502 if kind == "http502" else 401
                    data = b'{"error":{"message":"actual upstream error"}}'
                elif kind == "done":
                    data = b"data: [DONE]\n\n"
                elif kind == "eof":
                    data = b""
                elif kind == "empty":
                    data = frame({"reasoning_content": "thinking"}) + frame(finish="stop") + b"data: [DONE]\n\n"
                elif kind == "sse_empty":
                    data = b'data: {"error":{"message":"upstream stream ended before first business chunk"}}\n\n'
                elif kind == "length":
                    data = frame({"reasoning_content": "thinking"}) + frame(finish="length") + b"data: [DONE]\n\n"
                elif kind == "partial_error":
                    data = frame({"content": "partial"}) + b'data: {"error":{"message":"upstream stream ended before first business chunk"}}\n\n'
                else:
                    data = frame({"content": "business-ok"}) + frame(finish="stop") + b"data: [DONE]\n\n"
                self.send_response(code)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        os.chdir(cls.old_cwd)
        if cls.old_db is None:
            os.environ.pop("API_POOL_DB_DIR", None)
        else:
            os.environ["API_POOL_DB_DIR"] = cls.old_db
        cls.tmp.cleanup()

    def setUp(self):
        self.calls.clear()
        self.sequence.clear()
        self.a = self.ep("a", ["main", "feishu"])
        self.b = self.ep("b", ["main"])
        self.q = self.ep("qoder", ["feishu"])
        self.p = self.m.APIPool([self.a, self.b, self.q])
        self.m.pool = self.p
        self.p._set_current("main", "a")
        self.p._set_current("feishu", "qoder")
        self.p._last_pool_activity = time.time()
        self.m.set_client_headers({"User-Agent": "retry-recovery-sample/1"})
        self.addCleanup(self.m.clear_client_headers)

    def ep(self, name, groups):
        return self.m.Endpoint(id=name, name=name, model="m", api_key="isolated", in_pool=True,
            base_url=f"http://127.0.0.1:{self.server.server_port}/v1", pool_groups=groups,
            health_mode="none", use_proxy=False, max_retries=2)

    def call(self, kinds, retries=2, **kwargs):
        self.sequence[:] = kinds
        self.a.max_retries = retries
        with mock.patch.object(self.m.time, "sleep"):
            result, error = self.p._try_endpoint(self.a, {"model": "m", "messages": [], "stream": True}, 5,
                pool_group="main", request_id="golden", **kwargs)
            wire = b"".join(result) if result is not None else b""
        return wire, error

    def test_first_frame_empty_and_sse_empty_retry(self):
        for kind in ("done", "eof", "sse_empty"):
            with self.subTest(kind=kind):
                self.calls.clear()
                wire, error = self.call([kind, "ok"], retries=1)
                self.assertFalse(error, error)
                self.assertIn(b"business-ok", wire)
                self.assertEqual(len(self.calls), 2)
                self.assertEqual(self.a._cooldown_until, 0)

    def test_empty_then_transient_error_keeps_remaining_budget(self):
        for middle in ("http502", "done", "eof"):
            with self.subTest(middle=middle):
                self.calls.clear()
                wire, error = self.call(["empty", middle, "ok"])
                self.assertFalse(error, error)
                self.assertIn(b"business-ok", wire)
                self.assertEqual(len(self.calls), 3)
                self.assertEqual(self.a._cooldown_until, 0)

    def test_http_then_empty_cannot_reset_budget(self):
        wire, error = self.call(["http502", "empty", "ok"], retries=1)
        self.assertFalse(error)
        self.assertEqual(len(self.calls), 2)
        self.assertIn(b"empty_content", wire)

    def test_replay_failure_reports_actual_error_and_count(self):
        with mock.patch.object(self.m, "sys_log") as log:
            wire, _ = self.call(["empty", "http502"], retries=1)
        self.assertIn(b"HTTP 502", wire)
        self.assertNotIn(b'"code": "empty_content"', wire)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(any("原地重试 1 次" in str(c) for c in log.call_args_list))

    def test_auth_partial_and_legitimate_terminal_keep_boundaries(self):
        for kinds, expected in ((["empty", "auth", "ok"], 2), (["partial_error", "ok"], 1), (["length", "ok"], 1)):
            with self.subTest(kinds=kinds):
                self.calls.clear()
                self.a._cooldown_until = 0
                wire, _ = self.call(kinds, retries=3)
                self.assertEqual(len(self.calls), expected)
                if kinds[0] == "partial_error":
                    self.assertIn(b"partial", wire)
                if kinds[0] == "length":
                    self.assertIn(b"length", wire)

    def test_zero_budget_and_deadline(self):
        _, error = self.call(["done", "ok"], retries=0)
        self.assertIn("before first business chunk", error)
        self.assertEqual(len(self.calls), 1)
        self.calls.clear()
        _, error = self.call(["done", "ok"], retries=3, request_deadline=time.time() + 1)
        self.assertIn("budget", error)
        self.assertEqual(len(self.calls), 1)

    def fail_main(self):
        self.p._rotate(self.a, "HTTP 502: upstream failure", group="main")
        self.a._cooldown_until = time.time() - 1

    def test_main_failure_recovers_only_main(self):
        self.fail_main()
        self.p._background_probe(self.a, {"main": "b", "feishu": "qoder"})
        self.assertIn("main", self.a._defer_until_by_group)
        self.assertNotIn("feishu", self.a._defer_until_by_group)
        self.assertEqual(self.p._get_current("feishu"), "qoder")

    def test_manual_switch_cancels_pending_recovery(self):
        self.fail_main()
        self.p.switch_to_endpoint("b", group="main")
        self.p._background_probe(self.a, {"main": "b", "feishu": "qoder"})
        self.assertFalse(self.a._defer_until_by_group)
        self.assertEqual(self.p._get_current("main"), "b")

    def test_manual_switch_while_probe_running_wins(self):
        self.fail_main()
        self.a.health_mode = "chat"
        with mock.patch.object(self.p, "_probe_endpoint", side_effect=lambda ep: (self.p.switch_to_endpoint("b", group="main"), "")):
            self.p._background_probe(self.a, {"main": "b", "feishu": "qoder"})
        self.assertFalse(self.a._defer_until_by_group)
        self.assertEqual(self.p._get_current("main"), "b")

    def test_unrelated_recovery_without_handoff_only_restores_health(self):
        self.p.switch_to_endpoint("b", group="main")
        self.a._cooldown_until = time.time() - 1
        self.b.deferrable = False
        self.q.deferrable = False
        self.p._background_probe(self.a, {"main": "b", "feishu": "qoder"})
        self.assertEqual(self.p._get_current("main"), "b")
        self.assertEqual(self.p._get_current("feishu"), "qoder")
        self.assertFalse(self.a._defer_until_by_group)

    def test_shared_cooldown_actual_second_group_handoff_recovers_both(self):
        self.p._set_current("feishu", "a")
        self.p._rotate(self.a, "HTTP 502", group="main")
        self.p._set_current("feishu", "qoder")
        self.a._cooldown_until = time.time() - 1
        self.p._background_probe(self.a)
        self.assertEqual(set(self.a._defer_until_by_group), {"main", "feishu"})

    def test_manual_switch_from_cooled_pointer_does_not_register_return(self):
        self.a._cooldown_until = time.time() + 60
        self.p.switch_to_endpoint("b", group="main")
        self.assertFalse(self.a._recovery_groups)
        self.a._cooldown_until = time.time() - 1
        self.p._background_probe(self.a)
        self.assertEqual(self.p._get_current("main"), "b")

    def test_manual_subgroup_switch_cancels_cooled_endpoint_return(self):
        self.p.create_group("work", "dedicated", "api-work")
        self.p._group_defs["work"]["priority_by_group"] = {"main": 3}
        member = self.ep("worker", ["work"])
        self.p.add_endpoint(member)
        self.a._cooldown_until = time.time() + 60
        result, _ = self.p.switch_main_to_subgroup("work")
        self.assertTrue(result)
        self.assertFalse(self.a._recovery_groups)
        self.assertEqual(self.p._get_current("main"), "work")

    def test_cooldown_snapshot_records_only_actual_group(self):
        self.p._rotate(self.a, "HTTP 502", group="main")
        snapshot = self.m._collect_cooldown_state()
        self.assertEqual(snapshot["a"]["recovery_groups"], ["main"])
        self.p.switch_to_endpoint("b", group="main")
        self.assertNotIn("recovery_groups", self.m._collect_cooldown_state()["a"])

    def test_cooldown_recovery_group_survives_restart(self):
        self.p._rotate(self.a, "HTTP 502", group="main")
        self.m._sync_to_config()
        self.m.save_runtime_state_groups(dict(self.p._current_endpoint_by_group),
                                        cooldowns=self.m._collect_cooldown_state())
        spec = importlib.util.spec_from_file_location("retry_recovery_restarted", SOURCE)
        assert spec is not None and spec.loader is not None
        restarted = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = restarted
        try:
            with mock.patch.object(threading.Thread, "start"):
                spec.loader.exec_module(restarted)
            ep = restarted.pool.get_endpoint("a")
            self.assertEqual(ep._recovery_groups, {"main"})
            ep._cooldown_until = time.time() - 1
            restarted.pool._last_pool_activity = time.time()
            restarted.pool._background_probe(ep)
            self.assertNotIn("feishu", ep._defer_until_by_group)
            self.assertEqual(restarted.pool._get_current("feishu"), "qoder")
        finally:
            Path("api_config.json").unlink(missing_ok=True)
            Path("api_runtime_state.json").unlink(missing_ok=True)

    def test_late_success_during_cooldown_keeps_return_ownership(self):
        self.p._rotate(self.a, "HTTP 502", group="main")
        self.p._on_success(self.a, group="main", expected_route_epoch=0)
        self.assertIn("main", self.a._recovery_groups)
        self.assertEqual(self.p._get_current("main"), "b")

    def test_borrowed_child_recovery_lifecycle(self):
        for mode in ("direct", "siblings", "protected", "other-child", "manual", "restart", "detached"):
            with self.subTest(mode=mode):
                restored = self.ep("restored", ["child"])
                restored.deferrable = False
                fallback = self.ep("fallback", ["telegram"])
                fallback.deferrable = mode == "protected"
                sibling = self.ep("sibling", ["child"])
                sibling.deferrable = False
                other = self.ep("other", ["other-child"])
                other.deferrable = True
                self.p = self.m.APIPool([restored, fallback, other] + ([sibling] if mode == "siblings" else []))
                self.m.pool = self.p
                self.p._load_group_defs([
                    {"name": "telegram", "type": "mixed", "model": "pool-telegram"},
                    {"name": "unrelated", "type": "mixed", "model": "pool-unrelated"},
                    {"name": "child", "type": "dedicated", "model": "child-model",
                     "priority_by_group": {"telegram": 1, "unrelated": 1}},
                    {"name": "other-child", "type": "dedicated", "model": "other-model",
                     "priority_by_group": {"telegram": 3}},
                ])
                restored.priority_by_group = {"child": 1}
                sibling.priority_by_group = {"child": 2}
                fallback.priority_by_group = {"telegram": 2}
                self.p._set_current("child", restored.id)
                self.p._set_current("telegram", "child")
                self.p._set_current("unrelated", "child")
                self.p._last_pool_activity = time.time()
                self.p._rotate(restored, "HTTP 400: content-blocked", group="telegram")
                self.assertIn("telegram", restored._recovery_groups)
                self.assertNotIn("unrelated", restored._recovery_groups)
                if mode == "siblings":
                    self.p._rotate(sibling, "HTTP 400: content-blocked", group="telegram")
                    self.assertIn("telegram", sibling._recovery_groups)
                self.assertEqual(self.p._get_current("telegram"), fallback.id)
                if mode == "other-child":
                    self.p._set_current("other-child", other.id)
                    self.p._set_current("telegram", "other-child")
                if mode == "manual":
                    self.p.switch_to_endpoint(fallback.id, group="telegram")
                if mode == "detached":
                    self.p._group_defs["child"]["priority_by_group"].pop("telegram")
                if mode == "restart":
                    self.m._sync_to_config()
                    self.m.save_runtime_state_groups(dict(self.p._current_endpoint_by_group),
                                                    cooldowns=self.m._collect_cooldown_state())
                    spec = importlib.util.spec_from_file_location("borrowed_recovery_restarted", SOURCE)
                    assert spec is not None and spec.loader is not None
                    restarted = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = restarted
                    try:
                        with mock.patch.object(threading.Thread, "start"):
                            spec.loader.exec_module(restarted)
                        self.p = restarted.pool
                        restored = self.p.get_endpoint("restored")
                        self.assertIn("telegram", restored._recovery_groups)
                    finally:
                        Path("api_config.json").unlink(missing_ok=True)
                        Path("api_runtime_state.json").unlink(missing_ok=True)
                restored._cooldown_until = time.time() - 1
                self.p._last_pool_activity = time.time()
                with mock.patch.object(self.m, "sys_log") as log:
                    self.p._background_probe(restored)
                if mode in ("protected", "other-child"):
                    target = "other-child" if mode == "other-child" else fallback.id
                    self.assertEqual(self.p._get_current("telegram"), target)
                    self.assertIn("telegram", restored._defer_until_by_group)
                    self.p._reconcile_deferred()
                    self.assertEqual(self.p._get_current("telegram"), target)
                    self.p._last_pool_activity = time.time() - 301
                    self.p._reconcile_deferred()
                if mode in ("manual", "detached"):
                    self.assertEqual(self.p._get_current("telegram"), fallback.id)
                    continue
                self.assertEqual(self.p._get_current("telegram"), "child")
                self.assertEqual(self.p._get_current("child"), restored.id)
                self.assertEqual(self.p._get_current("unrelated"), "child")
                if mode not in ("protected", "other-child", "restart"):
                    self.assertTrue(any("[telegram]restored" in str(call) and "恢复回切" in str(call)
                                        for call in log.call_args_list))
                self.sequence[:] = ["ok"]
                if mode == "restart":
                    self.p.__class__.chat.__globals__["set_client_headers"]({"User-Agent": "retry-recovery-sample/1"})
                    self.addCleanup(self.p.__class__.chat.__globals__["clear_client_headers"])
                result = self.p.chat([{"role": "user", "content": "borrowed recovery golden"}],
                                     model="pool-telegram", extra_payload={"stream": True})
                self.assertIn(b"business-ok", b"".join(result))
                self.assertEqual(self.p._get_current("telegram"), "child")
                self.assertEqual(self.p._get_current("child"), restored.id)

    def test_retire_group_moves_recovery_ownership(self):
        self.a._recovery_groups.add("feishu")
        self.p._retire_group_key(self.a, "feishu", "work")
        self.assertEqual(self.a._recovery_groups, {"work"})
        self.p._retire_group_key(self.a, "work")
        self.assertFalse(self.a._recovery_groups)


if __name__ == "__main__":
    unittest.main()
