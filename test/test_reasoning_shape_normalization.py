"""出站历史 reasoning 形态归一化（2026-09-12）。

契约（外部实践 + 我们 09-10 实证：上游对"思考内容为空"的历史直接返回
`The reasoning_text in the thinking mode must be passed back to the API`）：
- assistant **带 tool_calls**：reasoning 缺失/空串/纯空白 → 补占位 `" "`
- assistant **不带 tool_calls**：reasoning 缺失/空 → 删掉该字段
- 有真实 reasoning → 原样保留
- 只对"需要回传 reasoning"的端点生效（DeepSeek/GLM/Kimi/MiMo 家族或 reasoning_policy=keep）；
  strip 端点继续整体剥离，不补占位
- 非变异：原始历史对象不得被改动（轮转各次尝试共用同一份历史）
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_reasoning_shape_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module._client_baseline.update({"User-Agent": "pytest-client/1.0"})
        return module
    finally:
        os.chdir(previous_cwd)


class UpstreamHandler(BaseHTTPRequestHandler):
    bodies = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            type(self).bodies.append(json.loads(raw.decode("utf-8")))
        except Exception:
            type(self).bodies.append({})
        payload = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):  # noqa: A002 - 静音测试上游日志
        pass


class ReasoningShapeTests(unittest.TestCase):
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
        UpstreamHandler.bodies = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.module = load_module(self.tmp.name)

    def deepseek_endpoint(self, endpoint_id="ds", model="deepseek-v4-flash"):
        return self.module.Endpoint(
            id=endpoint_id, name=endpoint_id, base_url=self.base_url, api_key="test",
            model=model, priority=1, in_pool=True, use_proxy=False, cooldown_minutes=5,
            pool_groups=["main"], max_retries=0, timeout=10,
        )

    # ── 单元：形态规则 ────────────────────────────────────────────────

    def normalize(self, messages, model="deepseek-v4-flash"):
        ep = self.deepseek_endpoint(model=model)
        return self.module.APIPool._messages_for_endpoint(messages, ep)

    def test_tool_call_with_missing_reasoning_gets_placeholder(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "thinking", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "r"},
        ]
        out = self.normalize(messages)
        self.assertEqual(out[1]["reasoning_content"], " ")
        self.assertNotIn("reasoning_content", messages[1], "原始历史不得被改写")

    def test_tool_call_with_empty_reasoning_gets_placeholder(self):
        for empty in ("", "   ", None):
            messages = [{"role": "assistant", "content": "t", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
                "reasoning_content": empty}]
            out = self.normalize(messages)
            self.assertEqual(out[0]["reasoning_content"], " ",
                             f"reasoning={empty!r} 应补占位")

    def test_tool_call_keeps_reasoning_text_field_name(self):
        messages = [{"role": "assistant", "content": "t", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
            "reasoning_text": ""}]
        out = self.normalize(messages)
        self.assertEqual(out[0]["reasoning_text"], " ")
        self.assertNotIn("reasoning_content", out[0], "字段名沿用消息里已有的那个")

    def test_plain_assistant_with_empty_reasoning_drops_field(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "answer", "reasoning_content": ""},
        ]
        out = self.normalize(messages)
        self.assertNotIn("reasoning_content", out[1])
        self.assertNotIn("reasoning_text", out[1])

    def test_plain_assistant_without_reasoning_unchanged(self):
        messages = [{"role": "assistant", "content": "answer"}]
        out = self.normalize(messages)
        self.assertIs(out, messages, "无改动时返回原对象（零拷贝）")

    def test_real_reasoning_preserved(self):
        messages = [{"role": "assistant", "content": "t", "reasoning_content": "真实推理",
                     "tool_calls": [{"id": "c1", "type": "function",
                                     "function": {"name": "f", "arguments": "{}"}}]}]
        out = self.normalize(messages)
        self.assertEqual(out[0]["reasoning_content"], "真实推理")

    def test_strip_endpoint_still_strips(self):
        messages = [{"role": "assistant", "content": "t", "reasoning_content": "",
                     "tool_calls": [{"id": "c1", "type": "function",
                                     "function": {"name": "f", "arguments": "{}"}}]}]
        out = self.normalize(messages, model="qwen3-max")
        self.assertNotIn("reasoning_content", out[0], "非回传家族端点继续剥离，不补占位")

    # ── 集成：真实 HTTP 上游收到的历史形态 ────────────────────────────

    def test_chat_payload_carries_placeholder_for_empty_tool_call_reasoning(self):
        ep = self.deepseek_endpoint()
        pool = self.module.APIPool([ep])
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "thinking", "reasoning_content": "",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "r"},
            {"role": "user", "content": "continue"},
        ]
        result = pool.chat(messages)
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        sent = UpstreamHandler.bodies[0]["messages"]
        assistant = [m for m in sent if m.get("role") == "assistant" and m.get("tool_calls")][0]
        self.assertEqual(assistant.get("reasoning_content"), " ",
                         "实际发出的历史里 tool-call 轮次必须带非空 reasoning")
        self.assertEqual(messages[1]["reasoning_content"], "", "调用方历史未被改写")


class SourceGuards(unittest.TestCase):
    def test_dead_reasoning_cache_removed(self):
        """护栏：写了没读的进程级 reasoning 缓存已删除（2026-09-12 用户定稿，只保留记录）。

        来历：2026-08-06 `a847e05` 为 Kcne 注入路径加的缓存；08-07 `4626f16` 删除注入逻辑后
        写侧遗留；09-12 清理。不要重新引入（需要跨端点 reasoning 补全时，请连同真实读取点一起设计）。
        """
        with open(MODULE_PATH, encoding="utf-8") as handle:
            src = handle.read()
        self.assertNotIn("_last_reasoning_content", src)
        self.assertNotIn("_last_reasoning_text", src)


if __name__ == "__main__":
    unittest.main()
