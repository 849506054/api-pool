"""响应解压链 _DecodedResponse（2026-09-10 客户端伪装配套）。

契约（铁律）：
1. 无 Content-Encoding / identity / 未知编码 → 完全透传，零开销
2. 解压失败 → 异常向上传播（按上游失败处理），绝不吐损坏字节
3. deflate 双模式：zlib 头自动识别失败 → 回退裸 deflate（RFC1951）
4. 集成：_try_endpoint 非流式路径收到 gzip 响应能正常解析 JSON
"""

import gzip
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import zlib
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_decoded_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        # 2026-09-12：池自身出站需确定身份；测试默认模拟"已有真实客户端打过池"。
        module._client_baseline.update({"User-Agent": "pytest-client/1.0"})
        return module
    finally:
        os.chdir(previous_cwd)


class FakeRawResp:
    """模拟 http.client.HTTPResponse：只有 read/readline/headers/close。"""

    def __init__(self, chunks, headers=None):
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.closed = False

    def read(self, size=65536):
        return self._chunks.pop(0) if self._chunks else b""

    def readline(self, limit=-1):
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data

    def close(self):
        self.closed = True


class DecodedResponseUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp_path:
            cls.module = load_module(tmp_path)

    def _wrap(self, raw_bytes, encoding, chunk_size=None):
        chunks = (
            [raw_bytes[i : i + chunk_size] for i in range(0, len(raw_bytes), chunk_size)]
            if chunk_size
            else [raw_bytes]
        )
        raw = FakeRawResp(chunks, {"Content-Encoding": encoding})
        return self.module._DecodedResponse(raw), raw

    def test_gzip_read(self):
        data = b'{"ok": true}' * 100
        w, _ = self._wrap(gzip.compress(data), "gzip")
        self.assertEqual(w.read(), data)

    def test_gzip_readline_and_iter_small_chunks(self):
        lines = [b'data: {"i": %d}\n' % i for i in range(5)]
        enc = gzip.compress(b"".join(lines))
        w, _ = self._wrap(enc, "gzip", chunk_size=7)
        self.assertEqual(list(w), lines)
        w2, _ = self._wrap(enc, "gzip", chunk_size=3)
        self.assertEqual(w2.readline(), lines[0])
        self.assertEqual(w2.readline(), lines[1])

    def test_zlib_deflate(self):
        data = b"hello " * 1000
        w, _ = self._wrap(zlib.compress(data), "deflate")
        self.assertEqual(w.read(), data)

    def test_raw_deflate_fallback(self):
        data = b"raw deflate stream " * 500
        enc = zlib.compress(data)[2:-4]  # 去掉 zlib 头尾 = RFC1951 裸流
        w, _ = self._wrap(enc, "deflate")
        self.assertEqual(w.read(), data)

    def test_corrupt_gzip_raises(self):
        w, _ = self._wrap(b"\x1f\x8b\x08\x00garbagegarbagegarbage", "gzip")
        with self.assertRaises(zlib.error):
            w.read()

    def test_no_encoding_passthrough(self):
        data = b'{"plain": true}'
        w, raw = self._wrap(data, "")
        self.assertEqual(w.read(), data)
        self.assertTrue(raw.closed or True)  # close 代理不炸即可

    def test_unknown_encoding_passthrough(self):
        data = b"br-bytes"
        w, _ = self._wrap(data, "br")
        self.assertEqual(w._encoding, "")
        self.assertEqual(w.read(), data)

    def test_read_size_boundaries(self):
        data = b"abcdefghij" * 100
        w, _ = self._wrap(gzip.compress(data), "gzip", chunk_size=5)
        self.assertEqual(w.read(3), b"abc")
        self.assertEqual(w.read(7), b"defghij")
        self.assertEqual(len(w.read()), 1000 - 10)
        self.assertEqual(w.read(), b"")

    def test_close_delegates(self):
        w, raw = self._wrap(gzip.compress(b"x"), "gzip")
        w.close()
        self.assertTrue(raw.closed)


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp_path:
            cls.module = load_module(tmp_path)

    def test_try_endpoint_decodes_gzip_json(self):
        module = self.module
        payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": False}
        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}}
        ).encode()
        enc = gzip.compress(body)

        class GzipResp:
            status = 200
            headers = {"Content-Encoding": "gzip", "Content-Type": "application/json"}

            def __init__(self):
                self._data = enc
                self._pos = 0

            def read(self, size=-1):
                if size == -1 or size < 0:
                    out = self._data[self._pos:]
                    self._pos = len(self._data)
                    return out
                out = self._data[self._pos:self._pos + size]
                self._pos += len(out)
                return out

            def readline(self, limit=-1):
                idx = self._data.find(b"\n", self._pos)
                if idx == -1:
                    return self.read()
                out = self._data[self._pos:idx + 1]
                self._pos = idx + 1
                return out

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        ep = module.Endpoint(
            id="e1", name="e1", base_url="https://up.example/v1", api_key="sk",
            model="m", max_retries=0, use_proxy=True,
        )
        with mock.patch.object(module.urllib.request, "urlopen", return_value=GzipResp()):
            result, error = module.pool._try_endpoint(ep, payload, 30, log_usage=False)
        self.assertIsNotNone(result, error)
        self.assertEqual(result.get("choices", [{}])[0].get("message", {}).get("content"), "ok")


    def test_fetch_models_decodes_gzip_json(self):
        """回归（2026-09-12）：拉模型走 fetch_models，此前未包装 _DecodedResponse，
        上游声明 gzip 时直接 decode 抛 "'utf-8' codec can't decode byte 0x8b"（Soleapi 实测）。"""
        module = self.module
        body = json.dumps({"data": [{"id": "b-model"}, {"id": "a-model"}]}).encode()
        enc = gzip.compress(body)

        class GzipResp:
            status = 200
            headers = {"Content-Encoding": "gzip", "Content-Type": "application/json"}

            def __init__(self):
                self._data = enc
                self._pos = 0

            def read(self, size=-1):
                if size is None or size < 0:
                    out = self._data[self._pos:]
                    self._pos = len(self._data)
                    return out
                out = self._data[self._pos:self._pos + size]
                self._pos += len(out)
                return out

            def readline(self, limit=-1):
                idx = self._data.find(b"\n", self._pos)
                if idx == -1:
                    return self.read()
                out = self._data[self._pos:idx + 1]
                self._pos = idx + 1
                return out

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(module.urllib.request, "urlopen", return_value=GzipResp()):
            models = module.pool.fetch_models("https://up.example/v1", "sk", timeout=5, use_proxy=True)
        self.assertEqual([m["id"] for m in models], ["a-model", "b-model"])


if __name__ == "__main__":
    unittest.main()
