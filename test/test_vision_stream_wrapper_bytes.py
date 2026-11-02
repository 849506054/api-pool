"""共同流链的图片完成提示必须为合法 bytes SSE，并安全关闭目标流。"""

import ast
import importlib.util
import json
import os
import sys
import tempfile
import textwrap
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from openai import OpenAI

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")
DONE_FRAME = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
PNG_DATA_URL = ("data:image/png;base64,"
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_server_vision_stream_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module._client_baseline.update({"User-Agent": "pytest-client/1.0"})
        return module
    finally:
        os.chdir(previous_cwd)


def load_stream_helpers():
    """隔离加载共同流链和 SSE 编码辅助；实线错误行为由 loopback 用例覆盖。"""
    with open(MODULE_PATH, encoding="utf-8") as source_file:
        source = source_file.read()
    tree = ast.parse(source)
    namespace = {"json": json}
    for name in ("_vision_notice", "_wrap_stream_release"):
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        segment = ast.get_source_segment(source, node)
        assert segment is not None
        exec(compile(textwrap.dedent(segment), "<stream_helpers>", "exec"), namespace)  # noqa: S102 — trusted local source
    return namespace


class VisionStreamWrapperBytesTest(unittest.TestCase):
    def test_notice_frames_are_valid_bytes_sse_json(self):
        notice = load_stream_helpers()["_vision_notice"]
        for text in ('图片解析完成\n\n', 'quoted "content"\nbackslash\\'):
            frame = notice(text)
            self.assertIsInstance(frame, bytes)
            self.assertTrue(frame.startswith(b"data: "))
            self.assertTrue(frame.endswith(b"\n\n"))
            self.assertEqual(json.loads(frame[6:])["choices"][0]["delta"]["content"], text)

    def test_common_stream_release_on_done_exception_and_notice_close(self):
        helpers = load_stream_helpers()
        for mode in ("done", "exception", "notice-close"):
            with self.subTest(mode=mode):
                released = []
                closed = []
                stub = types.SimpleNamespace(_lock=threading.Lock(),
                    _release_inflight=lambda *args, released=released: released.append(args))
                def target(mode=mode, closed=closed):
                    try:
                        yield DONE_FRAME
                        if mode == "exception":
                            raise RuntimeError("stream broke")
                    finally:
                        closed.append(True)
                upstream = target()
                # _try_endpoint validates raw upstream bytes before constructing this generator.
                stream = helpers["_wrap_stream_release"](stub, upstream, "ep", "main",
                    prefix=helpers["_vision_notice"]("图片解析完成"))
                if mode == "notice-close":
                    self.assertIsInstance(next(stream), bytes)
                    stream.close()
                elif mode == "exception":
                    with self.assertRaises(RuntimeError):
                        list(stream)
                else:
                    self.assertTrue(all(isinstance(frame, bytes) for frame in stream))
                self.assertEqual(closed, [True])
                self.assertEqual(released, [("ep", "main")])


class _MockUpstream(BaseHTTPRequestHandler):
    """目标端点：SSE 流；视觉端点：非流式 JSON。"""

    def log_message(self, format, *args):
        del format, args

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if body.get("stream"):
            def frame(delta, finish=None):
                chunk = {
                    "id": "chatcmpl-upstream", "object": "chat.completion.chunk",
                    "created": 1, "model": "mock-model",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                return b"data: " + json.dumps(chunk).encode() + b"\n\n"

            raw = b"".join([
                frame({"role": "assistant", "content": ""}),
                frame({"content": "TARGET-ANSWER"}),
                frame({}, finish="stop"),
                b"data: [DONE]\n\n",
            ])
            content_type = "text/event-stream"
        else:
            raw = json.dumps({
                "id": "chatcmpl-vision", "object": "chat.completion", "created": 1,
                "model": "mock-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "IMAGE-DESCRIPTION"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            content_type = "application/json"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class VisionStreamInterceptEndToEndTest(unittest.TestCase):
    """带图流式请求经「盲端点 + 视觉池」拦截后，下游必须收到完整流。"""

    def _endpoint(self, module, name, groups, base_url, is_vision):
        return module.Endpoint(
            id=name, name=name, base_url=base_url, api_key="test", model="mock-model",
            in_pool=True, pool_groups=groups, use_proxy=False, is_vision=is_vision,
            max_retries=0, stream_first_packet_timeout=5,
            stream_stall_timeout=0, stream_max_duration=0,
        )

    def test_streaming_image_request_survives_intercept(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _MockUpstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        base_url = f"http://127.0.0.1:{upstream.server_port}/v1"
        try:
            with tempfile.TemporaryDirectory() as tmp_path:
                module = load_module(tmp_path)
                module.pool = module.APIPool()
                module.pool.add_endpoint(self._endpoint(module, "blind", ["main"], base_url, False))
                module.pool.add_endpoint(self._endpoint(module, "vision-mock", ["vision"], base_url, True))
                pool_server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
                threading.Thread(target=pool_server.serve_forever, daemon=True).start()
                client = OpenAI(api_key="test", base_url=f"http://127.0.0.1:{pool_server.server_port}/v1")
                try:
                    chunks = list(client.chat.completions.create(
                        model="api-pool",
                        messages=[{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "描述这张图"},
                                {"type": "image_url", "image_url": {"url": PNG_DATA_URL}},
                            ],
                        }],
                        stream=True,
                    ))
                finally:
                    client.close()
                    pool_server.shutdown()
                    pool_server.server_close()
        finally:
            upstream.shutdown()
            upstream.server_close()

        text = "".join(
            (chunk.choices[0].delta.content or "")
            for chunk in chunks if chunk.choices and chunk.choices[0].delta
        )
        self.assertNotIn("正在调用视觉模型", text)
        self.assertIn("图片解析完成", text, "下游未收到转译完成帧")
        self.assertIn("TARGET-ANSWER", text, "目标端点回答未到达下游（流被截断）")


if __name__ == "__main__":
    unittest.main()
