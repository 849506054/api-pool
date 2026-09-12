"""池自身出站身份确定性 + 伤害隔离（2026-09-12）。

契约：
A. 身份解析 `resolve_pool_identity`：端点 client_profile → 池级 probe_client_profile →
   最近一次真实客户端指纹 → 都没有 = "none"（调用方必须跳过出站，不编造身份）。
B. 有客户端上下文（代理路径）时行为不变：端点 profile 优先，否则忠实透传当前请求头；
   客户端没带 UA 时不再补默认 UA（原 `_DEFAULT_OUTBOUND_UA` 已删除）。
C. 池自身出站（探活/测试/拉模型/健康检测）拿不到身份 → **零 HTTP 请求**，且端点状态一个字段都不改。
D. 伤害隔离：`_POOL_INITIATED_CTX.active` 期间 `_rotate` 不冷却、不计 fail_count、不改指针，
   只写 last_error；🧪 `_apply_test_result` 非容量类失败只写观测态，容量类仍写路由态。
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")

_load_seq = 0


@contextmanager
def loaded_module(tmp_path):
    global _load_seq
    _load_seq += 1
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_identity_{_load_seq}_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        os.chdir(previous_cwd)


def make_endpoint(module, endpoint_id="ep1", **kw):
    params = dict(
        id=endpoint_id, name=endpoint_id, base_url="http://127.0.0.1:1/v1",
        api_key="test", model="mdl", priority=1, in_pool=True,
        # use_proxy=True → 走 urllib.request.urlopen 分支（use_proxy=False 会走 build_opener，
        # 单测只 patch urlopen，故这里保持默认 True）
        use_proxy=True, cooldown_minutes=5, pool_groups=["main"], max_retries=0,
    )
    params.update(kw)
    return module.Endpoint(**params)


class CountingUrlopen:
    """替换 urllib.request.urlopen：只计数并返回固定响应，永不真的发请求。"""

    def __init__(self, body=b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'):
        self.calls = []
        self._body = body

    def __call__(self, req, timeout=None):
        headers = {k.lower(): v for k, v in dict(req.header_items()).items()}
        self.calls.append({"url": req.full_url, "headers": headers})
        holder = self

        class _Resp:
            status = 200
            headers = {"Content-Type": "application/json"}

            def read(self, size=-1):
                return holder._body

            def readline(self, limit=-1):
                return holder._body

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Resp()


class IdentityResolutionTests(unittest.TestCase):
    def test_priority_endpoint_then_pool_then_baseline_then_none(self):
        with tempfile.TemporaryDirectory() as tmp, loaded_module(tmp) as module:
            profiles = {"hermes": {"headers": {"User-Agent": "hermes-agent/0.21.0", "X-Stainless-Retry-Count": "0"}}}
            ep = make_endpoint(module, client_profile="hermes")
            headers, source = module.resolve_pool_identity(ep, profiles, "", label="t")
            self.assertEqual(source, "endpoint-profile")
            self.assertEqual(headers["User-Agent"], "hermes-agent/0.21.0")

            ep_plain = make_endpoint(module, "ep2")
            headers, source = module.resolve_pool_identity(ep_plain, profiles, "hermes", label="t")
            self.assertEqual(source, "pool-profile")

            # 无 profile：退化到最近一次真实客户端指纹（这里是模拟的客户端头）
            module._client_baseline.clear()
            module._client_baseline.update({"User-Agent": "real-client/1.0"})
            headers, source = module.resolve_pool_identity(ep_plain, profiles, "", label="t")
            self.assertEqual(source, "client-baseline")
            self.assertEqual(headers["User-Agent"], "real-client/1.0")

            # 三者皆无 → none（调用方必须跳过）
            module._client_baseline.clear()
            headers, source = module.resolve_pool_identity(ep_plain, profiles, "", label="t")
            self.assertEqual(source, "none")
            self.assertEqual(headers, {})

    def test_no_default_ua_fabrication(self):
        with tempfile.TemporaryDirectory() as tmp, loaded_module(tmp) as module:
            self.assertFalse(hasattr(module, "_DEFAULT_OUTBOUND_UA"), "编造默认 UA 必须删除")
            module._client_baseline.clear()
            module._client_baseline.update({"X-Custom": "1"})   # 有头但无 UA
            module._client_ctx.headers = {}
            headers = module.passthrough_client_headers()
            self.assertNotIn("User-Agent", headers)
            self.assertEqual(headers.get("X-Custom"), "1")


class PoolInitiatedOutboundTests(unittest.TestCase):
    def test_probe_without_identity_sends_nothing_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, loaded_module(tmp) as module:
            module._client_baseline.clear()
            module._client_ctx.headers = {}
            ep = make_endpoint(module)
            ep._health = "bad"
            ep._health_error = "旧错误"
            ep._fail_count = 2
            ep._cooldown_until = 0
            module.pool = module.APIPool([ep])
            counter = CountingUrlopen()
            with mock.patch.object(module.urllib.request, "urlopen", counter):
                ok, err = module.pool._probe_endpoint(ep)
            self.assertIsNone(ok, "无身份必须返回 None（跳过）")
            self.assertEqual(err, module.POOL_IDENTITY_SKIP_REASON)
            self.assertEqual(counter.calls, [], "跳过时不得发出任何 HTTP 请求")
            self.assertEqual((ep._health, ep._health_error, ep._fail_count), ("bad", "旧错误", 2),
                             "跳过时端点状态不得改动")

    def test_probe_uses_pool_profile_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp, loaded_module(tmp) as module:
            module._client_baseline.clear()
            module._client_ctx.headers = {}
            module.pool = module.APIPool([make_endpoint(module)])
            module.pool._client_profiles = {"hermes": {"headers": {"User-Agent": "hermes-agent/0.21.0"}}}
            module.pool._probe_client_profile = "hermes"
            ep = module.pool._endpoints[0]
            counter = CountingUrlopen()
            with mock.patch.object(module.urllib.request, "urlopen", counter):
                ok, err = module.pool._probe_endpoint(ep)
            self.assertTrue(ok, err)
            self.assertEqual(len(counter.calls), 1)
            self.assertEqual(counter.calls[0]["headers"].get("user-agent"), "hermes-agent/0.21.0")

    def test_proxy_path_still_passes_through_client_headers(self):
        with tempfile.TemporaryDirectory() as tmp, loaded_module(tmp) as module:
            module.set_client_headers({"User-Agent": "hermes-agent/9.9.9", "X-Stainless-Retry-Count": "3"})
            try:
                ep = make_endpoint(module)
                module.pool = module.APIPool([ep])
                payload = {"model": ep.model, "messages": [{"role": "user", "content": "hi"}], "stream": False}
                counter = CountingUrlopen()
                with mock.patch.object(module.urllib.request, "urlopen", counter):
                    module.pool._try_endpoint(ep, payload, 5, log_usage=False, force_no_retry=True)
                sent = counter.calls[0]["headers"]
                self.assertEqual(sent.get("user-agent"), "hermes-agent/9.9.9")
                self.assertEqual(sent.get("x-stainless-retry-count"), "3")
            finally:
                module.clear_client_headers()

    def test_check_all_health_skips_write_for_skipped(self):
        with tempfile.TemporaryDirectory() as tmp, loaded_module(tmp) as module:
            module._client_baseline.clear()
            module._client_ctx.headers = {}
            ep = make_endpoint(module)
            ep._health = "ok"
            ep._health_error = ""
            ep._health_latency_ms = 42
            ep._health_last_check = 111
            module.pool = module.APIPool([ep])
            results = [(ep.id, "skipped", -1, module.POOL_IDENTITY_SKIP_REASON)]
            with module.pool._lock:
                for ep_id, health, latency, error in results:
                    target = next((e for e in module.pool._endpoints if e.id == ep_id), None)
                    if target is None or health == "skipped":
                        continue
                    target._health = health
            self.assertEqual((ep._health, ep._health_error, ep._health_last_check), ("ok", "", 111))


class DamageIsolationTests(unittest.TestCase):
    def test_rotate_is_observation_only_for_pool_initiated(self):
        with tempfile.TemporaryDirectory() as tmp, loaded_module(tmp) as module:
            ep = make_endpoint(module)
            module.pool = module.APIPool([ep])
            module.pool._set_current("main", ep.id)
            before = dict(cooldown=ep._cooldown_until, fails=ep._fail_count, current=module.pool._get_current("main"))
            module._POOL_INITIATED_CTX.active = True
            try:
                module.pool._rotate(ep, "HTTP 401: unauthorized client detected")
            finally:
                module._POOL_INITIATED_CTX.active = False
            self.assertEqual(ep._cooldown_until, before["cooldown"], "池自身请求失败不得冷却端点")
            self.assertEqual(ep._fail_count, before["fails"], "不得计失败次数")
            self.assertEqual(module.pool._get_current("main"), before["current"], "不得改路由指针")
            self.assertEqual(ep._last_error, "HTTP 401: unauthorized client detected", "只记观测态")

    def test_rotate_normal_path_still_cools(self):
        with tempfile.TemporaryDirectory() as tmp, loaded_module(tmp) as module:
            ep = make_endpoint(module)
            module.pool = module.APIPool([ep])
            module.pool._rotate(ep, "HTTP 500: upstream boom")
            self.assertGreater(ep._cooldown_until, time.time(), "真实流量失败仍应冷却端点")
            self.assertEqual(ep._fail_count, 1)

    def test_apply_test_result_isolates_non_capacity_failure(self):
        with tempfile.TemporaryDirectory() as tmp, loaded_module(tmp) as module:
            ep = make_endpoint(module)
            module.pool = module.APIPool([ep])
            module.pool._apply_test_result(ep, error="HTTP 401: unauthorized client detected", latency_ms=12)
            self.assertEqual(ep._health, "bad")
            self.assertIn("unauthorized client", ep._health_error)
            self.assertEqual(ep._cooldown_until, 0, "非容量类失败不得冷却")
            self.assertEqual(ep._fail_count, 0, "非容量类失败不得计失败次数")

    def test_apply_test_result_keeps_capacity_failures(self):
        with tempfile.TemporaryDirectory() as tmp, loaded_module(tmp) as module:
            ep_balance = make_endpoint(module, "ep-balance")
            ep_quota = make_endpoint(module, "ep-quota", priority=2)
            module.pool = module.APIPool([ep_balance, ep_quota])
            module.pool._apply_test_result(ep_balance, error="HTTP 402: Insufficient Balance", latency_ms=10)
            self.assertTrue(ep_balance._manual_unlock_required, "余额不足仍须冻结（容量类例外）")
            module.pool._apply_test_result(ep_quota, error="HTTP 429: rate limit exceeded, retry-after: 60", latency_ms=10)
            self.assertGreater(ep_quota._cooldown_until, time.time(), "429 仍须按 Retry-After 冷却")


if __name__ == "__main__":
    unittest.main()
