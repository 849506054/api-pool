import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from openai import OpenAI

API_POOL_MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")
HERMES_ROOT = "/opt/hermes"


class SlowStreamingUpstream(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        chunks = [
            {"id": "upstream", "object": "chat.completion.chunk", "created": 1,
             "model": "mock-model", "choices": [{"index": 0, "delta": {"content": "partial"}}]},
            {"id": "upstream", "object": "chat.completion.chunk", "created": 1,
             "model": "mock-model", "choices": [{"index": 0, "delta": {"content": "duplicate"}}]},
        ]
        self.wfile.write(b"data: " + json.dumps(chunks[0]).encode() + b"\n\n")
        self.wfile.flush()
        time.sleep(1.2)
        self.wfile.write(b": keep-alive\n\n")
        self.wfile.write(b"data: " + json.dumps(chunks[1]).encode() + b"\n\n")
        self.wfile.flush()


class KeepaliveStallUpstream(BaseHTTPRequestHandler):
    """先发一条业务增量，随后只发 SSE 心跳注释——socket 层始终有数据，业务时钟停滞。"""

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        first = {"id": "upstream", "object": "chat.completion.chunk", "created": 1,
                 "model": "mock-model", "choices": [{"index": 0, "delta": {"content": "partial"}}]}
        later = {"id": "upstream", "object": "chat.completion.chunk", "created": 1,
                 "model": "mock-model", "choices": [{"index": 0, "delta": {"content": "duplicate"}}]}
        try:
            self.wfile.write(b"data: " + json.dumps(first).encode() + b"\n\n")
            self.wfile.flush()
            for _ in range(12):
                time.sleep(0.4)
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: " + json.dumps(later).encode() + b"\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class HermesAgentStub:
    api_mode = "chat_completions"
    provider = "custom"
    model = "api-pool"
    platform = "telegram"
    _interrupt_requested = False
    _disable_streaming = False
    reasoning_callback = None

    def __init__(self, base_url):
        self.base_url = base_url
        self.stream_delta_callback = None
        self.deltas = []
        self._current_streamed_assistant_text = ""

    def _create_request_openai_client(self, **_kwargs):
        return OpenAI(api_key="test", base_url=self.base_url)

    def _close_request_openai_client(self, client, **_kwargs):
        client.close()

    def _abort_request_openai_client(self, client, **_kwargs):
        client.close()

    def _touch_activity(self, *_args, **_kwargs):
        pass

    def _capture_rate_limits(self, *_args, **_kwargs):
        pass

    def _capture_credits(self, *_args, **_kwargs):
        pass

    def _check_openrouter_cache_status(self, *_args, **_kwargs):
        pass

    def _stream_diag_init(self):
        return {}

    def _stream_diag_capture_response(self, *_args, **_kwargs):
        pass

    def _has_stream_consumers(self):
        return True

    def _fire_stream_delta(self, text):
        self.deltas.append(text)
        self._current_streamed_assistant_text += text

    def _fire_reasoning_delta(self, *_args, **_kwargs):
        pass

    def _fire_tool_gen_started(self, *_args, **_kwargs):
        pass

    def _record_streamed_assistant_text(self, text):
        self._current_streamed_assistant_text += text

    def _emit_stream_start(self):
        pass

    def _emit_stream_end(self, **_kwargs):
        pass

    def _emit_stream_drop(self, **_kwargs):
        pass

    def _emit_wait_notice(self, **_kwargs):
        pass

    def _reset_stream_delivery_tracking(self):
        self._current_streamed_assistant_text = ""

    def _buffer_status(self, *_args, **_kwargs):
        pass

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _is_provider_stream_parse_error(self, *_args, **_kwargs):
        return False

    def _log_stream_retry(self, *_args, **_kwargs):
        pass


class HermesStreamErrorE2ETests(unittest.TestCase):
    def test_total_duration_error_reaches_hermes_without_replay_or_freeze(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), SlowStreamingUpstream)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()

            previous_cwd = os.getcwd()
            os.chdir(tmp_path)
            try:
                spec = importlib.util.spec_from_file_location("api_pool_hermes_e2e", API_POOL_MODULE_PATH)
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
            finally:
                os.chdir(previous_cwd)

            module.pool = module.APIPool()
            endpoint = module.Endpoint(
                id="mock", name="mock", base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
                api_key="test", model="mock-model", in_pool=True, pool_groups=["main"],
                use_proxy=False, max_retries=0, stream_first_packet_timeout=5,
                stream_stall_timeout=0, stream_max_duration=1,
            )
            module.pool.add_endpoint(endpoint)
            pool_server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
            pool_thread = threading.Thread(target=pool_server.serve_forever, daemon=True)
            pool_thread.start()

            sys.path.insert(0, HERMES_ROOT)
            try:
                from agent.chat_completion_helpers import (
                    interruptible_streaming_api_call,
                )

                direct_client = OpenAI(
                    api_key="test", base_url=f"http://127.0.0.1:{pool_server.server_port}/v1"
                )
                try:
                    wire_chunks = list(direct_client.chat.completions.create(
                        model="api-pool",
                        messages=[{"role": "user", "content": "test"}],
                        stream=True,
                    ))
                finally:
                    direct_client.close()
                wire_text = "".join((chunk.choices[0].delta.content or "") for chunk in wire_chunks if chunk.choices)
                wire_finishes = [chunk.choices[0].finish_reason for chunk in wire_chunks if chunk.choices]
                self.assertIn("error", wire_finishes)
                self.assertIn("API Pool Error", wire_text)
                self.assertIn("流式总时长超限", wire_text)
                self.assertNotIn("duplicate", wire_text)

                agent = HermesAgentStub(f"http://127.0.0.1:{pool_server.server_port}/v1")
                response = interruptible_streaming_api_call(agent, {
                    "model": "api-pool",
                    "messages": [{"role": "user", "content": "test"}],
                })
                choice = response.choices[0]
                # Hermes treats an error finish after partial delivery as a
                # recoverable length-truncated stub so its continuation path can run.
                self.assertEqual(choice.finish_reason, "length")
                self.assertIn("partial", choice.message.content)
                self.assertIn("API Pool Error", choice.message.content)
                self.assertIn("流式总时长超限", choice.message.content)
                self.assertNotIn("duplicate", choice.message.content)
                self.assertEqual(choice.message.content.count("partial"), 1)
                self.assertEqual(endpoint._fail_count, 0)
                self.assertEqual(endpoint._cooldown_until, 0)
            finally:
                pool_server.shutdown()
                pool_server.server_close()
                upstream.shutdown()
                upstream.server_close()
                if HERMES_ROOT in sys.path:
                    sys.path.remove(HERMES_ROOT)

    def test_business_stall_after_partial_output_reaches_hermes_as_continuable(self):
        """停滞在已有输出后发生时，Hermes 必须看到可续写的截断，而不是完整回答。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), KeepaliveStallUpstream)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()

            previous_cwd = os.getcwd()
            os.chdir(tmp_path)
            try:
                spec = importlib.util.spec_from_file_location("api_pool_hermes_stall_e2e", API_POOL_MODULE_PATH)
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
            finally:
                os.chdir(previous_cwd)

            module.pool = module.APIPool()
            endpoint = module.Endpoint(
                id="mock", name="mock", base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
                api_key="test", model="mock-model", in_pool=True, pool_groups=["main"],
                use_proxy=False, max_retries=0, stream_first_packet_timeout=5,
                stream_stall_timeout=1, stream_max_duration=0,
            )
            module.pool.add_endpoint(endpoint)
            pool_server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
            pool_thread = threading.Thread(target=pool_server.serve_forever, daemon=True)
            pool_thread.start()

            sys.path.insert(0, HERMES_ROOT)
            try:
                from agent.chat_completion_helpers import (
                    interruptible_streaming_api_call,
                )

                direct_client = OpenAI(
                    api_key="test", base_url=f"http://127.0.0.1:{pool_server.server_port}/v1"
                )
                try:
                    wire_chunks = list(direct_client.chat.completions.create(
                        model="api-pool",
                        messages=[{"role": "user", "content": "test"}],
                        stream=True,
                    ))
                finally:
                    direct_client.close()
                wire_text = "".join((chunk.choices[0].delta.content or "") for chunk in wire_chunks if chunk.choices)
                wire_finishes = [chunk.choices[0].finish_reason for chunk in wire_chunks if chunk.choices]
                self.assertIn("error", wire_finishes)
                self.assertNotIn("stop", wire_finishes)
                self.assertIn("API Pool Error", wire_text)
                self.assertIn("业务增量停滞", wire_text)
                self.assertNotIn("duplicate", wire_text)

                agent = HermesAgentStub(f"http://127.0.0.1:{pool_server.server_port}/v1")
                response = interruptible_streaming_api_call(agent, {
                    "model": "api-pool",
                    "messages": [{"role": "user", "content": "test"}],
                })
                choice = response.choices[0]
                # error finish after partial delivery → length-truncated stub → continuation path
                self.assertEqual(choice.finish_reason, "length")
                self.assertIn("partial", choice.message.content)
                self.assertIn("业务增量停滞", choice.message.content)
                self.assertEqual(choice.message.content.count("partial"), 1)
                self.assertNotIn("duplicate", choice.message.content)
                # 流事务失败仍不冻结端点
                self.assertEqual(endpoint._fail_count, 0)
                self.assertEqual(endpoint._cooldown_until, 0)
            finally:
                pool_server.shutdown()
                pool_server.server_close()
                upstream.shutdown()
                upstream.server_close()
                if HERMES_ROOT in sys.path:
                    sys.path.remove(HERMES_ROOT)


if __name__ == "__main__":
    unittest.main()
