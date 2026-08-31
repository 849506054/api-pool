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
        name = f"api_pool_gfb_{os.getpid()}_{id(tmp_path)}"
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


class GroupFallbackReturnLockTests(unittest.TestCase):
    """A0：子组→main 组级延迟回切锁（2026-08-31）。

    语义：子组入口/耗尽 fallback 落 main 时建立锁；锁定期内该子组请求直走
    main（不重复解析死组），且每次请求把空闲窗口滑动顺延；期满后第一个请求
    回组试探。main 组不受影响。
    """

    @staticmethod
    def endpoint(module, endpoint_id, priority, model, groups):
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
        )

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.module = load_module(self.tmp_dir.name)
        gpt = self.endpoint(self.module, "gpt-a", 1, "gpt-5.6-sol", ["api-pool-gpt"])
        m1 = self.endpoint(self.module, "main-a", 1, "glm-5.3", ["main"])
        m2 = self.endpoint(self.module, "main-b", 2, "glm-5.3", ["main"])
        self.pool = self.module.APIPool([gpt, m1, m2])
        # 生产由 config 的 pool_group_defs 加载；测试直接写组实体定义
        self.pool._group_defs["api-pool-gpt"] = {"type": "dedicated", "model": "gpt-5.6-sol"}
        self.gpt, self.m1, self.m2 = gpt, m1, m2

    def test_entry_fallback_establishes_lock(self):
        # gpt 组端点全部冷却 → 入口 fallback 落 main + 建锁
        self.gpt._cooldown_until = time.time() + 600
        seen = []

        def fake_try(ep, payload, timeout, **kwargs):
            seen.append(ep.id)
            return {"choices": [{"message": {"content": "ok"}}]}, ""

        self.pool._try_endpoint = fake_try
        self.pool.chat([{"role": "user", "content": "hi"}], model="gpt-5.6-sol")
        self.assertEqual(seen, ["main-a"])
        self.assertGreater(self.pool._group_fallback_lock_until.get("api-pool-gpt", 0), time.time())

    def test_locked_group_goes_straight_to_main_and_slides_window(self):
        # 锁已存在 → 不再对死组解析/告警，直接走 main；且窗口被滑动顺延
        base = time.time() + 120
        self.pool._group_fallback_lock_until["api-pool-gpt"] = base
        seen = []

        def fake_try(ep, payload, timeout, **kwargs):
            seen.append(ep.id)
            return {"choices": [{"message": {"content": "ok"}}]}, ""

        self.pool._try_endpoint = fake_try
        self.pool.chat([{"role": "user", "content": "hi"}], model="gpt-5.6-sol")
        # 直走 main 端点，未碰 gpt 端点
        self.assertEqual(seen, ["main-a"])
        # 窗口顺延：新锁值 > 原值（原剩余 120s → 顺延到 ~300s）
        self.assertGreater(self.pool._group_fallback_lock_until["api-pool-gpt"], base)

    def test_expired_lock_returns_to_group(self):
        # 锁已过期 → 本请求回组试探，成功即回组并清除锁
        self.pool._group_fallback_lock_until["api-pool-gpt"] = time.time() - 1
        seen = []

        def fake_try(ep, payload, timeout, **kwargs):
            seen.append(ep.id)
            return {"choices": [{"message": {"content": "ok"}}]}, ""

        self.pool._try_endpoint = fake_try
        self.pool.chat([{"role": "user", "content": "hi"}], model="gpt-5.6-sol")
        self.assertEqual(seen, ["gpt-a"])
        # 成功回组 → 锁清除
        self.assertLessEqual(self.pool._group_fallback_lock_until.get("api-pool-gpt", 0), time.time())

    def test_expired_lock_retry_failure_relocks(self):
        # 锁过期后回组试探失败（耗尽路径）→ 重新锁 5 分钟
        self.pool._group_fallback_lock_until["api-pool-gpt"] = time.time() - 1
        calls = []

        def fake_try(ep, payload, timeout, **kwargs):
            calls.append(ep.id)
            if ep.id == "gpt-a":
                # 模拟 gpt 端点持续故障：连接超时
                return None, "连接/超时错误: timed out"
            return {"choices": [{"message": {"content": "ok"}}]}, ""

        self.pool._try_endpoint = fake_try
        result = self.pool.chat([{"role": "user", "content": "hi"}], model="gpt-5.6-sol")
        # 回组失败 → 耗尽 fallback 到 main 追加一轮成功
        self.assertIn("gpt-a", calls)
        self.assertIn("main-a", calls)
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        # 重新锁定
        self.assertGreater(self.pool._group_fallback_lock_until.get("api-pool-gpt", 0), time.time())

    def test_main_group_requests_never_locked(self):
        # main 请求不受任何锁影响
        self.pool._group_fallback_lock_until["main"] = time.time() + 600  # 即使误写也不生效
        seen = []

        def fake_try(ep, payload, timeout, **kwargs):
            seen.append(ep.id)
            return {"choices": [{"message": {"content": "ok"}}]}, ""

        self.pool._try_endpoint = fake_try
        self.pool.chat([{"role": "user", "content": "hi"}], model="api-pool")
        self.assertEqual(seen, ["main-a"])

    def test_group_rename_and_delete_migrate_lock(self):
        self.pool._group_fallback_lock_until["api-pool-gpt"] = time.time() + 600
        ok, _ = self.pool.update_group("api-pool-gpt", {"name": "api-pool-gpt2"})
        self.assertTrue(ok)
        self.assertNotIn("api-pool-gpt", self.pool._group_fallback_lock_until)
        self.assertGreater(self.pool._group_fallback_lock_until.get("api-pool-gpt2", 0), 0)

        ok, _ = self.pool.delete_group("api-pool-gpt2")
        self.assertTrue(ok)
        self.assertNotIn("api-pool-gpt2", self.pool._group_fallback_lock_until)


if __name__ == "__main__":
    unittest.main()
