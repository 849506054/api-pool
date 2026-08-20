import importlib.util
import os
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_health_test_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class ConcurrentHealthCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.module = load_module(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def make_pool(self, count=3):
        endpoints = [
            self.module.Endpoint(id=f"ep-{index}", name=f"Endpoint {index}", enabled=True, in_pool=True)
            for index in range(count)
        ]
        return self.module.APIPool(endpoints), endpoints

    def test_overlapping_full_checks_do_not_duplicate_probes(self):
        pool, endpoints = self.make_pool(2)
        started = threading.Event()
        release = threading.Event()
        calls = []

        def probe(ep):
            calls.append(ep.id)
            started.set()
            release.wait(timeout=2)
            return ep.id, "ok", 1, ""

        pool._check_one_health = probe
        first_result = []
        first = threading.Thread(target=lambda: first_result.extend(pool.check_all_health()))
        first.start()
        self.assertTrue(started.wait(timeout=1))

        second = pool.check_all_health()
        self.assertEqual(len(second), 2)
        self.assertTrue(all(item["error"] == "健康检测进行中" for item in second))

        release.set()
        first.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertCountEqual(calls, [ep.id for ep in endpoints])
        self.assertEqual(len(first_result), 2)
        self.assertEqual(pool._probe_inflight, set())

        third = pool.check_all_health()
        self.assertEqual(len(third), 2)
        self.assertEqual(len(calls), 4)

    def test_full_check_skips_endpoint_claimed_by_background_probe(self):
        pool, endpoints = self.make_pool(2)
        pool._probe_inflight.add(endpoints[0].id)
        calls = []

        def probe(ep):
            calls.append(ep.id)
            return ep.id, "ok", 1, ""

        pool._check_one_health = probe
        result = pool.check_all_health()

        self.assertEqual(calls, [endpoints[1].id])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], endpoints[0].id)
        self.assertEqual(result[0]["error"], "健康检测进行中")
        self.assertEqual(
            result[1],
            {"id": endpoints[1].id, "health": "ok", "latency_ms": 1, "error": ""},
        )
        self.assertEqual(pool._probe_inflight, {endpoints[0].id})

    def test_full_check_limits_concurrency_to_two(self):
        pool, _ = self.make_pool(5)
        state_lock = threading.Lock()
        active = 0
        peak = 0

        def probe(ep):
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with state_lock:
                active -= 1
            return ep.id, "ok", 1, ""

        pool._check_one_health = probe
        result = pool.check_all_health()

        self.assertEqual(len(result), 5)
        self.assertLessEqual(peak, 2)
        self.assertGreaterEqual(peak, 2)

    def test_business_success_clears_stale_health_error(self):
        pool, endpoints = self.make_pool(1)
        endpoint = endpoints[0]
        endpoint._health = "bad"
        endpoint._health_error = "延迟过高: 7817ms"

        pool._on_success(endpoint, {"choices": []})

        self.assertEqual(endpoint._health, "ok")
        self.assertEqual(endpoint._health_error, "")

    def test_models_endpoint_returns_stable_aggregate_model(self):
        def fail_if_upstream_called(*args, **kwargs):
            self.fail("/v1/models must not query an upstream endpoint")

        self.module.pool.fetch_models = fail_if_upstream_called
        code, body, is_stream = self.module.api_handler("GET", "/v1/models", {})

        self.assertEqual(code, 200)
        self.assertFalse(is_stream)
        self.assertEqual(body["object"], "list")
        self.assertEqual(
            body["data"],
            [{"id": "api-pool", "object": "model", "created": 0, "owned_by": "api-pool"}],
        )

    def test_models_alias_matches_v1_models(self):
        v1_response = self.module.api_handler("GET", "/v1/models", {})
        alias_response = self.module.api_handler("GET", "/models", {})

        self.assertEqual(alias_response, v1_response)

    def test_http_handler_routes_v1_models(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.module.Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            conn.request("GET", "/v1/models")
            response = conn.getresponse()
            body = response.read().decode("utf-8")
            conn.close()

            self.assertEqual(response.status, 200)
            self.assertIn('"id": "api-pool"', body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_add_endpoint_loads_each_manual_unlock_flag_independently(self):
        pool = self.module.APIPool()
        pool.add_endpoint({"id": "locked", "name": "Locked", "in_pool": True, "manual_unlock_required": True})
        pool.add_endpoint({"id": "open", "name": "Open", "in_pool": True, "manual_unlock_required": False})

        by_id = {ep.id: ep for ep in pool._endpoints}
        self.assertTrue(by_id["locked"]._manual_unlock_required)
        self.assertFalse(by_id["open"]._manual_unlock_required)

    def test_balance_error_requires_manual_unlock(self):
        pool, endpoints = self.make_pool(1)
        endpoint = endpoints[0]

        pool._rotate(endpoint, 'HTTP 402: {"message":"insufficient balance"}')

        self.assertTrue(endpoint._manual_unlock_required)
        self.assertEqual(endpoint._cooldown_reason, "balance_insufficient")
        self.assertEqual(pool._active_endpoints(), [])
        self.assertEqual(pool._ep_to_dict(endpoint, False, time.time())["manual_unlock_required"], True)

        endpoint._cooldown_until = time.time() - 1
        pool._cleanup_expired_cooldowns()
        self.assertNotIn(endpoint.id, pool._probe_inflight)

        pool.clear_error(endpoint.id)
        self.assertFalse(endpoint._manual_unlock_required)
        self.assertEqual(endpoint._cooldown_reason, "")
        self.assertIn(endpoint, pool._active_endpoints())

    def test_balance_lock_survives_inflight_business_success(self):
        pool, endpoints = self.make_pool(1)
        endpoint = endpoints[0]
        endpoint._manual_unlock_required = True
        endpoint._cooldown_reason = "balance_insufficient"

        pool._on_success(endpoint, {"choices": []})

        self.assertTrue(endpoint._manual_unlock_required)
        self.assertEqual(endpoint._cooldown_reason, "balance_insufficient")

    def test_balance_locked_endpoint_is_visible_but_not_probed(self):
        pool, endpoints = self.make_pool(2)
        locked, available = endpoints
        locked._manual_unlock_required = True
        locked._cooldown_reason = "balance_insufficient"
        calls = []

        def probe(ep):
            calls.append(ep.id)
            return ep.id, "ok", 1, ""

        pool._check_one_health = probe
        result = pool.check_all_health()

        self.assertEqual(calls, [available.id])
        self.assertEqual({item["id"] for item in result}, {locked.id, available.id})
        locked_result = next(item for item in result if item["id"] == locked.id)
        self.assertEqual(locked_result["error"], "余额不足，仅支持手动解冻")

    def test_quota_error_uses_parsed_retry_after(self):
        pool, endpoints = self.make_pool(1)
        endpoint = endpoints[0]
        before = time.time()

        pool._rotate(endpoint, "HTTP 429: quota exceeded; retry-after: 90 seconds")

        self.assertFalse(endpoint._manual_unlock_required)
        self.assertEqual(endpoint._cooldown_reason, "quota_exceeded")
        self.assertGreaterEqual(endpoint._cooldown_until, before + 89)
        self.assertLessEqual(endpoint._cooldown_until, before + 91)

    def test_quota_error_without_duration_defaults_to_five_hours(self):
        pool, endpoints = self.make_pool(1)
        endpoint = endpoints[0]
        before = time.time()

        pool._rotate(endpoint, "HTTP 429: quota exhausted")

        self.assertEqual(endpoint._cooldown_reason, "quota_exceeded")
        self.assertGreaterEqual(endpoint._cooldown_until, before + 5 * 60 * 60 - 1)
        self.assertLessEqual(endpoint._cooldown_until, before + 5 * 60 * 60 + 1)

    def test_plain_rate_limit_keeps_normal_cooldown(self):
        pool, endpoints = self.make_pool(1)
        endpoint = endpoints[0]
        endpoint.cooldown_minutes = 3
        before = time.time()

        pool._rotate(endpoint, "HTTP 429: rate limit exceeded")

        self.assertEqual(endpoint._cooldown_reason, "")
        self.assertFalse(endpoint._manual_unlock_required)
        self.assertGreaterEqual(endpoint._cooldown_until, before + 179)
        self.assertLessEqual(endpoint._cooldown_until, before + 181)


if __name__ == "__main__":
    unittest.main()
