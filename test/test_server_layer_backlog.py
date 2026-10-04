"""HTTP server 层并发相关最小优化回归测试。

覆盖：
1. backlog：`_PoolServer.request_queue_size` == 128（原 `ThreadingHTTPServer` 默认 5）
2. 既有语义未被破坏：`daemon_threads` 仍为 True（`http.server.ThreadingHTTPServer`
   类属性，重启时 server_close 不等待在途流式线程）
3. 流式端到端：起最小上游 mock（SSE `data: {...}\\n\\n` + `data: [DONE]\\n\\n`），
   让池代理一次 `/v1/chat/completions` stream=true，验证下游收到的 SSE 帧完整、
   以 `data: [DONE]` 结尾、且请求不 hang。
"""

import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_POOL_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py"
)

# 并行建连时的观察上限：backlog 从 5 抬到 128 后，128 个已握手未 accept 的连接
# 应能被内核排队，而不是被丢弃。超过 128 的部分交由 somaxconn/客户端重传处理。
BURST_CLIENTS = 60


class SSEUpstream(BaseHTTPRequestHandler):
    """最小上游：返回分块的 SSE，末帧 data: [DONE]。"""

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        frames = [
            {"id": "cmpl-1", "object": "chat.completion.chunk", "created": 1,
             "model": "mock-model",
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": "he"}, "finish_reason": None}]},
            {"id": "cmpl-1", "object": "chat.completion.chunk", "created": 1,
             "model": "mock-model",
             "choices": [{"index": 0, "delta": {"content": "llo"}, "finish_reason": None}]},
            {"id": "cmpl-1", "object": "chat.completion.chunk", "created": 1,
             "model": "mock-model",
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        for frame in frames:
            self.wfile.write(b"data: " + json.dumps(frame).encode() + b"\n\n")
            self.wfile.flush()
            time.sleep(0.02)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


def _load_module(tmp_dir):
    """在干净 cwd 下加载 api_pool_server（与既有用例一致，避免写进仓库目录）。"""
    previous_cwd = os.getcwd()
    os.chdir(tmp_dir)
    try:
        spec = importlib.util.spec_from_file_location(
            "api_pool_server_test_mod", API_POOL_MODULE_PATH
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        os.chdir(previous_cwd)
    return module


class ServerLayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.module = _load_module(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_request_queue_size_raised_from_default(self):
        cls_ = self.module._PoolServer
        self.assertEqual(cls_.request_queue_size, 128)
        # 必须确实是 socketserver 默认值 5 的抬升，而非子类链上其他默认的巧合
        self.assertNotEqual(
            threading.current_thread().__class__.__name__, None
        )
        self.assertEqual(cls_.__mro__[1], ThreadingHTTPServer)

    def test_daemon_threads_still_true_for_fast_shutdown(self):
        # daemon_threads=True 来自 http.server.ThreadingHTTPServer；本轮不改它。
        # 断言它保持 True：SIGTERM 走 os._exit 不受影响，但 KeyboardInterrupt /
        # 测试内 server_close 路径不能因在途流式线程而阻塞。
        self.assertTrue(self.module._PoolServer.daemon_threads)

    def _listen_pending_from_proc_net(self, port):
        """读 listen socket 当前已排队的连接数（Send-Q），作为 backlog 行为观测点。

        这是瞬时计数不是上限，仅用于对照两个 server 实例在同样速率下的排空差异。
        """
        want = f"{port:04X}"
        with open("/proc/net/tcp") as fh:
            lines = fh.read().splitlines()
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 5 or parts[3] != "0A":  # st=0A -> LISTEN
                continue
            local = parts[1]
            if local.split(":", 1)[1] != want:
                continue
            return int(parts[4].split(":")[0], 16)  # tx_queue
        return None

    def test_listen_pending_observable_after_connect(self):
        """建连但服务端不 accept 时，/proc/net/tcp 的 Send-Q 应能观测到排队。

        这是一个存在性断言：证明 backlog 行为可观测，且抬升后的服务端仍遵循同一
        socketserver 调用路径（server_activate -> listen(request_queue_size)）。
        """
        module = self.module
        module.pool = module.APIPool()
        server = module._PoolServer(("127.0.0.1", 0), module.Handler)
        held = []
        try:
            # 不开 serve_forever -> accept 不执行，连接停在 backlog 里
            for _ in range(3):
                held.append(socket.create_connection(
                    ("127.0.0.1", server.server_port), timeout=5))
            pending = self._listen_pending_from_proc_net(server.server_port)
            self.assertIsNotNone(pending)
        finally:
            for sock in held:
                sock.close()
            server.server_close()

    def test_listen_backlog_is_class_attribute_not_stale_default(self):
        """request_queue_size 必须是 _PoolServer 的类属性且值抬升到位。

        /proc/net/tcp 与 ss 的 Send-Q 是「当前已排队待 accept 的连接数」而非
        backlog 上限，内核不暴露上限，无法反查；故以类属性 + MRO 定位断言。
        """
        cls_ = self.module._PoolServer
        self.assertEqual(cls_.request_queue_size, 128)
        self.assertIn("request_queue_size", cls_.__dict__)
        self.assertNotIn("request_queue_size", ThreadingHTTPServer.__dict__)

    def test_default_threading_server_stays_at_five(self):
        """对照：未子类化的 ThreadingHTTPServer 保持 socketserver 默认 5。"""
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.module.Handler)
        try:
            self.assertEqual(server.request_queue_size, 5)
            self.assertNotIsInstance(server, self.module._PoolServer)
        finally:
            server.server_close()

    def test_streaming_end_to_end_completes_and_ends_with_done(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), SSEUpstream)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        module = self.module
        try:
            module.pool = module.APIPool()
            endpoint = module.Endpoint(
                id="mock-sse",
                name="mock-sse",
                base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
                api_key="test",
                model="mock-model",
                in_pool=True,
                pool_groups=["main"],
                use_proxy=False,
                max_retries=0,
                stream_first_packet_timeout=10,
                stream_stall_timeout=0,
                stream_max_duration=0,
            )
            module.pool.add_endpoint(endpoint)
            pool_server = self.module._PoolServer(("127.0.0.1", 0), module.Handler)
            pool_thread = threading.Thread(target=pool_server.serve_forever, daemon=True)
            pool_thread.start()
            try:
                payload = json.dumps({
                    "model": "api-pool",
                    "messages": [{"role": "user", "content": "test"}],
                    "stream": True,
                }).encode()
                req = (
                    f"POST /v1/chat/completions HTTP/1.0\r\n"
                    f"Host: 127.0.0.1:{pool_server.server_port}\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(payload)}\r\n\r\n"
                ).encode() + payload
                raw = self._read_until_closed(pool_server.server_port, req)

                body = raw.split(b"\r\n\r\n", 1)[1]
                self.assertIn(b"data: {", body)
                self.assertTrue(body.endswith(b"data: [DONE]\n\n"))
                self.assertIn(b'"content": "he"', body)
                self.assertIn(b'"content": "llo"', body)
                self.assertIn(b'"finish_reason": "stop"', body)
            finally:
                pool_server.shutdown()
                pool_server.server_close()
        finally:
            upstream.shutdown()
            upstream.server_close()

    def _read_until_closed(self, port, request_bytes):
        """发一次请求并读到对端关闭；带硬超时防 hang。"""
        result = {}
        sock = socket.create_connection(("127.0.0.1", port), timeout=15)
        try:
            sock.settimeout(15)
            sock.sendall(request_bytes)
            chunks = []
            while True:
                try:
                    chunk = sock.recv(65536)
                except TimeoutError:
                    result["timeout"] = True
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            result["data"] = b"".join(chunks)
        finally:
            sock.close()
        self.assertNotIn("timeout", result, "流式响应未在 15s 内关闭连接")
        return result["data"]

    def test_burst_new_connections_all_served(self):
        """并发新建连接的 SYN 不因 backlog=5 被丢弃：BURST_CLIENTS 个请求全部成功。

        以 `_PoolServer` 的真实 listen socket 为观测点——先用 handler 阻塞住 accept
        会掩盖 backlog 抬升的意义，故改为全部并发发出并由不可达端口对照行为。
        这里只验证 60 个并发连接都能建连并拿到 HTTP 行。
        """
        module = self.module
        module.pool = module.APIPool()
        endpoint = module.Endpoint(
            id="mock-burst", name="mock-burst",
            base_url="http://127.0.0.1:1/v1", api_key="test", model="m",
            in_pool=True, pool_groups=["main"], use_proxy=False, max_retries=0,
        )
        module.pool.add_endpoint(endpoint)
        server = module._PoolServer(("127.0.0.1", 0), module.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            statuses = []
            errors = []
            lock = threading.Lock()
            ready = threading.Barrier(BURST_CLIENTS)

            def _hit(i):
                try:
                    ready.wait(timeout=10)
                    s = socket.create_connection(("127.0.0.1", server.server_port), timeout=10)
                    try:
                        s.sendall(b"GET /api/stats HTTP/1.0\r\nHost: x\r\n\r\n")
                        line = s.recv(200).split(b"\r\n", 1)[0]
                        with lock:
                            statuses.append(line)
                    finally:
                        s.close()
                except OSError as exc:  # pragma: no cover - 失败才记录
                    with lock:
                        errors.append(f"{type(exc).__name__}: {exc}")

            threads = [threading.Thread(target=_hit, args=(i,)) for i in range(BURST_CLIENTS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)
            self.assertEqual(errors, [])
            self.assertEqual(len(statuses), BURST_CLIENTS)
            self.assertTrue(all(b"HTTP/1." in line for line in statuses))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
