"""探活/自检请求输出上限测试（2026-09-13）。

缺陷：Responses API 要求 `max_output_tokens >= 16`，而池的探活 ping 原先固定 `max_tokens=3`
（模型/视觉自检用 5 / 10）。`_responses_body_from_chat` 会把该值原样搬到 `max_output_tokens`，
于是 `protocol=responses` 端点的探活必然 400：
`Invalid 'max_output_tokens': integer below minimum value. Expected a value >= 16, but got 3 instead.`
→ 端点被误标 bad（生产实测：AgentRouterZ-gpt6a / AgentRouterP-gpt6a）。

修复：池自身发起的探活与自检统一走 `APIPool.PROBE_MAX_TOKENS`（16）。
覆盖：单端点探活 / 批量探活 / 模型自检 / 视觉自检，以及 responses 桥接后的实际取值。
"""

import importlib.util
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_probe_max_tokens_test_{os.getpid()}_{id(threading.current_thread())}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        module.__dict__["CONFIG_FILE"] = os.path.join(tmp_path, "api_config.json")
        module.__dict__["RUNTIME_STATE_FILE"] = os.path.join(tmp_path, "api_runtime_state.json")
        return module
    finally:
        os.chdir(previous_cwd)


def make_endpoint(module, endpoint_id, protocol="responses", in_pool=True):
    return module.Endpoint(
        id=endpoint_id, name=endpoint_id, base_url="http://127.0.0.1:1", api_key="test",
        model="m-" + endpoint_id, priority=1, in_pool=in_pool, enabled=True,
        use_proxy=False, protocol=protocol, pool_groups=["main"],
    )


REPLY = ({"choices": [{"message": {"content": "pong"}}]}, None)


class ProbeMaxTokensTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module(tempfile.mkdtemp())
        self.pool = self.module.APIPool([])
        self.payloads = []
        self.pool._try_endpoint = mock.Mock(side_effect=self._capture)

    def _capture(self, ep, payload, *args, **kwargs):
        self.payloads.append(payload)
        return REPLY

    def assert_all_probes_meet_responses_minimum(self):
        self.assertTrue(self.payloads, "未捕获到任何探活/自检请求")
        for payload in self.payloads:
            self.assertGreaterEqual(payload["max_tokens"], 16, f"探活 max_tokens 过低: {payload}")
            # responses 端点会原样搬运该值 → 桥接后也必须满足上游下限
            bridged = self.module._responses_body_from_chat(payload)
            self.assertGreaterEqual(bridged["max_output_tokens"], 16)

    def test_single_endpoint_probe(self):
        ep = make_endpoint(self.module, "r1")
        self.pool._endpoints = [ep]
        self.pool._probe_endpoint(ep)
        self.assert_all_probes_meet_responses_minimum()

    def test_mode_aware_health_check(self):
        ep = make_endpoint(self.module, "r2")
        self.pool._endpoints = [ep]
        self.pool._check_one_health(ep)
        self.assert_all_probes_meet_responses_minimum()

    def test_batch_health_check(self):
        ep = make_endpoint(self.module, "r3")
        self.pool._endpoints = [ep]
        self.pool.check_all_health()
        self.assert_all_probes_meet_responses_minimum()

    def test_model_and_vision_self_check(self):
        self.pool.test_model_latency("http://127.0.0.1:1", "k", "m", protocol="responses")
        self.pool.test_vision("http://127.0.0.1:1", "k", "m", protocol="responses")
        self.assert_all_probes_meet_responses_minimum()

    def test_probe_constant_is_above_responses_floor(self):
        self.assertGreaterEqual(self.module.APIPool.PROBE_MAX_TOKENS, 16)


if __name__ == "__main__":
    unittest.main()
