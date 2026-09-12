"""客户端伪装 Client Profile（2026-09-10，2026-09-11 改为两模式透明网关）。

契约：
- client_profile 为具体名字 → 伪装：出站头集合 == 该 profile 头集（不叠加客户端头，避免混合指纹）
- client_profile 为空 → 透传：入站头全量复制（连接级/池托管头除外）
- 合并优先级：客户端特征/profile < default_headers < extra_headers
- Accept-Encoding 收敛到解压链支持的编码（剔除 br/zstd）
- 未知 profile 名降级为透传 + WARN，不 500
- profile 保留头（Host/Content-Length/Authorization/x-api-key/anthropic-version）被过滤
- profile 全部存在配置文件里，可编辑/覆盖/删除（含 hermes）
- 探活/拉模型线程无客户端上下文 → 复用客户端基线
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_client_profile_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        # 2026-09-12：池自身出站需确定身份；测试默认模拟"已有真实客户端打过池"。
        module._client_baseline.update({"User-Agent": "pytest-client/1.0"})
        return module
    finally:
        os.chdir(previous_cwd)


class FakeResponse:
    status = 200
    headers = {}

    def read(self):
        return b'{"choices":[{"message":{"role":"assistant","content":"ok"}}],"usage":{}}'

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


GOLDEN = {
    "User-Agent": "hermes-agent/0.21.0",
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
    "X-Stainless-Lang": "python",
    "X-Stainless-Package-Version": "2.24.0",
    "X-Stainless-OS": "Linux",
    "X-Stainless-Arch": "x64",
    "X-Stainless-Runtime": "CPython",
    "X-Stainless-Runtime-Version": "3.13.5",
    "X-Stainless-Async": "false",
    "x-stainless-retry-count": "0",
    "x-stainless-read-timeout": "600",
}
GOLDEN_LOWER = {k.lower(): v for k, v in GOLDEN.items()}


class ClientProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp_path:
            cls.module = load_module(tmp_path)

    def setUp(self):
        self.module.clear_client_headers()
        self.module._client_baseline.clear()

    def tearDown(self):
        self.module.clear_client_headers()
        self.module._client_baseline.clear()

    def _capture_outbound_headers(self, endpoint, payload=None, profiles=None, client_headers=None, in_probe_thread=False):
        module = self.module
        pool = module.APIPool()
        if profiles is not None:
            pool._client_profiles = profiles
        if client_headers is not None:
            module.set_client_headers(client_headers)
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["headers"] = {k.lower(): v for k, v in request.header_items()}
            return FakeResponse()

        def run():
            with mock.patch.object(module.urllib.request, "urlopen", side_effect=fake_urlopen):
                module.clear_client_headers() if in_probe_thread else None
                result, error = pool._try_endpoint(
                    endpoint,
                    payload or {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": False},
                    30,
                    log_usage=False,
                    is_probe=in_probe_thread,
                )
            seen["result"] = (result, error)

        if in_probe_thread:
            # 探活/测试线程：无客户端线程局部状态，只能靠客户端基线
            import threading
            t = threading.Thread(target=run)
            t.start()
            t.join()
        else:
            run()
        self.assertIsNotNone(seen["result"][0], f"request failed: {seen['result'][1]}")
        return seen["headers"]

    def _endpoint(self, **kwargs):
        params = {
            "id": "ep1", "name": "ep1", "base_url": "https://upstream.example/v1",
            "api_key": "sk-test", "model": "m", "max_retries": 0, "use_proxy": True,
        }
        params.update(kwargs)
        return self.module.Endpoint(**params)

    # ── 伪装：profile 头集为唯一来源 ──

    def test_profile_applies_full_golden_headers(self):
        h = self._capture_outbound_headers(
            self._endpoint(client_profile="hermes"),
            profiles={"hermes": {"headers": GOLDEN}},
            client_headers={"User-Agent": "some-other-client/1.0", "X-Other": "1"},
        )
        for k, v in GOLDEN_LOWER.items():
            self.assertEqual(h.get(k), v, f"missing/mismatch {k}")

    def test_profile_does_not_mix_client_headers(self):
        h = self._capture_outbound_headers(
            self._endpoint(client_profile="codex-tui"),
            profiles={"codex-tui": {"headers": {"User-Agent": "codex-tui/0.118.0", "x-app": "cli"}}},
            client_headers={"User-Agent": "hermes-agent/0.21.0", "X-Stainless-Lang": "python"},
        )
        self.assertEqual(h.get("user-agent"), "codex-tui/0.118.0")
        self.assertEqual(h.get("x-app"), "cli")
        # 伪装成别的客户端时不得残留当前客户端的指纹头
        self.assertNotIn("x-stainless-lang", h)

    # ── 透传：入站头全量复制 ──

    def test_passthrough_copies_client_headers(self):
        h = self._capture_outbound_headers(
            self._endpoint(),
            client_headers={
                "User-Agent": "hermes-agent/0.21.0", "Accept": "application/json",
                "X-Stainless-Lang": "python", "X-App": "cli", "Accept-Encoding": "gzip, deflate",
            },
        )
        self.assertEqual(h.get("user-agent"), "hermes-agent/0.21.0")
        self.assertEqual(h.get("accept"), "application/json")
        self.assertEqual(h.get("x-stainless-lang"), "python")
        self.assertEqual(h.get("x-app"), "cli")

    def test_passthrough_drops_managed_and_hop_by_hop_headers(self):
        h = self._capture_outbound_headers(
            self._endpoint(),
            client_headers={
                "User-Agent": "hermes-agent/0.21.0", "Host": "localhost:5100",
                "Content-Length": "1234", "Connection": "keep-alive",
                "Authorization": "Bearer pool-key", "x-api-key": "pool-key",
                "anthropic-version": "2023-06-01",
            },
        )
        self.assertNotEqual(h.get("host"), "localhost:5100")
        self.assertEqual(h.get("authorization"), "Bearer sk-test")
        self.assertNotIn("x-api-key", h)  # 池托管头：openai 协议不写，客户端带来的也不放行
        self.assertNotEqual(h.get("content-length"), "1234")
        self.assertNotEqual(h.get("connection"), "keep-alive")

    def test_passthrough_without_identity_does_not_fabricate(self):
        """2026-09-12：透传分支不再补默认 UA；池自身出站无身份时直接跳过（抛 PoolIdentityUnavailable）。"""
        self.module._client_baseline.clear()
        self.module.clear_client_headers()
        h = self.module.resolve_outbound_headers("")
        self.assertEqual(h, {})
        self.assertNotIn("x-stainless-lang", h)
        with self.assertRaises(self.module.PoolIdentityUnavailable):
            self._capture_outbound_headers(self._endpoint())

    # ── 优先级链 ──

    def test_endpoint_default_headers_override_profile(self):
        ep = self._endpoint(client_profile="hermes", default_headers={"User-Agent": "custom/1.0"})
        h = self._capture_outbound_headers(ep, profiles={"hermes": {"headers": GOLDEN}})
        self.assertEqual(h.get("user-agent"), "custom/1.0")
        self.assertEqual(h.get("x-stainless-lang"), "python")  # 其余头仍来自 profile

    def test_extra_headers_override_profile(self):
        ep = self._endpoint(client_profile="hermes", extra_headers={"X-Stainless-Lang": "js"})
        h = self._capture_outbound_headers(ep, profiles={"hermes": {"headers": GOLDEN}})
        self.assertEqual(h.get("x-stainless-lang"), "js")

    def test_endpoint_default_headers_override_passthrough(self):
        ep = self._endpoint(default_headers={"User-Agent": "hermes-agent/0.20.5"})
        h = self._capture_outbound_headers(ep, client_headers={"User-Agent": "hermes-agent/0.21.0"})
        self.assertEqual(h.get("user-agent"), "hermes-agent/0.20.5")

    # ── 降级与保护 ──

    def test_unknown_profile_falls_back_to_passthrough(self):
        h = self._capture_outbound_headers(
            self._endpoint(client_profile="no-such"),
            client_headers={"User-Agent": "hermes-agent/0.21.0", "X-Stainless-Lang": "python"},
        )
        self.assertEqual(h.get("user-agent"), "hermes-agent/0.21.0")
        self.assertEqual(h.get("x-stainless-lang"), "python")

    def test_sanitize_filters_reserved_headers(self):
        module = self.module
        clean, dropped = module._sanitize_profile_headers({
            "User-Agent": "x", "Host": "evil.example", "Authorization": "Bearer x",
            "x-api-key": "k", "anthropic-version": "2023-06-01",
            "Content-Length": "99", "Accept": "application/json",
        })
        self.assertEqual(
            sorted(dropped),
            ["Authorization", "Content-Length", "Host", "anthropic-version", "x-api-key"],
        )
        self.assertEqual(clean, {"User-Agent": "x", "Accept": "application/json"})

    def test_profile_reserved_header_never_reaches_wire(self):
        h = self._capture_outbound_headers(
            self._endpoint(client_profile="bad"),
            profiles={"bad": {"headers": {"Authorization": "Bearer evil", "X-App": "cli"}}},
        )
        self.assertEqual(h.get("authorization"), "Bearer sk-test")
        self.assertEqual(h.get("x-app"), "cli")

    # ── Accept-Encoding 收敛 ──

    def test_accept_encoding_filtered_on_passthrough(self):
        h = self._capture_outbound_headers(
            self._endpoint(),
            client_headers={"User-Agent": "curl/8.5.0", "Accept-Encoding": "gzip, deflate, br, zstd"},
        )
        self.assertEqual(h.get("accept-encoding"), "gzip, deflate")

    def test_accept_encoding_filtered_on_profile(self):
        h = self._capture_outbound_headers(
            self._endpoint(client_profile="p"),
            profiles={"p": {"headers": {"User-Agent": "p/1.0", "Accept-Encoding": "br"}}},
        )
        self.assertNotIn("accept-encoding", h)

    # ── 探活/测试/拉模型复用客户端基线 ──

    def test_probe_reuses_client_baseline_without_thread_context(self):
        module = self.module
        module.set_client_headers({"User-Agent": "hermes-agent/0.21.0", "X-Stainless-Lang": "python"})
        h = self._capture_outbound_headers(self._endpoint(), in_probe_thread=True)
        # 探活线程无客户端上下文 → 复用基线，避免 UA 白名单上游把探活判成 401
        self.assertEqual(h.get("user-agent"), "hermes-agent/0.21.0")
        self.assertEqual(h.get("x-stainless-lang"), "python")

    def test_probe_uses_profile_when_endpoint_has_one(self):
        h = self._capture_outbound_headers(
            self._endpoint(client_profile="hermes"),
            profiles={"hermes": {"headers": GOLDEN}},
            client_headers={"User-Agent": "hermes-agent/0.21.0"},
            in_probe_thread=True,
        )
        self.assertEqual(h.get("x-stainless-package-version"), "2.24.0")

    # ── profile CRUD：全部可编辑（含 hermes） ──

    def test_profiles_all_editable(self):
        pool = self.module.APIPool()
        pool._client_profiles = {}
        pool.save_client_profile("hermes", dict(GOLDEN))
        listed = pool.list_client_profiles()
        self.assertEqual([p["name"] for p in listed], ["hermes"])
        self.assertFalse(listed[0]["builtin"])
        # 客户端版本升级 → 直接覆盖同名 profile（不再有内建只读限制）
        pool.save_client_profile("hermes", {"User-Agent": "hermes-agent/0.22.0", "Accept": "application/json"})
        self.assertEqual(pool.list_client_profiles()[0]["headers"]["User-Agent"], "hermes-agent/0.22.0")
        pool.delete_client_profile("hermes")
        self.assertEqual(pool.list_client_profiles(), [])

    def test_save_rejects_empty_name(self):
        pool = self.module.APIPool()
        pool._client_profiles = {}
        with self.assertRaises(ValueError):
            pool.save_client_profile("   ", {"User-Agent": "x"})


if __name__ == "__main__":
    unittest.main()
