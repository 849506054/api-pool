"""上游 SSE 夹带裸 `data: null` 帧 → 池必须丢弃，不得透传给下游。

背景（2026-09-19 生产事故）：ps.air-outer（openai 兼容）的流里会夹裸帧
`data: null`。openai SDK 的 `_process_response_data` 对 `data: null` 返回 `None`
并原样 yield，Hermes 主循环 `chat_completion_helpers._call_chat_completions`
执行 `if not chunk.choices:` → `AttributeError: 'NoneType' object has no
attribute 'choices'`，3 次重试全崩 → cron job 失败（knowledge-sync 连续 3 次）。

池侧 openai 协议分支原为 `else: yield line` 原样转发每一行，而 gemini/anthropic
分支都有空帧过滤 —— 唯独 openai 没有。修复 = `continue` 丢弃该帧（+5 行）。

本测试锁死该行为，覆盖三种帧形态：
  1. 裸 `data: null`（本次事故形态）
  2. `data:null` 无空格变体
  3. 带前后空白的 `data:  null ` 变体
并做反向断言：正常帧 / 含 null 值的业务帧（如 `"content": null`）必须原样保留，
证明过滤只针对裸 null 帧、不误伤合法 JSON。
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

from openai import OpenAI

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_server_null_frame_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module._client_baseline.update({"User-Agent": "pytest-client/1.0"})
        return module
    finally:
        os.chdir(previous_cwd)


class NullFrameUpstream(BaseHTTPRequestHandler):
    """上游：在正常 SSE 帧之间插入裸 null 帧变体。"""

    mode: ClassVar[str] = "with-null"

    def log_message(self, format, *args):
        del format, args

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)

        def frame(delta=None, finish=None, usage=None, choices=None):
            body = {
                "id": "chatcmpl-upstream",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "mock-model",
            }
            if choices is not None:
                body["choices"] = choices
            else:
                body["choices"] = [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
            if usage is not None:
                body["usage"] = usage
            return b"data: " + json.dumps(body).encode() + b"\n\n"

        # 业务帧序列（修复前后都必须原样到达下游）
        events = [
            frame({"role": "assistant", "content": ""}),
            frame({"content": "hello "}),
            frame({"content": "world"}),
            # `content: null` 是合法业务帧（非裸 null 帧），必须保留
            frame({"content": None}),
            frame({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                   "function": {"name": "noop", "arguments": "{}"}}]}),
            frame({}, finish="tool_calls"),
            frame(choices=[], usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}),
        ]

        if type(self).mode == "with-null":
            raw = b"".join([
                events[0],
                events[1],
                b"data: null\n\n",          # 事故形态
                events[2],
                b"data:null\n\n",           # 无空格变体
                events[3],
                b"data:  null \n\n",        # 前后空白变体
                events[4],
                events[5],
                events[6],
                b"data: [DONE]\n\n",
            ])
        else:
            raw = b"".join(events) + b"data: [DONE]\n\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class NullFrameFilterTests(unittest.TestCase):
    def _serve(self, mode):
        NullFrameUpstream.mode = mode
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), NullFrameUpstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        return upstream

    def _consume(self, tmp_path, mode):
        """经池消费上游流；返回 (chunks, text, finishes, none_count)。

        none_count = 下游收到的 None chunk 数 —— 即 Hermes 崩溃的直接原因，
        必须恒为 0。
        """
        upstream = self._serve(mode)
        try:
            module = load_module(tmp_path)
            module.pool = module.APIPool()
            module.pool.add_endpoint(module.Endpoint(
                id="mock", name="mock",
                base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
                api_key="test", model="mock-model", in_pool=True, pool_groups=["main"],
                use_proxy=False, max_retries=0, stream_first_packet_timeout=5,
                stream_stall_timeout=0, stream_max_duration=0,
            ))
            pool_server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
            threading.Thread(target=pool_server.serve_forever, daemon=True).start()
            client = OpenAI(api_key="test", base_url=f"http://127.0.0.1:{pool_server.server_port}/v1")
            try:
                chunks = list(client.chat.completions.create(
                    model="api-pool",
                    messages=[{"role": "user", "content": "test"}],
                    stream=True,
                ))
            finally:
                client.close()
                pool_server.shutdown()
                pool_server.server_close()
        finally:
            upstream.shutdown()
            upstream.server_close()

        none_count = sum(1 for chunk in chunks if chunk is None)
        text = "".join(
            (chunk.choices[0].delta.content or "")
            for chunk in chunks if chunk is not None and chunk.choices
        )
        finishes = [
            chunk.choices[0].finish_reason
            for chunk in chunks if chunk is not None and chunk.choices and chunk.choices[0].finish_reason
        ]
        return chunks, text, finishes, none_count

    def test_null_frames_never_reach_the_downstream_sdk(self):
        """裸 `data: null` 帧必须被池丢弃 —— 下游收到 0 个 None chunk。

        修复前该断言失败（下游会收到 3 个 None chunk → Hermes AttributeError）。
        """
        with tempfile.TemporaryDirectory() as tmp_path:
            chunks, text, finishes, none_count = self._consume(tmp_path, "with-null")
            self.assertEqual(none_count, 0, "裸 data: null 帧被透传，下游会 AttributeError")
            # 业务内容完整到达，证明丢弃 null 帧不影响正常分片
            self.assertIn("hello ", text)
            self.assertIn("world", text)
            self.assertIn("tool_calls", finishes)

    def test_business_frames_survive_the_filter(self):
        """反向断言：过滤只针对裸 null 帧，合法业务帧（含 `content: null`）原样保留。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            with_null, text_a, finishes_a, _ = self._consume(tmp_path, "with-null")
        with tempfile.TemporaryDirectory() as tmp_path:
            clean, text_b, finishes_b, _ = self._consume(tmp_path, "clean")
        # 两种上游流（含/不含 null 帧）下游可见业务分片数必须一致
        self.assertEqual(len(with_null), len(clean))
        self.assertEqual(text_a, text_b)
        self.assertEqual(finishes_a, finishes_b)


if __name__ == "__main__":
    unittest.main()
