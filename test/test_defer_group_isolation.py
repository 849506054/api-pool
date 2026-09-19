"""延迟回切按组隔离（2026-09-19）。

回归的缺陷：`_defer_until` 是端点级共享字段，`_background_probe` 的逐组循环里
「已开启缓存保护」分支写入 now+300，紧接着另一组的 else 分支又把它清零，
于是既没有延迟回切、也没有回迁（`_reconcile_deferred` 因 defer==0 直接跳过）。

契约：
- 延迟回切按 (端点, 组) 存储，组之间互不覆盖。
- 判定只看本组当前端点：main 不受子组指针影响，子组也不替 main 决定。
- 未延迟时按回迁规则处理；延迟到期释放后回迁到恢复端点。
"""
import importlib.util
import os
import sys
import tempfile
import time
import unittest

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_defer_{os.getpid()}_{id(tmp_path)}"
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


class DeferGroupIsolationTests(unittest.TestCase):
    @staticmethod
    def endpoint(module, endpoint_id, priority, model, groups, deferrable=True):
        return module.Endpoint(
            id=endpoint_id,
            name=endpoint_id,
            base_url="http://127.0.0.1:1",
            api_key="test",
            model=model,
            priority=priority,
            in_pool=True,
            use_proxy=False,
            pool_groups=groups,
            deferrable=deferrable,
        )

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.module = load_module(self.tmp_dir.name)
        # X 同时属于 main 和 pool-ds；M 是 main 的当前端点；D 是 pool-ds 的备选
        self.x = self.endpoint(self.module, "x", 2, "deepseek-v4.1-flash", ["main", "pool-ds"])
        self.m = self.endpoint(self.module, "m", 3, "deepseek-v4.1-flash", ["main"])
        self.d = self.endpoint(self.module, "d", 2, "deepseek-v4.1-flash", ["pool-ds"])
        self.pool = self.module.APIPool([self.x, self.m, self.d])
        self.pool._probe_endpoint = lambda ep: (True, "")
        self.pool._last_pool_activity = time.time()

    # ── 核心回归：一组写入不被另一组覆盖 ──────────────────────────────
    def test_protected_group_keeps_defer_while_other_group_clears(self):
        # main 的当前是 M（开启缓存保护）→ main 需要延迟回切 X
        # pool-ds 的当前就是 X 自己 → pool-ds 无需保护
        self.pool._set_current("main", self.m.id)
        self.pool._set_current("pool-ds", self.x.id)
        self.pool._set_manual("pool-ds", self.x.id)
        self.pool._background_probe(self.x, {"main": self.m.id, "pool-ds": self.x.id})

        self.assertIn("main", self.x._defer_until_by_group,
                      "main 组的延迟回切被另一组的循环覆盖清零")
        self.assertGreater(self.x._defer_until_by_group["main"], time.time() + 250)
        self.assertNotIn("pool-ds", self.x._defer_until_by_group,
                         "pool-ds 的当前端点就是自己，不应产生延迟回切")
        # main 指针保持不动（延迟回切期间不主动回迁）
        self.assertEqual(self.pool._current_endpoint_by_group.get("main"), self.m.id)

    def test_sub_group_pointer_does_not_defer_for_main(self):
        # 反向：main 的当前就是 X 自己，pool-ds 的当前是 D → 只有 pool-ds 产生延迟回切
        self.pool._set_current("main", self.x.id)
        self.pool._set_current("pool-ds", self.d.id)
        self.pool._background_probe(self.x, {"main": self.x.id, "pool-ds": self.d.id})

        self.assertNotIn("main", self.x._defer_until_by_group)
        self.assertIn("pool-ds", self.x._defer_until_by_group)
        # main 指针不动（它本来就是 X），pool-ds 指针也不动（缓存保护中）
        self.assertEqual(self.pool._current_endpoint_by_group.get("main"), self.x.id)
        self.assertEqual(self.pool._current_endpoint_by_group.get("pool-ds"), self.d.id)

    # ── 延迟回切的生命周期 ────────────────────────────────────────────
    def _defer_on_main(self):
        self.pool._set_current("main", self.m.id)
        self.pool._set_current("pool-ds", self.x.id)
        self.pool._set_manual("pool-ds", self.x.id)
        self.pool._background_probe(self.x, {"main": self.m.id, "pool-ds": self.x.id})

    def test_active_pool_rolls_defer_forward(self):
        self._defer_on_main()
        self.pool._reconcile_deferred()
        self.assertIn("main", self.x._defer_until_by_group)
        self.assertGreater(self.x._defer_until_by_group["main"], time.time() + 290)

    def test_idle_pool_releases_and_fails_back(self):
        self._defer_on_main()
        self.pool._last_pool_activity = time.time() - 400  # 池空闲超过窗口
        self.pool._reconcile_deferred()
        self.assertEqual(self.x._defer_until_by_group, {},
                         "池空闲后延迟回切未释放")
        self.assertEqual(self.pool._current_endpoint_by_group.get("main"), self.x.id,
                         "释放后未回迁到恢复端点")

    def test_released_group_skipped_when_manual(self):
        self._defer_on_main()
        self.pool._set_manual("main", self.m.id)  # 用户手动指定 main 端点
        self.pool._last_pool_activity = time.time() - 400
        self.pool._reconcile_deferred()
        self.assertEqual(self.pool._current_endpoint_by_group.get("main"), self.m.id,
                         "手动端点不应被自动回迁覆盖")

    # ── 手动断言只影响本组 ────────────────────────────────────────────
    def test_manual_switch_clears_only_target_group(self):
        self._defer_on_main()
        self.x._defer_until_by_group["pool-ds"] = time.time() + 300
        self.pool.switch_to_endpoint(self.x.id, group="main")
        self.assertNotIn("main", self.x._defer_until_by_group)
        self.assertIn("pool-ds", self.x._defer_until_by_group,
                      "手动切换不该清掉其他组的缓存保护")

    def test_clear_error_clears_all_groups(self):
        self.x._defer_until_by_group["main"] = time.time() + 300
        self.x._defer_until_by_group["pool-ds"] = time.time() + 300
        self.pool.clear_error(self.x.id)
        self.assertEqual(self.x._defer_until_by_group, {})

    # ── 序列化口径 ────────────────────────────────────────────────────
    def test_serialization_views(self):
        now = time.time()
        self.x._defer_until_by_group["main"] = now + 300
        self.x._defer_until_by_group["pool-ds"] = now + 120
        self.assertTrue(self.pool._is_deferred(self.x))
        self.assertTrue(self.pool._is_deferred(self.x, "main"))
        self.assertFalse(self.pool._is_deferred(self.x, "pool-bg"))
        self.assertIn(self.pool._defer_remaining_max(self.x), (299, 300))
        view = self.pool._defer_by_group_view(self.x)
        self.assertEqual(sorted(view), ["main", "pool-ds"])
        self.assertIn(view["pool-ds"], (119, 120))


if __name__ == "__main__":
    unittest.main()
