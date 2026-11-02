"""Client identity golden samples over isolated HTTP, plus compressed response checks."""
import gzip
import http.client
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import brotli
import zstandard

SOURCE = Path(os.environ.get("API_POOL_TEST_SOURCE", Path(__file__).resolve().parents[1] / "api_pool_server.py"))
IDENTITY = {
    "User-Agent": "identity-golden/1.0",
    "Accept-Encoding": "gzip, deflate;q=0.8, br, zstd;q=0.9",
    "Anthropic-Version": "2023-01-01",
    "Anthropic-Beta": "tools-2024-04-04",
    "X-Stainless-Package-Version": "2.24.0",
    "X-Client-Session": "sample-session",
}


class IdentityWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="identity-wire-")
        cls.old_cwd = os.getcwd()
        cls.old_db = os.environ.get("API_POOL_DB_DIR")
        os.chdir(cls.tmp.name)
        os.environ["API_POOL_DB_DIR"] = cls.tmp.name
        spec = importlib.util.spec_from_file_location("pool_identity_wire", SOURCE)
        assert spec is not None and spec.loader is not None
        cls.mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.mod
        spec.loader.exec_module(cls.mod)
        cls.calls = []
        cls.bodies = []
        cls.encoding = ""
        cls.error_response = False

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                cls.calls.append({k.lower(): v for k, v in self.headers.items()})
                data = b'{"data":[{"id":"m"}]}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                cls.calls.append({k.lower(): v for k, v in self.headers.items()})
                cls.bodies.append(body)
                if cls.error_response:
                    data = json.dumps({"error": {"message": "identity-error-golden"}}).encode()
                elif self.path.endswith("/messages"):
                    data = json.dumps({"content": [{"type": "text", "text": "identity-ok"}],
                                       "stop_reason": "end_turn", "usage": {}}).encode()
                elif self.path.endswith("/responses"):
                    data = json.dumps({"id": "r1", "output": [{"type": "message", "content": [
                        {"type": "output_text", "text": "identity-ok"}]}], "usage": {}}).encode()
                elif ":generateContent" in self.path:
                    data = json.dumps({"candidates": [{"content": {"parts": [{"text": "identity-ok"}]},
                                                        "finishReason": "STOP"}]}).encode()
                elif body.get("stream"):
                    frames = [{"choices": [{"index": 0, "delta": {"content": "identity-ok"}, "finish_reason": None}]},
                              {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
                    data = b"".join(b"data: " + json.dumps(f).encode() + b"\n\n" for f in frames) + b"data: [DONE]\n\n"
                else:
                    data = json.dumps({"choices": [{"message": {"content": "identity-ok"}, "finish_reason": "stop"}],
                                       "usage": {}}).encode()
                if cls.encoding == "br":
                    data = brotli.compress(data)
                elif cls.encoding == "zstd":
                    data = zstandard.ZstdCompressor().compress(data)
                self.send_response(400 if cls.error_response else 200)
                self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json")
                self.send_header("Content-Length", str(len(data)))
                if cls.encoding:
                    self.send_header("Content-Encoding", cls.encoding)
                self.end_headers()
                self.wfile.write(data)

        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        cls.ep = cls.mod.Endpoint(id="identity", name="identity", base_url=f"http://127.0.0.1:{cls.upstream.server_port}/v1",
                                  model="m", api_key="isolated-endpoint-key", use_proxy=False, max_retries=0, health_mode="none")
        cls.mod.pool = cls.mod.APIPool([cls.ep])
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
        type(self).encoding = ""
        type(self).error_response = False
        self.ep.protocol = "openai"
        self.ep.health_mode = "none"
        self.ep.use_proxy = False
        self.ep.client_profile = ""
        self.ep.default_headers = {}
        self.ep.extra_headers = {}
        self.mod.pool._client_profiles = {}

    def request(self, headers=None, stream=False, route="chat/completions", extra=None):
        body = {"model": "m", "stream": stream}
        body.update({"input": "hi"} if route == "responses" else {"messages": [{"role": "user", "content": "hi"}]})
        body.update(extra or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=10)
        try:
            connection.request("POST", f"/endpoints/identity/v1/{route}", body=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            result = response.read().decode()
            self.assertEqual(response.status, 200, result)
            return result
        finally:
            connection.close()

    def test_identity_values_across_all_protocols(self):
        for protocol in ("openai", "anthropic", "responses", "gemini"):
            with self.subTest(protocol=protocol):
                self.ep.protocol = protocol
                self.assertIn("identity-ok", self.request(IDENTITY))
                for key, value in IDENTITY.items():
                    self.assertEqual(self.calls[-1].get(key.lower()), value, key)

    def test_managed_credentials_and_connection_tokens(self):
        self.assertIn("identity-ok", self.request({**IDENTITY, "Authorization": "Bearer local-client-key",
            "X-Api-Key": "local-key", "X-Goog-Api-Key": "local-google-key", "Proxy-Authorization": "proxy-key",
            "Connection": "X-Hop-Only, keep-alive", "X-Hop-Only": "connection-secret"}))
        seen = self.calls[-1]
        self.assertEqual(seen["authorization"], "Bearer isolated-endpoint-key")
        for name in ("x-api-key", "x-goog-api-key", "proxy-authorization", "x-hop-only"):
            self.assertNotIn(name, seen)

    def test_profile_isolation_and_endpoint_override(self):
        self.ep.client_profile = "sample"
        self.mod.pool._client_profiles = {"sample": {"headers": {"User-Agent": "profile/1", "X-Profile": "yes"}}}
        self.ep.extra_headers = {"X-Profile": "override"}
        self.assertIn("identity-ok", self.request(IDENTITY))
        self.assertEqual(self.calls[-1]["user-agent"], "profile/1")
        self.assertEqual(self.calls[-1]["x-profile"], "override")
        self.assertNotIn("x-client-session", self.calls[-1])

    def test_anthropic_fallback_version(self):
        self.ep.protocol = "anthropic"
        self.assertIn("identity-ok", self.request({"User-Agent": "sample/1"}))
        self.assertEqual(self.calls[-1]["anthropic-version"], "2023-06-01")

    def test_brotli_zstd_json_and_sse(self):
        for encoding in ("br", "zstd"):
            type(self).encoding = encoding
            for stream in (False, True):
                for route in ("chat/completions", "responses"):
                    with self.subTest(encoding=encoding, stream=stream, route=route):
                        result = self.request(IDENTITY, stream=stream, route=route)
                        self.assertIn("identity-ok", result)
                        if stream:
                            self.assertIn("response.completed" if route == "responses" else "[DONE]", result)

    def test_compressed_error_body(self):
        self.ep.health_mode = "chat"
        for encoding in ("br", "zstd"):
            with self.subTest(encoding=encoding):
                type(self).encoding = encoding
                type(self).error_response = True
                self.mod.set_client_headers(IDENTITY)
                try:
                    result, error = self.mod.pool._try_endpoint(self.ep, {"model": "m", "messages": []}, 5, log_usage=False)
                finally:
                    self.mod.clear_client_headers()
                self.assertIsNone(result)
                self.assertIn("identity-error-golden", error)

    def test_absent_user_agent_stays_absent(self):
        self.assertIn("identity-ok", self.request({"X-Client-Session": "sample-session"}))
        self.assertNotIn("user-agent", self.calls[-1])

    def test_absent_user_agent_with_proxy_enabled(self):
        self.ep.use_proxy = True
        with mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}):
            self.assertIn("identity-ok", self.request({"X-Client-Session": "sample-session"}))
        self.assertNotIn("user-agent", self.calls[-1])

    def test_accept_encoding_keeps_case_quality_and_spacing(self):
        header = "GZip;q=1.0,  br;q=0.8, zstd;q=0, *;q=0.1"
        self.assertIn("identity-ok", self.request({**IDENTITY, "Accept-Encoding": header}))
        self.assertEqual(self.calls[-1]["accept-encoding"], header)

    def test_incremental_compressed_first_frame(self):
        first = b"data: first\n\n"
        last = b"data: last\n\n"
        brotli_stream = brotli.Compressor()
        zstd_stream = zstandard.ZstdCompressor().compressobj()
        pairs = [
            ("br", brotli_stream.process(first) + brotli_stream.flush(), brotli_stream.process(last) + brotli_stream.finish()),
            ("zstd", zstd_stream.compress(first) + zstd_stream.flush(zstandard.COMPRESSOBJ_FLUSH_BLOCK),
             zstd_stream.compress(last) + zstd_stream.flush()),
        ]
        for encoding, part1, part2 in pairs:
            with self.subTest(encoding=encoding):
                class Raw:
                    def __init__(self, encoding=encoding, part1=part1, part2=part2):
                        self.headers = {"Content-Encoding": encoding}
                        self.parts = [part1, part2, b""]
                        self.reads = 0

                    def read(self, size=-1):
                        raise AssertionError("stream must use read1")

                    def read1(self, size=-1):
                        self.reads += 1
                        return self.parts.pop(0)

                raw = Raw()
                decoded = self.mod._DecodedResponse(raw)
                self.assertEqual(decoded.readline(), b"data: first\n")
                self.assertEqual(raw.reads, 1)
                self.assertEqual(decoded.read(), b"\n" + last)

    def test_zstd_concatenated_frames(self):
        encoded = b"".join(zstandard.ZstdCompressor().compress(x) for x in (b"one", b"two"))
        raw = io.BytesIO(encoded)
        raw.headers = {"Content-Encoding": "zstd"}
        self.assertEqual(self.mod._DecodedResponse(raw).read(), b"onetwo")

    def test_fragmented_decoders_and_truncation(self):
        data = b"data: identity-ok\n\n" * 30

        class Raw(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 3) if size is not None and size >= 0 else 3)

            read1 = read

        for encoding, encoded in (("br", brotli.compress(data)),
                                   ("zstd", zstandard.ZstdCompressor().compress(data)),
                                   ("gzip, br", brotli.compress(gzip.compress(data)))):
            with self.subTest(encoding=encoding):
                raw = Raw(encoded)
                raw.headers = {"Content-Encoding": encoding}
                self.assertEqual(b"".join(self.mod._DecodedResponse(raw)), data)
                raw = Raw(encoded[:-1])
                raw.headers = {"Content-Encoding": encoding}
                with self.assertRaises((ValueError, brotli.error, zstandard.ZstdError)):
                    self.mod._DecodedResponse(raw).read()


if __name__ == "__main__":
    unittest.main()
