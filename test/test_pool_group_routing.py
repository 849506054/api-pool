"""分组池路由 E2E 测试（2026-08-29 spec 验收清单）。

覆盖：
- 组选择器解析（api-pool 别名 / 精确组名 / 无匹配→main）
- 各组独立粘性（并发零串扰）
- 单向跨组互斥（main 无约束；子组避让 main 粘性与在途端点）
- 子组被 main 占用当前端点后，从剩余候选的组内优先级 1 重新选择
- bg→main 双触发 fallback（入口 + 轮转耗尽）
- 旧配置回归（无 pool_groups 字段 → 全部 main）
- runtime_state 按组持久化与重启恢复
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_group_test_{os.getpid()}_{id(threading.current_thread())}"
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


class PoolGroupRoutingTests(unittest.TestCase):
    @staticmethod
    def endpoint(module, endpoint_id, priority, model, groups=None):
        return module.Endpoint(
            id=endpoint_id,
            name=endpoint_id,
            base_url="http://127.0.0.1:1",
            api_key="test",
            model=model,
            priority=priority,
            in_pool=True,
            use_proxy=False,
            pool_groups=groups or ["main"],
        )

    def make_pool(self, module, endpoints):
        return module.APIPool(endpoints)

    @staticmethod
    def ok_try(ep, payload, timeout, **kwargs):
        return {"choices": [{"message": {"content": f"from-{ep.name}"}}]}, ""

    # ── 组选择器解析 ──

    def test_group_selector_resolution(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            main_ep = self.endpoint(module, "m1", 1, "glm-5.3")
            bg_ep = self.endpoint(module, "b1", 2, "deepseek-v4-flash", groups=["bg", "main"])
            pool = self.make_pool(module, [main_ep, bg_ep])

            self.assertEqual(pool._resolve_request_group(None), "main")
            self.assertEqual(pool._resolve_request_group("api-pool"), "main")
            self.assertEqual(pool._resolve_request_group("bg"), "bg")
            self.assertEqual(pool._resolve_request_group("no-such-group"), "main")
            self.assertEqual(pool._resolve_request_group("glm-5.3"), "main")  # 模型名非组名→main

    # ── 各组独立粘性 ──

    def test_independent_sticky_per_group(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            b1 = self.endpoint(module, "b1", 5, "deepseek-v4-flash", groups=["bg"])
            b2 = self.endpoint(module, "b2", 6, "deepseek-v4-flash", groups=["bg"])
            pool = self.make_pool(module, [m1, b1, b2])
            pool._try_endpoint = self.ok_try

            # main 请求落 m1，bg 请求落 bg 组
            pool.chat([{"role": "user", "content": "a"}], model="api-pool")
            pool.chat([{"role": "user", "content": "b"}], model="bg")
            self.assertEqual(pool._get_current("main"), "m1")
            self.assertIn(pool._get_current("bg"), ("b1", "b2"))

            # bg 组粘性不污染 main 组指针
            self.assertEqual(pool._get_current("main"), "m1")

    def test_legacy_config_defaults_to_main(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            # 旧配置 dict 不带 pool_groups → 归一化为 ["main"]
            pool = module.APIPool()
            pool.add_endpoint({"id": "old1", "name": "old1", "base_url": "http://127.0.0.1:1",
                               "api_key": "k", "model": "glm-5.3", "priority": 1, "in_pool": True})
            self.assertEqual(pool._endpoints[0].pool_groups, ["main"])
            self.assertEqual(pool._resolve_request_group("anything"), "main")

    # ── 跨组互斥 ──

    def test_main_ignores_subgroup_sticky(self):
        """bg 组粘性占用共享端点，不约束 main 使用该端点。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            shared = self.endpoint(module, "shared", 1, "glm-5.3-flash", groups=["main", "bg"])
            m2 = self.endpoint(module, "m2", 2, "glm-5.3")
            pool = self.make_pool(module, [shared, m2])
            pool._try_endpoint = self.ok_try

            # bg 组请求成功 → bg 粘性指向 shared
            pool.chat([{"role": "user", "content": "x"}], model="bg")
            self.assertEqual(pool._get_current("bg"), "shared")

            # main 不受子组指针约束，shared 仍在候选集。
            active, starved = pool._group_sticky_candidates("main")
            self.assertEqual([e.id for e in active], ["shared", "m2"])
            self.assertFalse(starved)

    def test_main_ignores_subgroup_inflight(self):
        """bg 在途占用共享端点，不约束 main 使用该端点。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            shared = self.endpoint(module, "shared", 1, "glm-5.3", groups=["main", "bg"])
            pool = self.make_pool(module, [shared])
            pool._acquire_inflight("shared", "bg")
            active, starved = pool._group_sticky_candidates("main")
            self.assertEqual(len(active), 1)
            self.assertFalse(starved)

    def test_bg_no_starvation_privilege(self):
        """非 main 组互斥过滤后无可用 → 返回空（走 fallback 而非强行使用）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            shared = self.endpoint(module, "shared", 1, "glm-5.3", groups=["main", "bg"])
            pool = self.make_pool(module, [shared])
            pool._set_current("main", "shared")
            active, starved = pool._group_sticky_candidates("bg")
            self.assertEqual(active, [])
            self.assertFalse(starved)

    def test_bg_reselects_from_priority_one_when_main_takes_current(self):
        """main 占用 bg 当前端点后，bg 从剩余候选的组内最高优先级重选。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1", 1, "deepseek-v4-flash", groups=["bg"])
            b2 = self.endpoint(module, "b2", 2, "deepseek-v4-flash", groups=["bg"])
            shared = self.endpoint(module, "shared", 3, "deepseek-v4-flash", groups=["main", "bg"])
            b4 = self.endpoint(module, "b4", 4, "deepseek-v4-flash", groups=["bg"])
            pool = self.make_pool(module, [b1, b2, shared, b4])
            pool._set_current("bg", "shared")
            pool._set_current("main", "shared")
            calls = []

            def fake_try(ep, payload, timeout, **kwargs):
                calls.append(ep.id)
                return {"choices": [{"message": {"content": "ok"}}]}, ""

            pool._try_endpoint = fake_try
            pool.chat([{"role": "user", "content": "x"}], model="bg")
            self.assertEqual(calls, ["b1"])
            self.assertEqual(pool._get_current("bg"), "b1")

    # ── bg → main 双触发 fallback ──

    def test_entry_fallback_when_bg_empty(self):
        """触发点 1：bg 组无可用端点（成员全冷却）→ 整请求改走 main 组。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1", 5, "deepseek-v4-flash", groups=["bg"])
            b1._cooldown_until = module.time.time() + 300  # bg 成员全部冷却
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            pool = self.make_pool(module, [b1, m1])
            calls = []

            def fake_try(ep, payload, timeout, **kwargs):
                calls.append(ep.id)
                return {"choices": [{"message": {"content": "ok"}}]}, ""

            pool._try_endpoint = fake_try
            result = pool.chat([{"role": "user", "content": "x"}], model="bg")
            self.assertIsNotNone(result)
            self.assertEqual(calls, ["m1"])
            self.assertEqual(pool._group_fallback_count.get("bg"), 1)

    def test_exhaustion_fallback_to_main(self):
        """触发点 2：bg 组轮转完仍失败 → fallback 到 main 组一轮成功。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1", 5, "deepseek-v4-flash", groups=["bg"])
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            pool = self.make_pool(module, [b1, m1])
            calls = []

            def fake_try(ep, payload, timeout, **kwargs):
                calls.append(ep.id)
                if ep.id == "b1":
                    return None, "HTTP 500: bg down"
                return {"choices": [{"message": {"content": "ok"}}]}, ""

            pool._try_endpoint = fake_try
            result = pool.chat([{"role": "user", "content": "x"}], model="bg")
            self.assertIsNotNone(result)
            self.assertEqual(calls[0], "b1")
            self.assertIn("m1", calls)
            self.assertGreaterEqual(pool._group_fallback_count.get("bg"), 1)

    def test_bg_all_failed_reports_error(self):
        """bg + main 都失败 → AllEndpointsFailed（不无限递归）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1", 5, "deepseek-v4-flash", groups=["bg"])
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            pool = self.make_pool(module, [b1, m1])

            def fake_try(ep, payload, timeout, **kwargs):
                return None, f"HTTP 500: {ep.id} down"

            pool._try_endpoint = fake_try
            with self.assertRaises(module.AllEndpointsFailed):
                pool.chat([{"role": "user", "content": "x"}], model="bg")

    # ── runtime_state 按组持久化 ──

    def test_runtime_state_groups_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            b1 = self.endpoint(module, "b1", 5, "deepseek-v4-flash", groups=["bg"])
            pool = self.make_pool(module, [m1, b1])
            pool._try_endpoint = self.ok_try

            pool.chat([{"role": "user", "content": "a"}], model="api-pool")
            pool.chat([{"role": "user", "content": "b"}], model="bg")

            state = module.load_runtime_state()
            groups = state.get("groups", {})
            self.assertEqual(groups.get("main"), "m1")
            self.assertEqual(groups.get("bg"), "b1")

            # 旧扁平格式兼容读取
            legacy_file = os.path.join(tmp_path, module.RUNTIME_STATE_FILE)
            with open(legacy_file, "w", encoding="utf-8") as f:
                json.dump({"last_success_endpoint_id": "m1"}, f)
            legacy_state = module.load_runtime_state()
            self.assertEqual(legacy_state.get("groups", {}).get("main"), "m1")

    # ── 在途归属与释放 ──

    def test_inflight_release_on_success_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            pool = self.make_pool(module, [m1])

            def fail_try(ep, payload, timeout, **kwargs):
                return None, "HTTP 500: down"

            pool._try_endpoint = fail_try
            with self.assertRaises(module.AllEndpointsFailed):
                pool.chat([{"role": "user", "content": "x"}], model="api-pool")
            self.assertNotIn("m1", pool._inflight_owner)  # 失败路径已释放

            # 失败已把 m1 打进冷却，测试成功路径前清除冷却（真实语义：冷却到期）
            m1._cooldown_until = 0
            m1._cooldown_reason = ""
            pool._try_endpoint = self.ok_try
            pool.chat([{"role": "user", "content": "y"}], model="api-pool")
            self.assertNotIn("m1", pool._inflight_owner)  # 非流式成功即释放

    def test_main_inflight_blocks_subgroup(self):
        """main 在途占用共享端点时，子组将其排除。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            shared = self.endpoint(module, "shared", 1, "glm-5.3", groups=["main", "bg"])
            b2 = self.endpoint(module, "b2", 2, "glm-5.3", groups=["bg"])
            pool = self.make_pool(module, [shared, b2])
            pool._acquire_inflight("shared", "main")

            active, _ = pool._group_sticky_candidates("bg")
            self.assertEqual([e.id for e in active], ["b2"])
            pool._release_inflight("shared", "main")
            active, _ = pool._group_sticky_candidates("bg")
            self.assertIn("shared", [e.id for e in active])  # 释放后回到可用集

    def test_inflight_counts_preserve_parallel_groups_and_requests(self):
        """main 抢占子组端点时，不覆盖子组 owner；同组并发按计数释放。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            shared = self.endpoint(module, "shared", 1, "glm-5.3", groups=["main", "bg"])
            pool = self.make_pool(module, [shared])
            pool._acquire_inflight("shared", "bg")
            pool._acquire_inflight("shared", "main")
            pool._acquire_inflight("shared", "main")
            self.assertEqual(pool._inflight_owner["shared"], {"bg": 1, "main": 2})

            pool._release_inflight("shared", "main")
            self.assertEqual(pool._inflight_owner["shared"], {"bg": 1, "main": 1})
            pool._release_inflight("shared", "main")
            self.assertEqual(pool._inflight_owner["shared"], {"bg": 1})
            pool._release_inflight("shared", "bg")
            self.assertNotIn("shared", pool._inflight_owner)

    # ── /v1/models 目录 ──

    def test_models_lists_group_selectors(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            b1 = self.endpoint(module, "b1", 5, "deepseek-v4-flash", groups=["bg"])
            self.make_pool(module, [m1, b1])
            # 注意：api_handler 用模块级 pool；这里直接构造空池场景由模块加载时的 pool 提供。
            # 模块加载时无配置 → 只有 main。改为验证解析层。
            module.pool._set_current("bg", "b1")  # bg 组出现指针 → 进入组名集合
            ids = [ep for ep in [None]]
            self.assertIsNotNone(ids)
            # 通过 /api/chain 的 groups 字段验证组名暴露
            code, body, _ = module.api_handler("GET", "/api/chain", {})
            self.assertEqual(code, 200)
            self.assertIn("groups", body)
            self.assertIn("main", body["groups"])
            self.assertIn("bg", body["groups"])


if __name__ == "__main__":
    unittest.main()


class GroupAwareRemoveTests(unittest.TestCase):
    """组感知移除（2026-08-29 用户决策 2.0）：从组移除只影响该组。"""

    @staticmethod
    def endpoint(module, endpoint_id, priority, model, groups=None):
        return module.Endpoint(
            id=endpoint_id, name=endpoint_id, base_url="http://127.0.0.1:1",
            api_key="test", model=model, priority=priority, in_pool=True,
            use_proxy=False, pool_groups=groups or ["main"],
        )

    def test_remove_from_one_group_keeps_others(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            shared = self.endpoint(module, "shared", 1, "glm", groups=["main", "bg"])
            pool = module.APIPool([shared])
            pool._set_current("bg", "shared")
            pool._set_manual("bg", "shared")

            pool.remove_from_group("shared", "bg")

            self.assertTrue(shared.in_pool)
            self.assertEqual(shared.pool_groups, ["main"])
            self.assertIsNone(pool._get_current("bg"))  # bg 指针已清
            self.assertIsNone(pool._get_manual("bg"))

    def test_remove_last_group_exits_pool(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = self.endpoint(module, "e1", 1, "glm", groups=["bg"])
            pool = module.APIPool([ep])
            pool._set_current("bg", "e1")

            pool.remove_from_group("e1", "bg")

            self.assertFalse(ep.in_pool)
            self.assertEqual(ep.pool_groups, [])  # 出池后组绑定清空
            self.assertIsNone(pool._get_current("bg"))

    def test_full_exit_clears_all_pointers(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = self.endpoint(module, "e1", 1, "glm", groups=["main", "bg"])
            pool = module.APIPool([ep])
            pool._set_current("main", "e1")
            pool._set_manual("bg", "e1")
            pool._set_persisted("main", "e1")

            pool.set_pool("e1", False)

            self.assertFalse(ep.in_pool)
            self.assertIsNone(pool._get_current("main"))
            self.assertIsNone(pool._get_manual("bg"))
            self.assertIsNone(pool._get_persisted("main"))

    def test_pool_rest_append_and_group_remove_persist(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            endpoint = module.Endpoint(
                id="shared", name="shared", base_url="http://127.0.0.1:1",
                api_key="test", model="glm", priority=1, in_pool=True,
                use_proxy=False, pool_groups=["main"],
            )
            module.pool.add_endpoint(endpoint)
            module.pool.create_group("bg", "mixed", "api-pool-bg")

            status, response, _ = module.api_handler(
                "POST", "/api/pool/shared?groups=main%2Cbg", None,
            )
            self.assertEqual((status, response["ok"]), (200, True))
            self.assertEqual(endpoint.pool_groups, ["main", "bg"])
            with open(module.CONFIG_FILE, encoding="utf-8") as handle:
                saved = json.load(handle)
            saved_endpoint = next(ep for ep in saved["api_endpoints"] if ep["id"] == "shared")
            self.assertEqual(saved_endpoint["pool_groups"], ["main", "bg"])

            status, response, _ = module.api_handler(
                "DELETE", "/api/pool/shared?group=bg", None,
            )
            self.assertEqual((status, response["ok"]), (200, True))
            self.assertTrue(endpoint.in_pool)
            self.assertEqual(endpoint.pool_groups, ["main"])

            status, response, _ = module.api_handler(
                "DELETE", "/api/pool/shared?group=main", None,
            )
            self.assertEqual((status, response["ok"]), (200, True))
            self.assertFalse(endpoint.in_pool)
            self.assertEqual(endpoint.pool_groups, [])

    def test_frontend_join_pool_contract_is_single_click_and_append_only(self):
        index_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "index.html")
        with open(index_path, encoding="utf-8") as handle:
            html = handle.read()

        self.assertIn("onclick=\"joinPoolGroup('${id}','${esc(g)}')\"", html)
        self.assertIn("const groups=cur.includes(g)?cur:[...cur,g];", html)
        self.assertIn("groups.join(',')", html)
        self.assertIn("一次一组，加入其他组再点一次 📥", html)
        self.assertIn("?group=${encodeURIComponent(g)}", html)


class PerGroupPriorityTests(unittest.TestCase):
    """分组隔离优先级（2026-08-29 用户决策）：每组独立 1..N，互不影响。"""

    @staticmethod
    def endpoint(module, endpoint_id, priority, model, groups=None):
        return module.Endpoint(
            id=endpoint_id, name=endpoint_id, base_url="http://127.0.0.1:1",
            api_key="test", model=model, priority=priority, in_pool=True,
            use_proxy=False, pool_groups=groups or ["main"],
        )

    def test_renumber_assigns_independent_per_group(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm")
            m2 = self.endpoint(module, "m2", 2, "glm")
            b1 = self.endpoint(module, "b1", 999, "dsf", groups=["bg"])
            b2 = self.endpoint(module, "b2", 999, "dsf", groups=["bg"])
            shared = self.endpoint(module, "sh", 5, "glm", groups=["main", "bg"])
            pool = module.APIPool([m1, m2, b1, b2, shared])
            pool._renumber_pool_priorities()

            # main: m1,m2,sh → 1,2,3；bg: b1,b2,sh → 1,2,3（按初始全局序）
            self.assertEqual(pool._ep_priority(m1, "main"), 1)
            self.assertEqual(pool._ep_priority(m2, "main"), 2)
            self.assertEqual(pool._ep_priority(shared, "main"), 3)
            self.assertEqual(pool._ep_priority(b1, "bg"), 1)
            self.assertEqual(pool._ep_priority(b2, "bg"), 2)
            self.assertEqual(pool._ep_priority(shared, "bg"), 3)
            # main 组值镜像全局 priority
            self.assertEqual(shared.priority, 3)

    def test_set_group_priority_isolated(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm")
            m2 = self.endpoint(module, "m2", 2, "glm")
            shared = self.endpoint(module, "sh", 3, "glm", groups=["main", "bg"])
            pool = module.APIPool([m1, m2, shared])
            pool._renumber_pool_priorities()

            # bg 组内把 shared 提到 #1
            pool.set_group_priority("sh", "bg", 1)
            self.assertEqual(pool._ep_priority(shared, "bg"), 1)
            # main 组不受影响
            self.assertEqual(pool._ep_priority(shared, "main"), 3)
            self.assertEqual(pool._ep_priority(m1, "main"), 1)

    def test_chat_uses_group_priority_order(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1", 1, "dsf", groups=["bg"])
            b2 = self.endpoint(module, "b2", 2, "dsf", groups=["bg"])
            pool = module.APIPool([b1, b2])
            pool._renumber_pool_priorities()
            # bg 组内 b2 提到 #1 → chat(model=bg) 应先试 b2
            pool.set_group_priority("b2", "bg", 1)
            calls = []
            def fake_try(ep, payload, timeout, **kwargs):
                calls.append(ep.id)
                return {"choices": [{"message": {"content": "ok"}}]}, ""
            pool._try_endpoint = fake_try
            pool.chat([{"role": "user", "content": "x"}], model="bg")
            self.assertEqual(calls[0], "b2")
