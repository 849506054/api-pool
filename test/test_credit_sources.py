"""积分徽标数据源 = 上游只读接口（2026-10-10）。

覆盖：wkm 三接口解析与 realm 归属、qoder /credits/summary 的 by_realm 映射、
kind=key/pool 两分支、无代理 opener 与硬超时、非阻塞刷新守卫、源失败保留旧读数。
全程 mock 出站（URL 路由表），不发任何真实请求。
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")

# wkm 的 prefix = 网关密钥前 12 位（上游 keysvc 口径），假前缀也按 12 位造
WKM_KEY_POOL = "wbk_poolkey1"
WKM_KEY_LIMITED = "wbk_limited1"
QODER_KEY_CN = "q" * 36
QODER_KEY_FOLLOW = "f" * 36


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_credit_sources_test_{os.getpid()}_{id(tmp_path)}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        module.CONFIG_FILE = os.path.join(tmp_path, "api_config.json")
        module.RUNTIME_STATE_FILE = os.path.join(tmp_path, "api_runtime_state.json")
        return module
    finally:
        os.chdir(previous_cwd)


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class Sequenced:
    """按调用次数依次取值的响应序列（模拟「第一把 key 被拒、第二把成功」）。
    必须是独立类型——wkm `/api/keys` 的正常响应本身就是 list，不能用 list 兼作序列。"""

    def __init__(self, *items):
        self.items = list(items)

    def next(self):
        return self.items.pop(0) if len(self.items) > 1 else self.items[0]


class _FakeOpener:
    """按 URL 后缀路由的假 opener；记录每次请求的 URL/头/超时。"""

    def __init__(self, routes, calls):
        self.routes = routes
        self.calls = calls

    def open(self, req, timeout=None):
        url = req.full_url
        self.calls.append({"url": url, "headers": dict(req.headers), "timeout": timeout})
        for suffix, payload in self.routes.items():
            if url.endswith(suffix):
                if isinstance(payload, Sequenced):
                    payload = payload.next()
                if isinstance(payload, Exception):
                    raise payload
                return _FakeResponse(payload)
        raise AssertionError(f"未预期的出站请求: {url}")


class CreditSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.module = load_module(self.tmp.name)
        self.module.WKM_API_TOKEN = "wbt_test_token"
        self.accounts_dir = os.path.join(self.tmp.name, "qoder_accounts")
        os.makedirs(self.accounts_dir, exist_ok=True)
        self.module.QODER_ACCOUNTS_DIR = self.accounts_dir
        self.module._CREDIT_WARNED.clear()
        self.pool = self.module.APIPool()
        self.module.pool = self.pool
        self.calls = []
        self.handlers = []
        self.routes = {}

    def write_qoder_settings(self, keys, active="cn"):
        with open(os.path.join(self.accounts_dir, "settings.json"), "w", encoding="utf-8") as fh:
            json.dump({"api_keys": keys}, fh)
        with open(os.path.join(self.accounts_dir, "active_realm.json"), "w", encoding="utf-8") as fh:
            json.dump({"realm": active}, fh)

    def install_http_routes(self, routes):
        self.routes = routes
        opener = _FakeOpener(routes, self.calls)

        def fake_build_opener(*handlers):
            self.handlers = handlers
            return opener

        patcher = mock.patch("urllib.request.build_opener", fake_build_opener)
        patcher.start()
        self.addCleanup(patcher.stop)

    def wkm_routes(self, keys=None, snapshot=None, accounts=None):
        return {
            "/api/keys": keys if keys is not None else [],
            "/api/accounts/credits-snapshot": snapshot or {"accounts": []},
            "/api/accounts": accounts or {"accounts": []},
        }

    # ── wkm ──────────────────────────────────────────────────────────

    def test_wkm_maps_snapshot_realm_and_key_quota(self):
        self.install_http_routes(self.wkm_routes(
            keys=[{"prefix": WKM_KEY_POOL, "realm": "cn", "quota_credit": 0, "used_credit": 0, "enabled": True},
                  {"prefix": WKM_KEY_LIMITED, "realm": "global", "quota_credit": 500, "used_credit": 120, "enabled": True},
                  {"prefix": "wbk_disabledxxxx", "realm": "cn", "quota_credit": 0, "used_credit": 0, "enabled": False}],
            snapshot={"accounts": [{"uid": "u1", "credits": 100},
                                   {"uid": "u2", "credits": 50},
                                   {"uid": "u3", "credits": 7}]},
            accounts={"accounts": [{"uid": "u1", "realm": "cn", "credits": 999},
                                   {"uid": "u2", "realm": "global", "credits": 0}]},
        ))
        out = self.module.APIPool._read_wkm_credits(self.pool)
        # uid→realm 来自 /api/accounts；未登记 uid（u3）按既有口径回落 cn
        self.assertEqual(out[WKM_KEY_POOL]["kind"], "pool")
        self.assertEqual(out[WKM_KEY_POOL]["realm"], "cn")
        self.assertEqual(out[WKM_KEY_POOL]["remaining"], 107.0)
        # 限额 key：quota_credit - used_credit，kind=key
        self.assertEqual(out[WKM_KEY_LIMITED]["kind"], "key")
        self.assertEqual(out[WKM_KEY_LIMITED]["remaining"], 380.0)
        # 停用的 key 不进徽标
        self.assertEqual(len(out), 2)
        for call in self.calls:
            self.assertEqual(call["headers"].get("Authorization"), "Bearer wbt_test_token")

    def test_wkm_unbound_realm_key_shows_both_realms(self):
        self.install_http_routes(self.wkm_routes(
            keys=[{"prefix": WKM_KEY_POOL, "realm": "", "quota_credit": 0, "used_credit": 0, "enabled": True}],
            snapshot={"accounts": [{"uid": "u1", "credits": 100}, {"uid": "u2", "credits": 50}]},
            accounts={"accounts": [{"uid": "u1", "realm": "cn"}, {"uid": "u2", "realm": "global"}]},
        ))
        out = self.module.APIPool._read_wkm_credits(self.pool)
        self.assertEqual(out[WKM_KEY_POOL]["realm"], "both")
        self.assertEqual(out[WKM_KEY_POOL]["remaining"], 150.0)

    def test_wkm_without_token_returns_empty_and_warns_once(self):
        self.module.WKM_API_TOKEN = ""
        with mock.patch.object(self.module, "sys_log") as log:
            self.assertEqual(self.module.APIPool._read_wkm_credits(self.pool), {})
            self.assertEqual(self.module.APIPool._read_wkm_credits(self.pool), {})
        self.assertEqual(log.call_count, 1)
        self.assertEqual(self.calls, [])

    def test_wkm_failure_is_not_swallowed_into_wrong_numbers(self):
        """realm 归属不全（/api/accounts 失败）时整体失败，由上层保留旧读数。"""
        self.install_http_routes(self.wkm_routes())
        self.routes["/api/accounts"] = urllib.error.URLError("boom")
        with self.assertRaises(urllib.error.URLError):
            self.module.APIPool._read_wkm_credits(self.pool)

    # ── qoder ────────────────────────────────────────────────────────

    def test_qoder_summary_by_realm_and_key_realm_binding(self):
        self.write_qoder_settings([
            {"key": QODER_KEY_CN, "realm": "cn", "enabled": True},
            {"key": QODER_KEY_FOLLOW, "realm": "", "enabled": True},
            {"key": "x" * 36, "realm": "intl", "enabled": False},
        ], active="cn")
        self.install_http_routes({"/credits/summary": {"by_realm": {"cn": 2999, "intl": 400},
                                                       "totals": {"remain": 3399, "accounts": 8}}})
        out = self.module.APIPool._read_qoder_credits(self.pool)
        self.assertEqual(out[QODER_KEY_CN[:12]]["remaining"], 2999.0)
        self.assertEqual(out[QODER_KEY_CN[:12]]["kind"], "pool")
        # 绑定为空 = 跟随 active_realm.json
        self.assertEqual(out[QODER_KEY_FOLLOW[:12]]["realm"], "cn")
        self.assertEqual(out[QODER_KEY_FOLLOW[:12]]["remaining"], 2999.0)
        self.assertEqual(len(out), 2)
        self.assertEqual(self.calls[0]["url"], self.module.QODER_API_BASE + "/credits/summary")

    def test_qoder_tries_next_key_when_first_is_rejected(self):
        """第一把 key 被吊销（401）→ 换下一把；网络类错误不换（避免对挂掉的实例轮着超时）。"""
        k1, k2 = "1" * 36, "2" * 36
        self.write_qoder_settings([{"key": k1, "realm": "cn", "enabled": True},
                                   {"key": k2, "realm": "intl", "enabled": True}], active="cn")
        self.install_http_routes({"/credits/summary": Sequenced(
            urllib.error.HTTPError("u", 401, "unauthorized", {}, None),
            {"by_realm": {"cn": 2999, "intl": 400}},
        )})
        out = self.module.APIPool._read_qoder_credits(self.pool)
        self.assertEqual(out[k2[:12]]["remaining"], 400.0)
        self.assertEqual([c["headers"]["Authorization"] for c in self.calls],
                         ["Bearer " + k1, "Bearer " + k2])

        # 网络故障（无 HTTP 状态码）→ 立即上抛，不逐把重试
        self.calls.clear()
        self.install_http_routes({"/credits/summary": urllib.error.URLError("down")})
        with self.assertRaises(urllib.error.URLError):
            self.module.APIPool._read_qoder_credits(self.pool)
        self.assertEqual(len(self.calls), 1)

    # ── 出站契约 ─────────────────────────────────────────────────────

    def test_outbound_bypasses_global_proxy_and_honours_timeout(self):
        self.install_http_routes(self.wkm_routes())
        self.module.APIPool._read_wkm_credits(self.pool)
        self.assertEqual(len(self.handlers), 1)
        self.assertIsInstance(self.handlers[0], urllib.request.ProxyHandler)
        self.assertEqual(self.handlers[0].proxies, {})
        for call in self.calls:
            self.assertEqual(call["timeout"], self.module.CREDIT_HTTP_TIMEOUT)

    # ── 缓存 / 失败保留旧读数 ────────────────────────────────────────

    def test_source_failure_keeps_previous_readings(self):
        self.write_qoder_settings([{"key": QODER_KEY_CN, "realm": "cn", "enabled": True}], active="cn")
        self.install_http_routes(dict(self.wkm_routes(
            keys=[{"prefix": WKM_KEY_POOL, "realm": "cn", "quota_credit": 0, "used_credit": 0, "enabled": True}],
            snapshot={"accounts": [{"uid": "u1", "credits": 100}]},
            accounts={"accounts": [{"uid": "u1", "realm": "cn"}]},
        ), **{"/credits/summary": {"by_realm": {"cn": 1000}}}))
        first = self.module.APIPool._read_credit_sources(self.pool)
        self.assertEqual(first[WKM_KEY_POOL]["remaining"], 100.0)
        self.assertEqual(first[QODER_KEY_CN[:12]]["remaining"], 1000.0)

        # wkm 挂了、qoder 换值：wkm 保留旧读数，qoder 刷新
        self.pool._credit_cache = first
        self.routes["/api/keys"] = urllib.error.URLError("wkm down")
        self.routes["/credits/summary"] = {"by_realm": {"cn": 1234}}
        with mock.patch.object(self.module, "sys_log"):
            second = self.module.APIPool._read_credit_sources(self.pool)
        self.assertEqual(second[WKM_KEY_POOL]["remaining"], 100.0)
        self.assertEqual(second[QODER_KEY_CN[:12]]["remaining"], 1234.0)

    def test_credit_view_skips_refresh_while_one_is_running(self):
        ep = self.module.Endpoint(
            id="ep-1", name="ep-1", site_name="site-a",
            base_url="https://site-a.example/v1", api_key=WKM_KEY_POOL + "zzz", model="m1",
            priority=1, priority_by_group={}, timeout=17, enabled=True, in_pool=True,
            use_proxy=False, protocol="openai", pool_groups=["main"],
        )
        self.install_http_routes(self.wkm_routes(
            keys=[{"prefix": WKM_KEY_POOL, "realm": "cn", "quota_credit": 0, "used_credit": 0, "enabled": True}],
            snapshot={"accounts": [{"uid": "u1", "credits": 42}]},
            accounts={"accounts": [{"uid": "u1", "realm": "cn"}]},
        ))
        self.pool._credit_last_read = 0.0
        self.pool._credit_cache = {WKM_KEY_POOL[:12]: {"kind": "pool", "realm": "cn", "remaining": 7.0,
                                                       "src": "wkm", "ts": 0.0}}
        self.assertTrue(self.pool._credit_refresh_lock.acquire(blocking=False))
        try:
            view = self.pool._credit_view(ep)
            self.assertEqual(view["remaining"], 7.0)     # 刷新在跑 → 直接读旧缓存
            self.assertEqual(self.calls, [])              # 且不发任何出站
            self.assertEqual(self.pool._credit_last_read, 0.0)   # 时间戳不前进，稍后可重试
        finally:
            self.pool._credit_refresh_lock.release()
        view = self.pool._credit_view(ep)
        self.assertEqual(view["remaining"], 42.0)
        self.assertGreater(len(self.calls), 0)

    def test_credit_view_uses_cache_within_interval(self):
        ep = self.module.Endpoint(
            id="ep-2", name="ep-2", site_name="site-a",
            base_url="https://site-a.example/v1", api_key=QODER_KEY_CN, model="m1",
            priority=1, priority_by_group={}, timeout=17, enabled=True, in_pool=True,
            use_proxy=False, protocol="openai", pool_groups=["main"],
        )
        self.install_http_routes({})
        self.pool._credit_last_read = self.module.time.time()
        self.pool._credit_cache = {QODER_KEY_CN[:12]: {"kind": "pool", "realm": "cn", "remaining": 5.0,
                                                       "src": "qoder", "ts": 0.0}}
        self.assertEqual(self.pool._credit_view(ep)["remaining"], 5.0)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
