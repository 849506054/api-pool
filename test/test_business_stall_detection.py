import importlib.util
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location(f"api_pool_bizstall_{id(tmp_path)}", MODULE_PATH)
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


class KeepaliveStallResponse:
    """模拟「心跳续命」的坏死流：先来一条业务增量，然后无限 SSE 注释心跳。

    socket 层始终有数据到达（readline 立即返回），旧逻辑的 socket 停滞
    超时永远不会触发；业务增量停滞检测应在 stall_timeout 后中止。
    每组心跳间 sleep 让墙钟真实跨过 deadline（纯内存快流不会触发墙钟检查）。
    """

    def __init__(self, first_content=True, group_sleep=0.35, max_groups=30, raise_on_exhaust=False):
        self.first_line = (
            b'data: {"choices":[{"delta":{"content":"partial"}}]}\n' if first_content
            else b'data: {"choices":[{"delta":{}}]}\n'
        )
        self.socket = FakeSocket()
        self._group_sleep = group_sleep
        self._max_groups = max_groups
        self._raise_on_exhaust = raise_on_exhaust

    def readline(self):
        return self.first_line

    def __iter__(self):
        for _ in range(self._max_groups):
            yield b": keep-alive\n"
            yield b"\n"
            yield b'data: {"choices":[{"delta":{}}]}\n'
            time.sleep(self._group_sleep)
        if self._raise_on_exhaust:
            raise AssertionError("心跳流应在业务停滞超时后被中止，不应迭代到尽头")

    def close(self):
        pass


class HealthyStreamResponse:
    """正常流：增量持续到达，业务时钟不断刷新。

    生产线径 stream_first_packet_timeout>0 会预读 readline()，因此首行走
    readline、其余走 __iter__（与 KeepaliveStallResponse 同构）。
    """

    def __init__(self):
        self.first_line = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n'
        self.socket = FakeSocket()
        self._iter = iter([
            b'data: {"choices":[{"delta":{"content":"chunk0"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"chunk1"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"chunk2"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"chunk3"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"chunk4"}}]}\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n',
            b"data: [DONE]\n",
        ])

    def readline(self):
        return self.first_line

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._iter)

    def close(self):
        pass


