import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_stream_timeout_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class FakeSocket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class FakeStreamResponse:
    def __init__(self, first_line=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n'):
        self.first_line = first_line
        self.socket = FakeSocket()

    def readline(self):
        return self.first_line

    def __iter__(self):
        return iter([b"data: [DONE]\n"])

    def close(self):
        pass


class StreamTimeoutPolicyTests(unittest.TestCase):
    def test_default_stream_total_duration_is_unlimited(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            self.assertEqual(module.Endpoint().stream_max_duration, 0)

    def test_first_packet_timeout_is_independent_from_connection_timeout(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            endpoint = module.Endpoint(
                id="stream", name="stream", base_url="http://example/v1", api_key="x",
                model="m", timeout=60, max_retries=0, stream_first_packet_timeout=120,
                stream_stall_timeout=0, stream_max_duration=0, in_pool=True, use_proxy=True,
            )
            response = FakeStreamResponse()
            payload = {"model": "m", "messages": [], "stream": True}
            with mock.patch.object(module.urllib.request, "urlopen", return_value=response), \
                    mock.patch.object(module, "_get_resp_socket", return_value=response.socket):
                result, error = module.APIPool()._try_endpoint(endpoint, payload, 60, log_usage=False)
                self.assertEqual(error, "")
                list(result)
            self.assertEqual(response.socket.timeouts, [120])


if __name__ == "__main__":
    unittest.main()
