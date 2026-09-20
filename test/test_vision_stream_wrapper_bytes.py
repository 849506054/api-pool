"""视觉转译流式包装器必须吐 bytes（2026-09-20 生产事故护栏）。

背景：请求带图 + 命中 `is_vision=False` 端点 → 走 vision_wrapper 分支。该分支的
三处提示帧是 f-string（`str`），而 `Handler.do_POST` 直接 `self.wfile.write(chunk)`
→ `TypeError: a bytes-like object is required, not 'str'` → 响应头已发（200 +
text/event-stream）但正文 0 字节 → Hermes 侧 EmptyStreamError，3×3 重试全崩 →
「模型 provider failed after retries」。

覆盖两层：
1. 单测：从源码提取 `vision_wrapper`，用桩驱动两条分支（目标失败 / 目标成功），
   断言每个 yield 都是 bytes 且提示帧是合法 SSE + JSON。
2. 端到端：真起池 + mock 上游，发一条带 `image_url` 的流式请求（目标端点盲、
   视觉池有候选），断言下游能收到完整流（含两条提示帧与目标回答）而非断流。
"""

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


def load_vision_wrapper():
    """从 api_pool_server.py 源码中提取 vision_wrapper 并编译成可调用对象。"""
    source = open(MODULE_PATH, encoding="utf-8").read()
    tree = ast.parse(source)
    node = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "vision_wrapper"
    )
    segment = ast.get_source_segment(source, node)
    assert segment is not None
    code = textwrap.dedent(segment)
    namespace = {
        "group": "main",
        "request_id": "vision-wrapper-test",
        "request_deadline": None,
        "defer_at_request": 0,
        "request_route_epoch": 0,
    }
    exec(compile(code, "<vision_wrapper>", "exec"), namespace)
    return namespace["vision_wrapper"], namespace


class _StubPool:
    """只实现 vision_wrapper 用到的池方法。"""

    def __init__(self, err):
        self._err = err
        self._lock = threading.Lock()

    def _translate_images_sync(self, messages, candidates, group, **kwargs):
        return messages

    def _try_endpoint(self, ep, payload, timeout, **kwargs):
        if self._err:
            return None, self._err
        return iter([DONE_FRAME]), None

    def _on_success(self, ep, **kwargs):
        pass

    def _release_inflight(self, ep_id, group):
        pass


class VisionStreamWrapperBytesTest(unittest.TestCase):
    def _collect(self, err):
        wrapper, namespace = load_vision_wrapper()
        namespace["self"] = _StubPool(err)
        endpoint = types.SimpleNamespace(id="ep-1", name="stub-endpoint")
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        return list(wrapper(endpoint, payload, 30, ["vision-ep"]))

    def test_all_frames_are_bytes_on_both_branches(self):
        for err in ("HTTP 502: upstream boom", None):
            with self.subTest(err=err):
                frames = self._collect(err)
                failed = [(i, type(f).__name__) for i, f in enumerate(frames) if not isinstance(f, bytes)]
                self.assertEqual(failed, [], f"vision_wrapper 产出非 bytes 帧: {failed}")
                self.assertTrue(frames, "vision_wrapper 未产出任何帧")

    def test_notice_frames_are_valid_sse_json(self):
        for err in ("HTTP 502: upstream boom", None):
            with self.subTest(err=err):
                for frame in self._collect(err):
                    self.assertTrue(frame.startswith(b"data: "), frame[:40])
                    self.assertTrue(frame.endswith(b"\n\n"), frame[-10:])
                    json.loads(frame[len(b"data: "):].decode("utf-8"))

    def test_vision_wrapper_source_encodes_every_yield(self):
        """源码护栏：该函数内不得再有裸 str yield。"""
        source = open(MODULE_PATH, encoding="utf-8").read()
        tree = ast.parse(source)
        node = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "vision_wrapper"
        )
        for child in ast.walk(node):
            if isinstance(child, ast.Yield) and isinstance(child.value, ast.JoinedStr):
                call = ast.dump(child.value)
                self.assertIn("encode", call, f"vision_wrapper 第 {child.lineno} 行 yield str 未 encode")


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
        self.assertIn("检测到图片", text, "下游未收到转译提示帧（拦截分支未生效或帧丢失）")
        self.assertIn("图片解析完成", text, "下游未收到转译完成帧")
        self.assertIn("TARGET-ANSWER", text, "目标端点回答未到达下游（流被截断）")


if __name__ == "__main__":
    unittest.main()
