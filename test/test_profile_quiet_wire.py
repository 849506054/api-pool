"""现有同步和关闭检测的实线出站契约。"""
import copy
import json
import unittest

import test_identity_wire as wire

IDENTITY = wire.IDENTITY


class ProfileQuietWireTests(wire.IdentityWireTests):
    def test_sync_latest_client_body_and_headers(self):
        sample = {**IDENTITY, "User-Agent": "codex-cli/1.2.3", "Accept-Encoding": "br, zstd;q=0.8",
                  "X-App": "codex", "Cookie": "private", "Authorization": "secret", "Traceparent": "trace"}
        body = {"messages": [{"role": "user", "content": "private conversation"}], "tools": [{"name": "private"}],
                "user": "sample-user", "metadata": {"client_name": "codex", "session_id": "sample-session",
                "business": "private"}, "api_key": "secret"}
        self.assertIn("identity-ok", self.request(sample, route="responses", extra=body))
        self.mod.pool._client_profiles = {"target": {"headers": {"X-Old-Client": "old"}}}
        _, response, _ = self.mod.api_handler("POST", "/api/client-profiles/sync-identity", {"name": "target"})
        self.assertTrue(response["ok"], response)
        profile = self.mod.pool._client_profiles["target"]
        self.assertEqual(profile["headers"]["User-Agent"], "codex-cli/1.2.3")
        self.assertEqual(profile["headers"]["Accept-Encoding"], "br, zstd;q=0.8")
        for key in ("Cookie", "Authorization", "Traceparent", "X-Old-Client"):
            self.assertNotIn(key, profile["headers"])
        self.assertEqual(profile["body_identity"], {"user": "sample-user", "metadata": {"client_name": "codex", "session_id": "sample-session"}})
        self.mod.clear_client_headers()
        self.ep.client_profile = "target"
        self.assertIn("identity-ok", self.request({**IDENTITY, "X-App": "caller"}))
        self.assertEqual(self.calls[-1]["x-app"], "codex")
        self.assertEqual(self.calls[-1]["accept-encoding"], "br, zstd;q=0.8")
        self.assertEqual(self.bodies[-1]["user"], "sample-user")
        self.assertEqual(self.bodies[-1]["metadata"]["client_name"], "codex")
        self.assertEqual(self.bodies[-1]["messages"][0]["content"], "hi")
        # Editing only the headers preserves synchronized body identity.
        self.mod.pool.save_client_profile("target", {"User-Agent": "codex-cli/1.2.4"})
        self.assertEqual(self.mod.pool._client_profiles["target"]["body_identity"], profile["body_identity"])
        # Snapshot reload from the saved config retains both fields.
        with open("api_config.json", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertIn("body_identity", saved["client_profiles"]["target"])

    def test_profile_version_and_compression_values_on_wire(self):
        self.ep.client_profile = "target"
        self.mod.pool._client_profiles = {"target": {"headers": {**IDENTITY, "User-Agent": "target/2"}}}
        for encoding in ("br", "zstd"):
            type(self).encoding = encoding
            for protocol in ("openai", "anthropic", "responses"):
                with self.subTest(encoding=encoding, protocol=protocol):
                    self.ep.protocol = protocol
                    self.assertIn("identity-ok", self.request({"User-Agent": "caller/1"}))
                    self.assertEqual(self.calls[-1]["user-agent"], "target/2")
                    self.assertEqual(self.calls[-1]["anthropic-version"], IDENTITY["Anthropic-Version"])
                    self.assertEqual(self.calls[-1]["accept-encoding"], IDENTITY["Accept-Encoding"])

    def test_missing_or_empty_profile_has_zero_outbound(self):
        before = len(self.calls)
        self.mod.set_client_headers(IDENTITY)
        try:
            for entry in (None, {"headers": {}}, {"headers": {"Authorization": "secret"}}):
                self.ep.client_profile = "target"
                self.mod.pool._client_profiles = {"target": entry} if entry else {}
                with self.assertRaises(self.mod.PoolIdentityUnavailable):
                    self.mod.pool._try_endpoint(self.ep, {"model": "m", "messages": []}, 5)
        finally:
            self.mod.clear_client_headers()
        self.assertEqual(len(self.calls), before)

    def test_profile_body_bridge_retains_business_values(self):
        self.ep.client_profile = "target"
        self.mod.pool._client_profiles = {"target": {"headers": {"User-Agent": "target/1"},
            "body_identity": {"user": "target-user", "metadata": {"client_name": "target", "session_id": "s1"}}}}
        payload = {"model": "m", "user": "caller", "metadata": {"client_name": "caller", "business": "keep"}, "tools": [{"a": 1}]}
        original = copy.deepcopy(payload)
        for protocol in ("openai", "responses", "anthropic", "gemini"):
            self.ep.protocol = protocol
            converted = self.mod.pool._apply_profile_body_identity(payload, self.ep)
            self.assertEqual(converted["metadata"]["business"], "keep")
            self.assertEqual(converted["tools"], [{"a": 1}])
            if protocol == "anthropic":
                self.assertEqual(converted["metadata"]["user_id"], "target-user")
            elif protocol != "gemini":
                self.assertEqual(converted["user"], "target-user")
                self.assertEqual(converted["metadata"]["client_name"], "target")
            self.assertNotEqual(converted["metadata"].get("client_name"), "caller")
        self.assertEqual(payload, original)

    def test_disabled_management_and_models_zero_outbound(self):
        before = len(self.calls)
        state = (self.ep._fail_count, self.ep._cooldown_until, self.ep._last_error)
        self.mod.pool._probe_client_profile = "target"
        self.mod.pool._client_profiles = {"target": {"headers": {"User-Agent": "target/1"}}}
        self.mod.clear_client_headers()
        routes = [("POST", "/api/test", {"id": self.ep.id}),
                  ("GET", f"/api/endpoints/{self.ep.id}/models", {}),
                  ("POST", "/api/fetch-models", {"base_url": self.ep.base_url, "api_key": self.ep.api_key}),
                  ("POST", "/api/test-model", {"base_url": self.ep.base_url, "api_key": self.ep.api_key, "model": "other"}),
                  ("POST", "/api/test-vision", {"base_url": self.ep.base_url, "api_key": self.ep.api_key, "model": "other"})]
        for method, path, body in routes:
            with self.subTest(path=path):
                _, result, _ = self.mod.api_handler(method, path, body)
                self.assertIn("已关闭检测", result.get("error", ""), result)
        with self.assertRaises(self.mod.PoolIdentityUnavailable):
            self.mod.pool._try_endpoint(self.ep, {"messages": []}, 5)
        with self.assertRaises(self.mod.PoolIdentityUnavailable):
            self.mod.pool._credit_http_json(f"http://127.0.0.1:{self.upstream.server_port}/credits/summary")
        self.ep.in_pool = True
        self.ep.pool_groups = ["main"]
        _, pool_result, _ = self.mod.api_handler("POST", "/api/test-pool", {"message": "hi"})
        self.assertFalse(pool_result["ok"])
        self.assertIn("已关闭检测", str(pool_result))
        self.mod.pool.check_all_health()
        self.assertEqual(len(self.calls), before)
        self.assertEqual((self.ep._fail_count, self.ep._cooldown_until, self.ep._last_error), state)
        self.assertIn("identity-ok", self.request(IDENTITY))
        self.assertEqual(len(self.calls), before + 1)

    def test_disabled_probe_inside_client_context_and_form_hint(self):
        before = len(self.calls)
        self.mod.set_client_headers(IDENTITY)
        try:
            with self.assertRaises(self.mod.PoolIdentityUnavailable):
                self.mod.pool._try_endpoint(self.ep, {"messages": []}, 5, is_probe=True)
            with self.assertRaises(self.mod.PoolIdentityUnavailable):
                self.mod.pool.fetch_models(self.ep.base_url + "/new", "other", health_mode="none")
            result = self.mod.pool.test_model_latency(self.ep.base_url + "/new", "other", "m", health_mode="none")
            self.assertTrue(result["skipped"])
        finally:
            self.mod.clear_client_headers()
        self.assertEqual(len(self.calls), before)

    def test_configured_enabled_endpoint_uses_own_switch(self):
        enabled = self.mod.Endpoint(id="enabled", name="enabled", base_url=self.ep.base_url,
            api_key=self.ep.api_key, model="m2", use_proxy=False, max_retries=0, health_mode="models")
        self.mod.pool._endpoints.append(enabled)
        self.mod.pool._probe_client_profile = "target"
        self.mod.pool._client_profiles = {"target": {"headers": {"User-Agent": "target/1"}}}
        self.mod.clear_client_headers()
        try:
            before = len(self.calls)
            models = self.mod.pool.fetch_endpoint_models("enabled")
            self.assertEqual(models[0]["id"], "m")
            self.assertEqual(len(self.calls), before + 1)
            with self.assertRaises(self.mod.PoolIdentityUnavailable):
                self.mod.pool.fetch_models(enabled.base_url, enabled.api_key)
            self.assertEqual(len(self.calls), before + 1)
        finally:
            self.mod.pool._endpoints.remove(enabled)

    def test_enabled_pool_request_is_allowed(self):
        self.ep.health_mode = "chat"
        self.mod.pool._probe_client_profile = "target"
        self.mod.pool._client_profiles = {"target": {"headers": {"User-Agent": "target/1"}}}
        self.mod.clear_client_headers()
        result, error = self.mod.pool._try_endpoint(self.ep, {"messages": []}, 5)
        self.assertEqual(error, "")
        self.assertIsNotNone(result)

    def test_profile_filters_connection_tokens_and_rejects_invalid_headers(self):
        clean, dropped = self.mod._sanitize_profile_headers({"User-Agent": "target/1", "Connection": "X-Hop",
            "X-Hop": "secret", "Proxy-Authorization": "secret", "Cookie": "secret", "Anthropic-Version": "v2"})
        self.assertEqual(clean, {"User-Agent": "target/1", "Anthropic-Version": "v2"})
        self.assertEqual(len(dropped), 4)
        for headers in ({"X-Test": "v\r\nInjected: yes"}, {"User-Agent": "a", "user-agent": "b"}):
            with self.assertRaises(ValueError):
                self.mod._sanitize_profile_headers(headers)


if __name__ == "__main__":
    unittest.main()
