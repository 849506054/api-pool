"""严格校验 400（reasoning 回传 / tool 配对）的「换形态重试」防御（2026-09-12）。

契约（2026-09-12 定案）：
- 请求级预算 **1**，且这一次重试**就是换形态**：跳过 `tool_call_id_prefix` 重写、
  改回客户端原始 tool_call id（替换原先「原样复读」的重试；原样复读只是重复同一份被校验的历史）。
- 仅当本尝试**确实应用过前缀重写**（`attempt_prefix_applied > 0`）时才发这一次；
  否则改回原始 id 无变化 → 不重试，直接轮转。
- `_rewrite_tool_call_ids` **非变异**：轮转各次尝试共用同一份 Hermes 历史，原地改写会把
  某端点需要的重写结果带给后续异构端点（违反按端点隔离铁律），并使「改回原始 id」不可能。

集成用例用真实本地 HTTP 上游（ThreadingHTTPServer）跑 `pool.chat()` 全链路，
上游按「历史里是否存在指定前缀的 tool_call id」返回 400/200，不 mock 池内方法。
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")

PREFIX = "call_00_ET_"
STRICT_MESSAGE = "The `reasoning_text` in the thinking mode must be passed back to the API."


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_strict_retry_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class UpstreamHandler(BaseHTTPRequestHandler):
    """按配置返回严格校验 400 或 200；记录每次请求的 tool_call id 序列。"""

    records = []            # 每次请求的 assistant tool_call id 列表（探活等无 tool_calls 记为 []）
    reject_prefix = ""      # 历史里出现该前缀的 id → 400（严格校验）
    always_strict = False   # 无视 id 一律 400（模拟上游持续判坏）
    reject_status = 400     # 非严格错误场景（如 500）用

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        ids = []
        for message in body.get("messages") or []:
            if isinstance(message, dict) and message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    if isinstance(call, dict) and call.get("id"):
                        ids.append(call["id"])
        type(self).records.append(ids)

        mismatch_prefix = bool(type(self).reject_prefix) and any(
            call_id.startswith(type(self).reject_prefix) for call_id in ids
        )
        if type(self).always_strict or mismatch_prefix:
            body_bytes = json.dumps({"error": {"message": STRICT_MESSAGE, "type": "invalid_request_error"}}).encode()
            status = type(self).reject_status
        else:
            status = 200
            body_bytes = json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}}
            ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def log_message(self, fmt, *args):  # 静音测试输出
        pass


class StrictValidationPrefixRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        UpstreamHandler.records = []
        UpstreamHandler.reject_prefix = ""
        UpstreamHandler.always_strict = False
        UpstreamHandler.reject_status = 400
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.module = load_module(self.tmp.name)
        self.messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "thinking", "reasoning_content": "r",
             "tool_calls": [{"id": "call_client_1", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_client_1", "name": "f", "content": "result"},
            {"role": "user", "content": "continue"},
        ]

    def endpoint(self, endpoint_id, prefix="", priority=1):
        return self.module.Endpoint(
            id=endpoint_id, name=endpoint_id, base_url=self.base_url, api_key="test",
            model="deepseek-v4-flash", priority=priority, in_pool=True, use_proxy=False,
            cooldown_minutes=5, pool_groups=["main"], max_retries=0, timeout=10,
            tool_call_id_prefix=prefix,
        )

    # ── 单元：非变异重写 ──────────────────────────────────────────────
    def test_rewrite_is_pure_and_reports_changed_count(self):
        pool = self.module.APIPool([])
        snapshot = json.dumps(self.messages, ensure_ascii=False, sort_keys=True)
        out, changed = pool._rewrite_tool_call_ids(self.messages, PREFIX)
        self.assertEqual(changed, 2, "assistant tool_call id + tool 消息 tool_call_id 各 1 条")
        self.assertEqual(json.dumps(self.messages, ensure_ascii=False, sort_keys=True), snapshot,
                         "原始历史必须零改动（轮转各次尝试共用）")
        self.assertTrue(out[1]["tool_calls"][0]["id"].startswith(PREFIX))
        self.assertEqual(out[2]["tool_call_id"], out[1]["tool_calls"][0]["id"], "assistant/tool 配对保持")
        again, changed_again = pool._rewrite_tool_call_ids(out, PREFIX)
        self.assertEqual(changed_again, 0, "已是前缀格式 → 幂等，changed=0")
        self.assertEqual(again, out)

    # ── 集成：前缀命中 400 → 唯一一次重试即「原始 id」变体 → 成功 ────────
    def test_strict_400_retries_with_original_ids(self):
        UpstreamHandler.reject_prefix = PREFIX
        ep = self.endpoint("ds4f", prefix=PREFIX)
        pool = self.module.APIPool([ep])
        result = pool.chat(list(self.messages))
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")

        attempts = [ids for ids in UpstreamHandler.records if ids]
        self.assertEqual(len(attempts), 2, f"期望 2 次尝试（重写 → 原始 id），实际 {attempts}")
        self.assertTrue(all(i.startswith(PREFIX) for i in attempts[0]), "首次用重写后的 id")
        self.assertEqual(attempts[1], ["call_client_1"],
                         "唯一一次重试必须改回客户端原始 tool_call id（不是原样复读）")
        self.assertEqual(self.messages[1]["tool_calls"][0]["id"], "call_client_1",
                         "调用方传入的历史不得被改写")
        self.assertEqual(ep._cooldown_until, 0, "客户端类错误不冻结端点")

    # ── 集成：无前缀时改回原始 id 无变化 → 不重试，直接轮转 ─────────────
    def test_no_prefix_skips_variant_retry(self):
        UpstreamHandler.always_strict = True
        ep = self.endpoint("plain", prefix="")
        pool = self.module.APIPool([ep])
        with self.assertRaises(self.module.AllEndpointsFailed):
            pool.chat(list(self.messages))
        attempts = [ids for ids in UpstreamHandler.records if ids]
        self.assertEqual(len(attempts), 1, f"无前缀时改回原始 id 无变化，不应重试，实际 {attempts}")
        self.assertEqual(attempts[0], ["call_client_1"])

    # ── 集成：前缀重写不污染轮转后的异构端点 ──────────────────────────
    def test_prefix_rewrite_does_not_leak_to_next_endpoint(self):
        UpstreamHandler.reject_prefix = PREFIX
        first = self.endpoint("with-prefix", prefix=PREFIX, priority=1)
        second = self.endpoint("no-prefix", prefix="", priority=2)
        pool = self.module.APIPool([first, second])
        result = pool.chat(list(self.messages))
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")

        attempts = [ids for ids in UpstreamHandler.records if ids]
        self.assertTrue(any(i.startswith(PREFIX) for i in attempts[0]),
                        "第 1 个端点按自己的配置重写")
        last = attempts[-1]
        self.assertEqual(last, ["call_client_1"],
                         "轮转后的端点必须收到原始 id（重写不得跨端点泄漏）")
        self.assertEqual(self.messages[1]["tool_calls"][0]["id"], "call_client_1")


if __name__ == "__main__":
    unittest.main()