class BusinessStallDetectionTests(unittest.TestCase):
    """B1：有效业务增量停滞检测（2026-08-31）。"""

    def make_endpoint(self, module, stall=1, max_duration=0):
        return module.Endpoint(
            id="biz", name="biz", base_url="http://example/v1", api_key="x",
            model="m", timeout=60, max_retries=0,
            stream_first_packet_timeout=120, stream_stall_timeout=stall,
            stream_max_duration=max_duration, in_pool=True, use_proxy=True,
        )

    def test_keepalive_stream_aborted_by_business_stall(self):
        """有输出的心跳续命流：透传已见内容、提前中止（finish=stop）、不重放。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            response = KeepaliveStallResponse(first_content=True)  # 首行业务增量
            payload = {"model": "m", "messages": [], "stream": True}
            with mock.patch.object(module.urllib.request, "urlopen", return_value=response), \
                    mock.patch.object(module, "_get_resp_socket", return_value=response.socket):
                result, error = module.APIPool()._try_endpoint(
                    self.make_endpoint(module, stall=1), payload, 60, log_usage=False)
                self.assertEqual(error, "")
                out = list(result)
            text = b"".join(out)
            # 已见内容已透传；stall=1s + 组间隔 0.35s → 约 3 组后中止（30 组上限远未到）
            self.assertIn(b"partial", text)
            self.assertIn(b"[DONE]", text)
            self.assertLessEqual(out.count(b'"delta":{}') , 5, "应在数个心跳组内中止，不应跑完 30 组")
            # 有输出 → 不重放、finish=stop（与 socket 停滞语义一致），无可见错误文本
            tail = [c for c in out if b"finish_reason" in c]
            self.assertTrue(tail and b'"stop"' in tail[-1])

    def test_no_output_keepalive_retries_once_then_visible_error(self):
        """无输出的心跳流：原端点内部重试一次，仍停滞 → 可见 error 原因。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            responses = [KeepaliveStallResponse(first_content=False, max_groups=6),
                         KeepaliveStallResponse(first_content=False, max_groups=6)]
            payload = {"model": "m", "messages": [], "stream": True}
            calls = []
            def fake_open(req, timeout=None):
                calls.append(1)
                return responses[min(len(calls) - 1, len(responses) - 1)]
            with mock.patch.object(module.urllib.request, "urlopen", side_effect=fake_open), \
                    mock.patch.object(module, "_get_resp_socket",
                                      side_effect=lambda r: r.socket):
                result, error = module.APIPool()._try_endpoint(
                    self.make_endpoint(module, stall=1), payload, 60, log_usage=False)
                self.assertEqual(error, "")
                out = list(result)
            text = b"".join(out).decode("utf-8", "replace")
            # 两次尝试（首次 + 内部重试）都被业务停滞中止
            self.assertEqual(len(calls), 2)
            # 下游可见：error finish + 业务增量停滞原因
            self.assertIn("业务增量停滞", text)
            self.assertIn('"error"', text)
            self.assertIn("[DONE]", text)

    def test_healthy_stream_not_affected(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            response = HealthyStreamResponse()
            payload = {"model": "m", "messages": [], "stream": True}
            with mock.patch.object(module.urllib.request, "urlopen", return_value=response), \
                    mock.patch.object(module, "_get_resp_socket", return_value=response.socket):
                result, error = module.APIPool()._try_endpoint(
                    self.make_endpoint(module, stall=60), payload, 60, log_usage=False)
                self.assertEqual(error, "")
                out = b"".join(result)
            self.assertIn(b"hi", out)
            self.assertIn(b"chunk4", out)
            self.assertIn(b"finish_reason", out)
            self.assertIn(b"[DONE]", out)

    def test_zero_stall_disables_business_deadline(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            # stall=0 → business_stall_deadline=None；心跳流完整走完有界迭代器
            response = KeepaliveStallResponse(group_sleep=0, max_groups=5, raise_on_exhaust=False)
            payload = {"model": "m", "messages": [], "stream": True}
            with mock.patch.object(module.urllib.request, "urlopen", return_value=response), \
                    mock.patch.object(module, "_get_resp_socket", return_value=response.socket):
                result, error = module.APIPool()._try_endpoint(
                    self.make_endpoint(module, stall=0), payload, 60, log_usage=False)
                self.assertEqual(error, "")
                chunks = list(result)
            self.assertFalse(any("业务增量停滞" in c.decode("utf-8", "replace") for c in chunks))
            # 5 组心跳 × 3 行 = 15 行全部透传（迭代器走完，无中止）
            self.assertGreaterEqual(len(chunks), 15)

    def test_business_stall_lifecycle_log_includes_request_id(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            response = KeepaliveStallResponse(group_sleep=0.35, max_groups=30)
            payload = {"model": "m", "messages": [], "stream": True}
            logs = []
            with mock.patch.object(module.urllib.request, "urlopen", return_value=response), \
                    mock.patch.object(module, "_get_resp_socket", return_value=response.socket), \
                    mock.patch.object(module, "sys_log", side_effect=lambda msg, level="INFO": logs.append((msg, level))):
                result, error = module.APIPool()._try_endpoint(
                    self.make_endpoint(module, stall=1), payload, 60, log_usage=False,
                    request_id="b2req01",
                )
                self.assertEqual(error, "")
                list(result)
            lifecycle_errors = [msg for msg, level in logs if level == "ERROR" and "流式事务失败" in msg]
            self.assertEqual(len(lifecycle_errors), 1)
            self.assertTrue(lifecycle_errors[0].startswith("[req=b2req01] "))
            self.assertIn("流式无有效业务增量停滞", lifecycle_errors[0])


if __name__ == "__main__":
    unittest.main()
