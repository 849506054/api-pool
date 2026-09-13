"""Hermes 身份采样 + profile 身份手动同步（2026-09-13）。

契约：
- 采样仅在代理路径入站 UA 为 `hermes-agent/<ver>` 时发生；只留最近一份，**仅内存不落盘**
- 采样头经保留头过滤（Authorization/Host/... 不入样本）+ Accept-Encoding 收敛
- `POST /api/client-profiles/sync-identity` 按**策略 b** 更新：只覆盖身份白名单键且在采样中存在的键，
  profile 其余头保持原值；无采样/未知 profile → 400；无变更时幂等（changes 为空）
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_profile_sync_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


HERMES_SAMPLE_HEADERS = {
    "User-Agent": "hermes-agent/0.21.2",
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate, br",
    "X-Stainless-Lang": "python",
    "X-Stainless-Package-Version": "2.24.0",
    "X-Stainless-OS": "Linux",
    "X-Stainless-Arch": "x64",
    "X-Stainless-Runtime": "CPython",
    "X-Stainless-Runtime-Version": "3.13.5",
    "X-Stainless-Async": "false",
    "Authorization": "Bearer secret",
    "Host": "127.0.0.1:5200",
}

PROFILE_HEADERS = {
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
    "x-stainless-read-timeout": "600",
    "X-Custom-Keep": "keep-me",
}


class HermesIdentitySampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp_path:
            cls.module = load_module(tmp_path)

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.mkdtemp()
        os.chdir(self._tmp)
        with self.module._hermes_identity_lock:
            self.module._hermes_identity_sample.clear()
        self.module.pool._client_profiles = {}

    def tearDown(self):
        with self.module._hermes_identity_lock:
            self.module._hermes_identity_sample.clear()
        os.chdir(self._orig_cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── 采样 ──────────────────────────────────────────────────────────

    def test_non_hermes_ua_is_not_sampled(self):
        self.module.set_client_headers({"User-Agent": "OpenAI/Python 2.24.0"}, source="/v1/chat/completions")
        self.assertFalse(self.module.get_hermes_identity_sample()["available"])
        self.module.set_client_headers({"User-Agent": "Python-urllib/3.13"}, source="/v1/chat/completions")
        self.assertFalse(self.module.get_hermes_identity_sample()["available"])
        self.module.set_client_headers({}, source="/v1/chat/completions")
        self.assertFalse(self.module.get_hermes_identity_sample()["available"])

    def test_hermes_ua_sampled_with_version_and_source(self):
        self.module.set_client_headers(HERMES_SAMPLE_HEADERS, source="/v1/chat/completions")
        sample = self.module.get_hermes_identity_sample()
        self.assertTrue(sample["available"])
        self.assertEqual(sample["version"], "0.21.2")
        self.assertEqual(sample["user_agent"], "hermes-agent/0.21.2")
        self.assertEqual(sample["source"], "/v1/chat/completions")
        self.assertGreater(sample["ts"], 0)

    def test_only_latest_sample_is_kept(self):
        self.module.set_client_headers(HERMES_SAMPLE_HEADERS, source="/v1/chat/completions")
        self.module.set_client_headers(dict(HERMES_SAMPLE_HEADERS, **{"User-Agent": "hermes-agent/0.22.0"}),
                                       source="/v1/responses")
        sample = self.module.get_hermes_identity_sample()
        self.assertEqual(sample["version"], "0.22.0")
        self.assertEqual(sample["source"], "/v1/responses")

    def test_sample_filters_reserved_headers_and_accept_encoding(self):
        self.module.set_client_headers(HERMES_SAMPLE_HEADERS, source="/v1/chat/completions")
        headers = self.module.get_hermes_identity_sample()["headers"]
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Host", headers)
        # br 不在解压链支持集合内 → 收敛；其余身份头保留
        self.assertEqual(headers["Accept-Encoding"], "gzip, deflate")
        self.assertEqual(headers["X-Stainless-Package-Version"], "2.24.0")

    # ── 同步（策略 b）─────────────────────────────────────────────────

    def _seed_profile(self, name="hermes", headers=None):
        self.module.pool._client_profiles[name] = {"headers": dict(headers or PROFILE_HEADERS)}

    def test_sync_updates_identity_fields_only(self):
        self._seed_profile()
        self.module.set_client_headers(HERMES_SAMPLE_HEADERS, source="/v1/chat/completions")
        code, data, _ = self.module.api_handler("POST", "/api/client-profiles/sync-identity", {"name": "hermes"})
        self.assertEqual(code, 200, data)
        changed = {c["header"]: c for c in data["changes"]}
        self.assertEqual(changed["User-Agent"]["old"], "hermes-agent/0.21.0")
        self.assertEqual(changed["User-Agent"]["new"], "hermes-agent/0.21.2")
        headers = self.module.pool._client_profiles["hermes"]["headers"]
        self.assertEqual(headers["User-Agent"], "hermes-agent/0.21.2")
        # profile 独有的非身份头保留原值（策略 b：不删不增）
        self.assertEqual(headers["X-Custom-Keep"], "keep-me")
        self.assertEqual(headers["x-stainless-read-timeout"], "600")
        # 采样里含但属于非身份白名单的头（Accept）不得被写入/新增
        self.assertEqual(headers["Accept"], "application/json")
        self.assertNotIn("X-Stainless-Async", changed)
        self.assertEqual(len(data["changes"]), 1)

    def test_sync_unknown_profile_and_empty_sample(self):
        self.module.set_client_headers(HERMES_SAMPLE_HEADERS, source="/v1/chat/completions")
        code, data, _ = self.module.api_handler("POST", "/api/client-profiles/sync-identity", {"name": "nope"})
        self.assertEqual(code, 400)
        self.assertIn("不存在", data["error"])

        with self.module._hermes_identity_lock:
            self.module._hermes_identity_sample.clear()
        self._seed_profile()
        code, data, _ = self.module.api_handler("POST", "/api/client-profiles/sync-identity", {"name": "hermes"})
        self.assertEqual(code, 400)
        self.assertIn("暂无 Hermes 流量采样", data["error"])

    def test_sync_is_idempotent(self):
        self._seed_profile()
        self.module.set_client_headers(HERMES_SAMPLE_HEADERS, source="/v1/chat/completions")
        code, first, _ = self.module.api_handler("POST", "/api/client-profiles/sync-identity", {"name": "hermes"})
        self.assertEqual(code, 200)
        self.assertEqual(len(first["changes"]), 1)
        code, second, _ = self.module.api_handler("POST", "/api/client-profiles/sync-identity", {"name": "hermes"})
        self.assertEqual(code, 200)
        self.assertEqual(second["changes"], [])

    def test_sync_persists_and_exposes_sample_meta(self):
        self._seed_profile()
        self.module.set_client_headers(HERMES_SAMPLE_HEADERS, source="/v1/chat/completions")
        code, data, _ = self.module.api_handler("GET", "/api/client-profiles", {})
        self.assertEqual(code, 200)
        self.assertTrue(data["hermes_sample"]["available"])
        self.assertEqual(data["hermes_sample"]["version"], "0.21.2")
        self.assertEqual(data["hermes_sample"]["source"], "/v1/chat/completions")

        self.module.api_handler("POST", "/api/client-profiles/sync-identity", {"name": "hermes"})
        with open("api_config.json", "r", encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(
            cfg["client_profiles"]["hermes"]["headers"]["User-Agent"], "hermes-agent/0.21.2",
        )

    def test_get_without_sample_reports_unavailable(self):
        code, data, _ = self.module.api_handler("GET", "/api/client-profiles", {})
        self.assertEqual(code, 200)
        self.assertFalse(data["hermes_sample"]["available"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
