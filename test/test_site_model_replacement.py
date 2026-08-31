"""Targeted tests for endpoint sites, model discovery, and group model replacement."""

import importlib.util
import json
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
        name = f"api_pool_site_models_test_{os.getpid()}_{id(tmp_path)}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        module.CONFIG_FILE = os.path.join(tmp_path, "api_config.json")
        module.RUNTIME_STATE_FILE = os.path.join(tmp_path, "api_runtime_state.json")
        return module
    finally:
        os.chdir(previous_cwd)


class SiteModelReplacementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.module = load_module(self.tmp.name)
        self.pool = self.module.APIPool()
        self.module.pool = self.pool

    def endpoint(self, endpoint_id, model, *, site="site-a", name=None, groups=None, **kwargs):
        defaults = dict(
            id=endpoint_id,
            name=name or endpoint_id,
            site_name=site,
            base_url="https://site-a.example/v1",
            api_key="secret-a",
            model=model,
            priority=kwargs.pop("priority", 1),
            priority_by_group=kwargs.pop("priority_by_group", {}),
            timeout=17,
            enabled=True,
            in_pool=True,
            use_proxy=False,
            protocol="openai",
            default_headers={"X-Default": "one"},
            extra_headers={"X-Extra": "two"},
            pool_groups=groups or ["main"],
        )
        defaults.update(kwargs)
        ep = self.module.Endpoint(**defaults)
        self.pool.add_endpoint(ep)
        return ep

    def test_site_name_is_backward_compatible_and_serialized(self):
        legacy = self.module.Endpoint(id="legacy", name="legacy")
        self.assertEqual(legacy.site_name, "")
        self.pool.add_endpoint(legacy)
        listed = next(ep for ep in self.pool.list_endpoints() if ep["id"] == "legacy")
        self.assertEqual(listed["site_name"], "")
        self.module._sync_to_config()
        with open(self.module.CONFIG_FILE, encoding="utf-8") as handle:
            saved = json.load(handle)["api_endpoints"]
        self.assertEqual(saved[0]["site_name"], "")

    def test_endpoint_models_uses_all_of_that_endpoints_connection_fields(self):
        ep = self.endpoint("source", "model-a")
        expected = [{"id": "model-b"}]
        with mock.patch.object(self.pool, "fetch_models", return_value=expected) as fetch:
            status, response, _ = self.module.api_handler(
                "GET", "/api/endpoints/source/models", None
            )
        self.assertEqual((status, response["models"], response["count"]), (200, expected, 1))
        fetch.assert_called_once_with(
            base_url=ep.base_url,
            api_key=ep.api_key,
            timeout=17,
            use_proxy=ep.use_proxy,
            protocol=ep.protocol,
            default_headers=ep.default_headers,
            extra_headers=ep.extra_headers,
        )

    def test_endpoint_models_unknown_endpoint_is_404(self):
        status, response, _ = self.module.api_handler(
            "GET", "/api/endpoints/missing/models", None
        )
        self.assertEqual(status, 404)
        self.assertIn("端点不存在", response["error"])

    def test_existing_match_replaces_only_current_group_and_inherits_position_and_pointers(self):
        source = self.endpoint(
            "a-old", "model-a", groups=["main", "bg"],
            priority=2, priority_by_group={"main": 2, "bg": 1},
        )
        other = self.endpoint(
            "other", "other", groups=["bg"], priority=1,
            priority_by_group={"bg": 2},
        )
        replacement = self.endpoint(
            "a-new", "model-b", groups=["archive"], in_pool=True,
            priority=9, priority_by_group={"archive": 1},
        )
        self.pool._derive_group_defs()
        self.pool._set_current("bg", source.id)
        self.pool._set_manual("bg", source.id)
        self.pool._set_persisted("bg", source.id)

        result, created, moved = self.pool.replace_group_model("bg", source.id, "model-b")

        self.assertIs(result, replacement)
        self.assertFalse(created)
        self.assertTrue(moved)
        self.assertEqual(source.pool_groups, ["main"])
        self.assertEqual(replacement.pool_groups, ["archive", "bg"])
        self.assertEqual(self.pool._ep_priority(replacement, "bg"), 1)
        self.assertEqual(self.pool._ep_priority(other, "bg"), 2)
        self.assertEqual(self.pool._get_current("bg"), replacement.id)
        self.assertEqual(self.pool._get_manual("bg"), replacement.id)
        self.assertEqual(self.pool._get_persisted("bg"), replacement.id)

    def test_matching_prefers_connection_then_enabled_cooldown_priority_and_config_order(self):
        source = self.endpoint("source", "model-a")
        wrong_connection = self.endpoint(
            "wrong", "model-b", base_url="https://other.example/v1", priority=1
        )
        disabled = self.endpoint("disabled", "model-b", enabled=False, priority=1)
        cooling = self.endpoint("cooling", "model-b", priority=1)
        cooling._cooldown_until = self.module.time.time() + 60
        preferred = self.endpoint("preferred", "model-b", priority=2)
        later = self.endpoint("later", "model-b", priority=2)
        # add_endpoint 连续化优先级；显式构造同组同优先级以验证配置顺序终裁。
        wrong_connection.priority_by_group["main"] = 1
        disabled.priority_by_group["main"] = 1
        cooling.priority_by_group["main"] = 1
        preferred.priority_by_group["main"] = 2
        later.priority_by_group["main"] = 2
        # _endpoints 是持久化配置顺序；固定顺序以验证最终 tie-break。
        self.pool._endpoints = [source, wrong_connection, disabled, cooling, preferred, later]

        result, created, _ = self.pool.replace_group_model("main", source.id, "model-b")

        self.assertFalse(created)
        self.assertIs(result, preferred)
        self.assertIsNot(result, wrong_connection)
        self.assertIsNot(result, disabled)
        self.assertIsNot(result, cooling)
        self.assertIsNot(result, later)

    def test_clone_joins_only_selected_group_preserves_source_other_groups_and_has_blank_runtime(self):
        source = self.endpoint(
            "source", "model-a", name="site-a-model-a", groups=["main", "bg"],
            priority_by_group={"main": 2, "bg": 1},
        )
        self.endpoint("collision", "unrelated", name="site-a-model-b", groups=["archive"])
        source._fail_count = 7
        source._last_error = "old failure"
        source._cooldown_until = self.module.time.time() + 100
        source._total_calls = 9

        clone, created, _ = self.pool.replace_group_model("bg", source.id, "model-b")

        self.assertTrue(created)
        self.assertNotEqual(clone.id, source.id)
        self.assertEqual(clone.name, "site-a-model-b-2")
        self.assertEqual(clone.model, "model-b")
        self.assertEqual(clone.site_name, source.site_name)
        self.assertEqual(clone.base_url, source.base_url)
        self.assertEqual(clone.api_key, source.api_key)
        self.assertEqual(clone.pool_groups, ["bg"])
        self.assertTrue(clone.in_pool)
        self.assertEqual(source.pool_groups, ["main"])
        self.assertEqual(clone._fail_count, 0)
        self.assertEqual(clone._last_error, "")
        self.assertEqual(clone._cooldown_until, 0)
        self.assertEqual(clone._total_calls, 0)

    def test_empty_site_name_is_rejected_without_mutation(self):
        source = self.endpoint("legacy", "model-a", site="")
        before = [(ep.id, ep.model, list(ep.pool_groups)) for ep in self.pool._endpoints]
        with self.assertRaisesRegex(ValueError, "site_name"):
            self.pool.replace_group_model("main", source.id, "model-b")
        after = [(ep.id, ep.model, list(ep.pool_groups)) for ep in self.pool._endpoints]
        self.assertEqual(after, before)

    def test_dedicated_group_rejects_incompatible_model_without_mutation(self):
        source = self.endpoint("source", "model-a", groups=["dedicated"])
        self.pool._group_defs["dedicated"] = {"type": "dedicated", "model": "model-a"}
        before = [(ep.id, ep.model, list(ep.pool_groups)) for ep in self.pool._endpoints]
        with self.assertRaisesRegex(ValueError, "仅允许模型"):
            self.pool.replace_group_model("dedicated", source.id, "model-b")
        self.assertEqual(
            [(ep.id, ep.model, list(ep.pool_groups)) for ep in self.pool._endpoints], before
        )

    def test_api_persists_config_and_moved_runtime_pointer(self):
        source = self.endpoint("source", "model-a")
        self.pool._set_current("main", source.id)
        self.pool._set_manual("main", source.id)
        self.pool._set_persisted("main", source.id)

        status, response, _ = self.module.api_handler(
            "POST", "/api/groups/main/replace-model",
            {"endpoint_id": source.id, "model": "model-b"},
        )

        self.assertEqual(status, 200)
        replacement_id = response["endpoint"]["id"]
        self.assertTrue(response["created"])
        with open(self.module.CONFIG_FILE, encoding="utf-8") as handle:
            saved = json.load(handle)["api_endpoints"]
        saved_replacement = next(ep for ep in saved if ep["id"] == replacement_id)
        self.assertEqual(saved_replacement["pool_groups"], ["main"])
        with open(self.module.RUNTIME_STATE_FILE, encoding="utf-8") as handle:
            runtime = json.load(handle)
        self.assertEqual(runtime["groups"]["main"], replacement_id)

    def test_frontend_select_model_route_precedes_generic_pool_route(self):
        source = self.endpoint("source", "model-a")
        self.pool._set_current("main", source.id)

        status, response, _ = self.module.api_handler(
            "POST", "/api/pool/source/select-model",
            {"group": "main", "model": "model-b"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["action"], "cloned")
        self.assertTrue(response["endpoint_id"])
        self.assertEqual(response["endpoint_name"], "site-a-model-b")
        self.assertNotEqual(response["endpoint_id"], source.id)

    def test_select_model_api_logs_success_and_validation_failure(self):
        source = self.endpoint("source", "model-a")
        self.pool._set_current("main", source.id)
        logs = []

        with mock.patch.object(
            self.module, "sys_log", side_effect=lambda message, level="INFO": logs.append((message, level))
        ):
            status, response, _ = self.module.api_handler(
                "POST", "/api/pool/source/select-model",
                {"group": "main", "model": "model-b"},
            )
            missing_status, _, _ = self.module.api_handler(
                "POST", "/api/pool/source/select-model",
                {"group": "main", "model": ""},
            )

        self.assertEqual(status, 200)
        self.assertEqual(response["action"], "cloned")
        expected_success_log = (
            "模型切换成功: 组 'main' 源端点 source → 模型 'model-b' → 端点 'site-a-model-b'（克隆）"
        )
        self.assertIn((expected_success_log, "INFO"), logs)
        self.assertEqual(missing_status, 400)
        self.assertIn(
            ("模型切换失败(400): 参数缺失 group='main' model='' source='source'", "ERROR"),
            logs,
        )

    def test_frontend_prefetches_pool_models_before_native_select_opens(self):
        index_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "index.html")
        with open(index_path, encoding="utf-8") as handle:
            html = handle.read()

        self.assertIn("if (e && e.in_pool) endpointModels(e.id).catch(() => {});", html)
        self.assertIn(
            "onmousedown=\"event.stopPropagation();loadPoolModelOptions(this,'${escAttr(ep.id)}')\"",
            html,
        )
        self.assertIn("console.error('[select-model 失败]'", html)
        self.assertIn("切换失败(${group}→${model})", html)


if __name__ == "__main__":
    unittest.main()
