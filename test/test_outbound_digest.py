import importlib.util
import io
import os
import tempfile
import unittest
from unittest import mock


MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_outbound_digest_test", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._client_baseline.update({"User-Agent": "pytest-client/1.0"})
        return module
    finally:
        os.chdir(old_cwd)


def http_error(url, code, body=b"{}"):
    return module_level_urlerror(url, code, body)


def module_level_urlerror(url, code, body):
    import urllib.error
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(body))


class OutboundDigestTests(unittest.TestCase):
    """失败尝试的出站摘要（2026-09-13 ps.air /responses 405 边缘拦截排查用）。"""

    def test_digest_reports_sizes_without_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            secret = "SENSITIVE_PROMPT_TEXT_9381"
            body = {
                "model": "gpt-6-astra",
                "instructions": secret,
                "input": [{"role": "user", "content": secret}] * 3,
                "tools": [{"type": "function", "name": "t"}],
                "stream": True,
            }
            data = module.json.dumps(body, ensure_ascii=False).encode("utf-8")
            digest = module.APIPool._outbound_digest("https://up.example/v1/responses", data)
        self.assertIn("url=https://up.example/v1/responses", digest)
        self.assertIn(f"body_bytes={len(data)}", digest)
        self.assertIn("instructions=", digest)
        self.assertIn("input=3项/", digest)
        self.assertIn("tools=1项/", digest)
        self.assertIn("stream=True", digest)
        self.assertNotIn(secret, digest)

    def test_digest_never_raises_on_garbage(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            digest = module.APIPool._outbound_digest("https://up.example/v1/x", b"not-json")
        self.assertIn("body_bytes=8", digest)

    def test_failed_attempt_logs_digest_and_probe_marker(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = module.Endpoint(
                id="p1", name="probe-target", base_url="https://up.example/v1", api_key="k",
                model="gpt-6-astra", protocol="responses", max_retries=0, use_proxy=True, in_pool=True,
            )
            pool = module.APIPool([ep])
            logs = []
            module.sys_log = lambda msg, level="INFO": logs.append((msg, level))
            payload = {"model": "gpt-6-astra", "messages": [{"role": "user", "content": "ping"}], "stream": True}
            with mock.patch.object(module.urllib.request, "urlopen",
                                   side_effect=module_level_urlerror("https://up.example/v1/responses", 405, b"<html>405</html>")):
                real = pool._try_endpoint(ep, payload, 1, log_usage=False, force_no_retry=True, request_id="deadbeef")
                probe = pool._try_endpoint(ep, payload, 1, log_usage=False, force_no_retry=True,
                                           is_probe=True, request_id="deadbeef")
        self.assertIsNone(real[0])
        self.assertIsNone(probe[0])
        digests = [msg for msg, _ in logs if "出站摘要(失败" in msg]
        self.assertEqual(len(digests), 2, digests)
        self.assertIn("url=https://up.example/v1/responses", digests[0])
        self.assertIn("body_bytes=", digests[0])
        self.assertNotIn("(探活)", digests[0])
        self.assertIn("(探活)", digests[1])
        self.assertTrue(all(level == "WARN" for msg, level in logs if "出站摘要(失败" in msg))

    def test_successful_attempt_logs_no_digest(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = module.Endpoint(
                id="p2", name="ok-target", base_url="https://up.example/v1", api_key="k",
                model="m", protocol="openai", max_retries=0, use_proxy=True, in_pool=True,
            )
            pool = module.APIPool([ep])
            logs = []
            module.sys_log = lambda msg, level="INFO": logs.append((msg, level))
            resp = mock.MagicMock()
            resp.read.return_value = module.json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": "hi"}}], "usage": {}}
            ).encode("utf-8")
            resp.headers = {}
            resp.__enter__ = lambda self: self
            resp.__exit__ = lambda self, *a: False
            with mock.patch.object(module.urllib.request, "urlopen", return_value=resp):
                result, error = pool._try_endpoint(ep, {"model": "m", "messages": []}, 1,
                                                   log_usage=False, force_no_retry=True)
        self.assertEqual(error, "")
        self.assertIsNotNone(result)
        self.assertEqual([msg for msg, _ in logs if "出站摘要" in msg], [])


if __name__ == "__main__":
    unittest.main()
