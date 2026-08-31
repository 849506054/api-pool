import importlib.util
import os
import sys
import tempfile
import threading
import unittest

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_current_endpoint_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class CurrentEndpointRoutingTests(unittest.TestCase):
    @staticmethod
    def endpoint(module, endpoint_id, priority, model):
        return module.Endpoint(
            id=endpoint_id,
            name=endpoint_id,
            base_url="http://127.0.0.1:1",
            api_key="test",
            model=model,
            priority=priority,
            in_pool=True,
            use_proxy=False,
        )

    def test_successful_current_endpoint_stays_first_on_next_request(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            low = self.endpoint(module, "low", 1, "deepseek-v4-flash")
            current = self.endpoint(module, "current", 3, "gpt-5.6-sol")
            pool = module.APIPool([low, current])
            pool._current_endpoint_id = current.id
            calls = []

            def fake_try(ep, payload, timeout, **kwargs):
                calls.append(ep.id)
                return {"choices": [{"message": {"content": "ok"}}]}, ""

            pool._try_endpoint = fake_try
            pool.chat([{"role": "user", "content": "one"}])
            pool.chat([{"role": "user", "content": "two"}])

            self.assertEqual(calls, ["current", "current"])

    def test_failover_keeps_same_model_candidate_before_lower_priority_model(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            failed = self.endpoint(module, "failed", 5, "claude-opus-5")
            same_model = self.endpoint(module, "same-model", 6, "claude-opus-5")
            lower_priority = self.endpoint(module, "other-model", 1, "deepseek-v4-flash")
            pool = module.APIPool([lower_priority, failed, same_model])
            pool._current_endpoint_id = failed.id
            calls = []

            def fake_try(ep, payload, timeout, **kwargs):
                calls.append(ep.id)
                if ep is failed:
                    return None, "connection failed"
                return {"choices": [{"message": {"content": "ok"}}]}, ""

            pool._try_endpoint = fake_try
            pool._probe_endpoint = lambda ep: (True, "")
            pool.chat([{"role": "user", "content": "one"}])

            self.assertEqual(calls, ["failed", "same-model"])

    def test_background_recovery_does_not_replace_healthy_current_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            recovered = self.endpoint(module, "recovered", 1, "deepseek-v4-flash")
            current = self.endpoint(module, "current", 3, "gpt-5.6-sol")
            pool = module.APIPool([recovered, current])
            pool._current_endpoint_id = current.id
            pool._last_pool_activity = module.time.time()
            recovered._cooldown_until = module.time.time() - 1
            pool._probe_endpoint = lambda ep: (True, "")

            pool._background_probe(recovered, current.id)

            self.assertEqual(pool._current_endpoint_id, current.id)
            self.assertGreater(recovered._defer_until, module.time.time())

    def test_current_endpoint_cache_protection_controls_deferred_failback(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            recovered = self.endpoint(module, "recovered", 1, "deepseek-v4-flash")
            current = self.endpoint(module, "current", 3, "gpt-5.6-sol")
            recovered.deferrable = False
            current.deferrable = True
            pool = module.APIPool([recovered, current])
            pool._current_endpoint_id = current.id
            pool._last_pool_activity = module.time.time()
            pool._probe_endpoint = lambda ep: (True, "")

            pool._background_probe(recovered, current.id)

            self.assertGreater(recovered._defer_until, module.time.time())
            self.assertEqual(pool._current_endpoint_id, current.id)

    def test_current_endpoint_without_cache_protection_immediately_fails_back(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            recovered = self.endpoint(module, "recovered", 1, "deepseek-v4-flash")
            current = self.endpoint(module, "current", 3, "gpt-5.6-sol")
            recovered.deferrable = True
            current.deferrable = False
            pool = module.APIPool([recovered, current])
            pool._current_endpoint_id = current.id
            pool._last_pool_activity = module.time.time()
            pool._probe_endpoint = lambda ep: (True, "")

            pool._background_probe(recovered, current.id)

            self.assertEqual(recovered._defer_until, 0)
            self.assertEqual(pool._current_endpoint_id, recovered.id)

    def test_reconciliation_uses_current_endpoint_cache_protection(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            recovered = self.endpoint(module, "recovered", 1, "deepseek-v4-flash")
            current = self.endpoint(module, "current", 3, "gpt-5.6-sol")
            recovered.deferrable = False
            current.deferrable = False
            recovered._defer_until = module.time.time() + 300
            pool = module.APIPool([recovered, current])
            pool._current_endpoint_id = current.id
            pool._last_pool_activity = module.time.time()

            pool._reconcile_deferred()

            self.assertEqual(recovered._defer_until, 0)
            self.assertEqual(pool._current_endpoint_id, recovered.id)

    def test_manual_override_is_not_replaced_by_background_recovery(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            recovered = self.endpoint(module, "recovered", 1, "deepseek-v4-flash")
            current = self.endpoint(module, "current", 3, "gpt-5.6-sol")
            current.deferrable = False
            pool = module.APIPool([recovered, current])
            pool._current_endpoint_id = current.id
            pool._manual_override_id = current.id
            pool._last_pool_activity = module.time.time()
            pool._probe_endpoint = lambda ep: (True, "")

            pool._background_probe(recovered, current.id)

            self.assertEqual(recovered._defer_until, 0)
            self.assertEqual(pool._current_endpoint_id, current.id)
            self.assertEqual(pool._manual_override_id, current.id)

    def test_deferred_recovery_does_not_proactively_replace_healthy_current(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            recovered = self.endpoint(module, "recovered", 1, "deepseek-v4-flash")
            current = self.endpoint(module, "current", 3, "gpt-5.6-sol")
            pool = module.APIPool([recovered, current])
            pool._current_endpoint_id = current.id
            pool._last_pool_activity = module.time.time()
            recovered._defer_until = module.time.time() + 300
            calls = []

            def fake_try(ep, payload, timeout, **kwargs):
                calls.append(ep.id)
                return {"choices": [{"message": {"content": "ok"}}]}, ""

            pool._try_endpoint = fake_try
            pool.chat([{"role": "user", "content": "one"}])

            self.assertEqual(calls, ["current"])
            self.assertGreater(recovered._defer_until, module.time.time())

    def test_current_failure_can_fail_over_to_deferred_recovery(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            deferred = self.endpoint(module, "deferred", 1, "gpt-5.6-sol")
            current = self.endpoint(module, "current", 3, "gpt-5.6-sol")
            pool = module.APIPool([deferred, current])
            pool._current_endpoint_id = current.id
            pool._last_pool_activity = module.time.time()
            deferred._defer_until = module.time.time() + 300
            calls = []

            def fake_try(ep, payload, timeout, **kwargs):
                calls.append(ep.id)
                if ep is current:
                    return None, "HTTP 503: simulated"
                return {"choices": [{"message": {"content": "ok"}}]}, ""

            pool._try_endpoint = fake_try
            pool.chat([{"role": "user", "content": "one"}])

            self.assertEqual(calls, ["current", "deferred"])
            self.assertEqual(deferred._defer_until, 0)

    def test_failover_still_excludes_cooldown_quota_rpm_and_manual_lock(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            failed = self.endpoint(module, "failed", 1, "gpt-5.6-sol")
            deferred = self.endpoint(module, "deferred", 2, "gpt-5.6-sol")
            cooldown = self.endpoint(module, "cooldown", 3, "gpt-5.6-sol")
            quota = self.endpoint(module, "quota", 4, "gpt-5.6-sol")
            rpm = self.endpoint(module, "rpm", 5, "gpt-5.6-sol")
            locked = self.endpoint(module, "locked", 6, "gpt-5.6-sol")
            pool = module.APIPool([failed, deferred, cooldown, quota, rpm, locked])
            deferred._defer_until = module.time.time() + 300
            cooldown._cooldown_until = module.time.time() + 300
            quota.daily_limit = 1
            quota._today_used = 1
            rpm.rpm_limit = 1
            rpm._req_timestamps.append(module.time.time())
            locked._manual_unlock_required = True

            candidates = pool._failover_endpoints()

            self.assertEqual([ep.id for ep in candidates], ["failed", "deferred"])

    def test_late_success_does_not_override_newer_manual_switch(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            old = self.endpoint(module, "old", 1, "claude-opus-5")
            manual = self.endpoint(module, "manual", 2, "gpt-5.6-sol")
            pool = module.APIPool([old, manual])
            pool._set_current(pool.MAIN_GROUP, old.id)
            request_epoch = pool._get_route_epoch(pool.MAIN_GROUP)

            self.assertTrue(pool.switch_to_endpoint(manual.id))
            pool._on_success(old, {"choices": []}, expected_route_epoch=request_epoch)

            self.assertEqual(pool._get_current(pool.MAIN_GROUP), manual.id)
            self.assertEqual(pool._get_manual(pool.MAIN_GROUP), manual.id)

    def test_chat_inflight_success_does_not_override_manual_switch(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            old = self.endpoint(module, "old", 1, "claude-opus-5")
            manual = self.endpoint(module, "manual", 2, "gpt-5.6-sol")
            pool = module.APIPool([old, manual])
            pool._set_current(pool.MAIN_GROUP, old.id)
            entered = threading.Event()
            release = threading.Event()

            def fake_try(ep, payload, timeout, **kwargs):
                entered.set()
                release.wait(2)
                return {"choices": [{"message": {"content": "ok"}}]}, ""

            pool._try_endpoint = fake_try
            worker = threading.Thread(
                target=pool.chat,
                args=([{"role": "user", "content": "one"}],),
            )
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertTrue(pool.switch_to_endpoint(manual.id))
            release.set()
            worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(pool._get_current(pool.MAIN_GROUP), manual.id)
            self.assertEqual(pool._get_manual(pool.MAIN_GROUP), manual.id)

    def test_late_failure_does_not_override_newer_manual_switch(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            old = self.endpoint(module, "old", 1, "claude-opus-5")
            manual = self.endpoint(module, "manual", 2, "gpt-5.6-sol")
            candidate = self.endpoint(module, "candidate", 3, "glm-5.3-flash")
            pool = module.APIPool([old, manual, candidate])
            pool._set_current(pool.MAIN_GROUP, old.id)
            request_epoch = pool._get_route_epoch(pool.MAIN_GROUP)

            self.assertTrue(pool.switch_to_endpoint(manual.id))
            pool._rotate(old, "HTTP 503: simulated", expected_route_epoch=request_epoch)

            self.assertEqual(pool._get_current(pool.MAIN_GROUP), manual.id)
            self.assertEqual(pool._get_manual(pool.MAIN_GROUP), manual.id)

    def test_late_success_does_not_override_background_failback(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            recovered = self.endpoint(module, "recovered", 1, "gpt-5.6-sol")
            old = self.endpoint(module, "old", 2, "claude-opus-5")
            old.deferrable = False
            pool = module.APIPool([recovered, old])
            pool._set_current(pool.MAIN_GROUP, old.id)
            request_epoch = pool._get_route_epoch(pool.MAIN_GROUP)
            pool._last_pool_activity = module.time.time()
            pool._probe_endpoint = lambda ep: (True, "")

            pool._background_probe(recovered, old.id)
            pool._on_success(old, {"choices": []}, expected_route_epoch=request_epoch)

            self.assertEqual(pool._get_current(pool.MAIN_GROUP), recovered.id)
            self.assertIsNone(pool._get_manual(pool.MAIN_GROUP))


if __name__ == "__main__":
    unittest.main()
