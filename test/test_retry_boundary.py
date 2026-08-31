import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_retry_boundary_{id(tmp_path)}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        module.CONFIG_FILE = os.path.join(tmp_path, "api_config.json")
        module.RUNTIME_STATE_FILE = os.path.join(tmp_path, "api_runtime_state.json")
        return module
    finally:
        os.chdir(previous_cwd)


class FakeResponse:
    def __init__(self, body=None):
        self.body = body or {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.body).encode()


class RetryBoundaryTests(unittest.TestCase):
    def test_max_retries_normalized_on_add_and_update(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool()
            pool.add_endpoint({"id": "high", "name": "high", "max_retries": 99})
            pool.add_endpoint({"id": "low", "name": "low", "max_retries": -4})
            pool.add_endpoint({"id": "bad", "name": "bad", "max_retries": "bad"})
            self.assertEqual([ep.max_retries for ep in pool._endpoints], [3, 0, 1])
            pool.update_endpoint("low", {"max_retries": 8})
            self.assertEqual(next(ep for ep in pool._endpoints if ep.id == "low").max_retries, 3)

    def test_remaining_request_budget_caps_upstream_timeout(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool()
            endpoint = module.Endpoint(
                id="budget", name="budget", base_url="http://example/v1", api_key="x",
                model="m", timeout=60, max_retries=0, use_proxy=True,
            )
            seen_timeouts = []

            def fake_urlopen(_request, timeout):
                seen_timeouts.append(timeout)
                return FakeResponse()

            deadline = time.time() + 2
            with mock.patch.object(module.urllib.request, "urlopen", side_effect=fake_urlopen):
                result, error = pool._try_endpoint(
                    endpoint, {"model": "m", "messages": [], "stream": False}, 60,
                    log_usage=False, request_deadline=deadline,
                )
            self.assertEqual(error, "")
            self.assertIsNotNone(result)
            self.assertEqual(len(seen_timeouts), 1)
            self.assertGreater(seen_timeouts[0], 0)
            self.assertLessEqual(seen_timeouts[0], 2)

    def test_expired_request_budget_skips_upstream_call(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool()
            endpoint = module.Endpoint(
                id="expired", name="expired", base_url="http://example/v1", api_key="x",
                model="m", max_retries=0,
            )
            with mock.patch.object(module.urllib.request, "urlopen") as urlopen:
                result, error = pool._try_endpoint(
                    endpoint, {"model": "m", "messages": []}, 60,
                    request_deadline=time.time() - 1,
                )
            self.assertIsNone(result)
            self.assertIn("request budget exhausted", error)
            urlopen.assert_not_called()

    def test_temperature_cleanup_recursion_keeps_request_context(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool()
            endpoint = module.Endpoint(
                id="clean", name="clean", base_url="http://example/v1", api_key="x",
                model="m", max_retries=1, use_proxy=True,
            )
            http_error = urllib.error.HTTPError(
                "http://example/v1/chat/completions", 400, "bad", {},
                io.BytesIO(b'{"error":"temperature is unsupported"}'),
            )
            timeouts = []

            def fake_urlopen(_request, timeout):
                timeouts.append(timeout)
                if len(timeouts) == 1:
                    raise http_error
                return FakeResponse()

            deadline = time.time() + 3
            with mock.patch.object(module.urllib.request, "urlopen", side_effect=fake_urlopen):
                result, error = pool._try_endpoint(
                    endpoint,
                    {"model": "m", "messages": [], "temperature": 0.2},
                    60, log_usage=False, request_id="cleanreq", request_deadline=deadline,
                )
            self.assertEqual(error, "")
            self.assertIsNotNone(result)
            self.assertEqual(len(timeouts), 2)
            self.assertTrue(all(0 < timeout <= 3 for timeout in timeouts))

    def test_image_translation_forwards_request_context(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool()
            vision = module.Endpoint(id="vision", name="vision", model="vm", is_vision=True)
            calls = []

            def fake_try(*_args, **kwargs):
                calls.append(kwargs)
                return {"choices": [{"message": {"content": "description"}}]}, ""

            pool._try_endpoint = fake_try
            deadline = time.time() + 30
            translated = pool._translate_images_sync(
                [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]}],
                [vision], "main", request_id="visionreq", request_deadline=deadline,
            )
            self.assertIn("图片解析内容", translated[0]["content"][-1]["text"])
            self.assertEqual(calls[0]["request_id"], "visionreq")
            self.assertEqual(calls[0]["request_deadline"], deadline)
            self.assertTrue(calls[0]["force_no_retry"])


if __name__ == "__main__":
    unittest.main()
