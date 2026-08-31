import importlib.util
import os
import sys
import tempfile
import unittest

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location(f"api_pool_devrole_test_{id(tmp_path)}", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


# 生产/探测确认的真实拒绝文案（2026-08-31）
PROD_MSGS = (
    # AgentRouter-ds4f（Anthropic 风格反序列化，生产 15:10:15）
    "HTTP 400: {\"error\":{\"message\":\"Failed to deserialize the JSON body into the target type: "
    "messages[0].role: unknown variant `developer`, expected one of `system`, `user`, `assistant`, "
    "`tool`, `latest_reminder` at line 1 column 48827\",\"type\":\"invalid_request_error\"}}",
    # SiliconFlow Qwen3-VL（探测 400 code 20015）
    "HTTP 400: {\"code\":20015,\"message\":\"Input tag 'developer' found using 'role' does not match "
    "any of the expected tags: 'system', 'user', 'assistant', 'tool'\",\"data\":null}",
    # Opencode / Console Go 上游（生产 15:10:17 Error 1214）
    "HTTP 400: {\"error\":{\"type\":\"server_error\",\"message\":\"Error from provider (Console Go): "
    "Upstream request failed: [1214] Incorrect role information\"}}",
    # qnaigc（探测 502 upstream PARAM_ERROR；同签名族）
    "HTTP 400: {\"error\":{\"message\":\"role错误，支持类型：user、system、assistant、function、tool "
    "(type: PARAM_ERROR)\",\"type\":\"upstream_error\"}}",
)

NON_ROLE_MSGS = (
    "HTTP 400: {\"error\":{\"message\":\"invalid temperature\"}}",
    "HTTP 404: model not found",
    "连接/超时错误: The read operation timed out",
    "",
)


class RoleRejectSignatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp_path:
            cls.module = load_module(tmp_path)
        cls.is_role_reject = staticmethod(cls.module.APIPool._is_role_reject_error)
        cls.downgrade = staticmethod(cls.module.APIPool._downgrade_developer_role)
        cls.has_dev = staticmethod(cls.module.APIPool._has_developer_role)

    def test_prod_signatures_detected(self):
        for msg in PROD_MSGS:
            self.assertTrue(self.is_role_reject(msg), msg[:80])

    def test_non_role_errors_not_flagged(self):
        for msg in NON_ROLE_MSGS:
            self.assertFalse(self.is_role_reject(msg), repr(msg))

    def test_5xx_role_wording_not_flagged(self):
        # qnaigc 实际以 502 返回角色错误；签名识别仅用于 4xx 客户端类轮转路径，
        # 5xx 不该触发降级标记
        self.assertFalse(self.is_role_reject(
            "HTTP 502: {\"error\":{\"message\":\"role错误，支持类型：user、system、assistant、function、tool\"}}"))

    def test_downgrade_only_touches_developer(self):
        msgs = [
            {"role": "developer", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
            {"role": "developer", "content": [{"type": "text", "text": "tail"}]},
        ]
        out = self.downgrade(msgs)
        self.assertEqual([m["role"] for m in out], ["system", "user", "assistant", "system"])
        # 非目标消息保持同一对象；tool_calls 等字段原样保留
        self.assertIs(out[1], msgs[1])
        self.assertIs(out[2], msgs[2])
        self.assertEqual(out[2]["tool_calls"], [{"id": "t1"}])
        # 原列表不被原地修改
        self.assertEqual(msgs[0]["role"], "developer")

    def test_has_developer_role(self):
        self.assertTrue(self.has_dev([{"role": "developer"}]))
        self.assertFalse(self.has_dev([{"role": "system"}, {"role": "user"}]))
        self.assertFalse(self.has_dev([]))


class RoleDowngradeRotationTests(unittest.TestCase):
    """端到端：角色拒绝 400 后，下一候选收到 system 首条消息并成功。"""

    @staticmethod
    def endpoint(module, endpoint_id, priority, model="test-model"):
        return module.Endpoint(
            id=endpoint_id,
            name=endpoint_id,
            base_url="http://127.0.0.1:1",
            api_key="test",
            model=model,
            priority=priority,
            in_pool=True,
            use_proxy=False,
        )

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.module = load_module(self.tmp_dir.name)

    def test_rotation_downgrades_and_succeeds(self):
        pool = self.module.APIPool()
        bad = self.endpoint(self.module, "bad", 1)
        good = self.endpoint(self.module, "good", 2)
        pool._endpoints.extend([bad, good])
        pool._current_endpoint_id = bad.id
        seen_payloads = []

        def fake_try(ep, payload, timeout, **kwargs):
            seen_payloads.append((ep.id, payload))
            if ep.id == "bad":
                return None, PROD_MSGS[0]
            return {"choices": [{"message": {"content": "ok"}}]}, ""

        pool._try_endpoint = fake_try
        result = pool.chat([
            {"role": "developer", "content": "You are helpful."},
            {"role": "user", "content": "hi"},
        ])
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        # bad 收到原始 developer；good 收到降级 system
        self.assertEqual(seen_payloads[0][1]["messages"][0]["role"], "developer")
        self.assertEqual(seen_payloads[1][1]["messages"][0]["role"], "system")
        # 内容不丢失
        self.assertEqual(seen_payloads[1][1]["messages"][0]["content"], "You are helpful.")
        # bad 不记账不冻结
        self.assertEqual(bad._fail_count, 0)
        self.assertEqual(bad._cooldown_until, 0)

    def test_no_downgrade_when_no_developer_in_request(self):
        pool = self.module.APIPool()
        bad = self.endpoint(self.module, "bad", 1)
        good = self.endpoint(self.module, "good", 2)
        pool._endpoints.extend([bad, good])
        pool._current_endpoint_id = bad.id
        seen_payloads = []

        def fake_try(ep, payload, timeout, **kwargs):
            seen_payloads.append((ep.id, payload))
            if ep.id == "bad":
                # 上游吐槽 role，但请求本身没有 developer → 不触发降级
                return None, PROD_MSGS[0]
            return {"choices": [{"message": {"content": "ok"}}]}, ""

        pool._try_endpoint = fake_try
        result = pool.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}])
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        # 两个端点都收到原样 system 首条
        self.assertEqual(seen_payloads[0][1]["messages"][0]["role"], "system")
        self.assertEqual(seen_payloads[1][1]["messages"][0]["role"], "system")

    def test_plain_400_keeps_developer_for_next_candidate(self):
        pool = self.module.APIPool()
        bad = self.endpoint(self.module, "bad", 1)
        good = self.endpoint(self.module, "good", 2)
        pool._endpoints.extend([bad, good])
        pool._current_endpoint_id = bad.id
        seen_payloads = []

        def fake_try(ep, payload, timeout, **kwargs):
            seen_payloads.append((ep.id, payload))
            if ep.id == "bad":
                return None, "HTTP 400: invalid request shape"
            return {"choices": [{"message": {"content": "ok"}}]}, ""

        pool._try_endpoint = fake_try
        pool.chat([{"role": "developer", "content": "s"}, {"role": "user", "content": "hi"}])
        # 普通客户端类 400 不触发角色降级，下一候选仍收到 developer
        self.assertEqual(seen_payloads[1][1]["messages"][0]["role"], "developer")


if __name__ == "__main__":
    unittest.main()
