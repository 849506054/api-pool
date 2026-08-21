import importlib.util
import os
import sys
import tempfile
import unittest

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_server_restart_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class PersistentCurrentEndpointTests(unittest.TestCase):
    @staticmethod
    def endpoint(module, endpoint_id, priority):
        return module.Endpoint(
            id=endpoint_id,
            name=endpoint_id,
            base_url="http://127.0.0.1:1",
            api_key="test",
            model="test",
            priority=priority,
            in_pool=True,
            use_proxy=False,
        )

    def test_restored_current_endpoint_stays_first_across_requests(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            first = self.endpoint(module, "first", 1)
            restored = self.endpoint(module, "restored", 3)
            pool = module.APIPool([first, restored])
            pool._current_endpoint_id = restored.id
            pool._manual_override_id = restored.id
            calls = []

            def fake_try(ep, payload, timeout, **kwargs):
                calls.append(ep.id)
                return {"choices": [{"message": {"content": "ok"}}]}, ""

            pool._try_endpoint = fake_try
            pool.chat([{"role": "user", "content": "one"}])
            pool.chat([{"role": "user", "content": "two"}])

            self.assertEqual(calls, ["restored", "restored"])
            self.assertEqual(pool._manual_override_id, restored.id)
            self.assertEqual(pool._current_endpoint_id, restored.id)

    def test_restored_endpoint_failure_clears_override_and_fails_over(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            first = self.endpoint(module, "first", 1)
            restored = self.endpoint(module, "restored", 3)
            pool = module.APIPool([first, restored])
            pool._current_endpoint_id = restored.id
            pool._manual_override_id = restored.id
            calls = []

            def fake_try(ep, payload, timeout, **kwargs):
                calls.append(ep.id)
                if ep is restored:
                    return None, "HTTP 500"
                return {"choices": [{"message": {"content": "ok"}}]}, ""

            pool._try_endpoint = fake_try
            result = pool.chat([{"role": "user", "content": "fail over"}])

            self.assertEqual(result["choices"][0]["message"]["content"], "ok")
            self.assertEqual(calls[0], "restored")
            self.assertIn("first", calls[1:])
            self.assertIsNone(pool._manual_override_id)
            self.assertEqual(pool._current_endpoint_id, first.id)

    def test_manual_switch_replaces_restored_current_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            first = self.endpoint(module, "first", 1)
            restored = self.endpoint(module, "restored", 3)
            pool = module.APIPool([first, restored])
            pool._current_endpoint_id = restored.id
            pool._manual_override_id = restored.id

            self.assertTrue(pool.switch_to_endpoint(first.id))
            self.assertEqual(pool._manual_override_id, first.id)
            self.assertEqual(pool._current_endpoint_id, first.id)


if __name__ == "__main__":
    unittest.main()
