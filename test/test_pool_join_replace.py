"""入池组 = 本次选中的组（2026-11-05）。

契约：点哪个组就只进哪个组，不再把旧组一起带上；旧组的 per-group 状态
（优先级/停用/延迟回切）随退出一起作废；显式传空组名列表按 400 拒绝，
不静默落回 main。
"""
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
        name = f"api_pool_join_{os.getpid()}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class PoolJoinReplaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.module = load_module(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_pool(self, groups, in_pool=True):
        ep = self.module.Endpoint(id="e1", name="E1", enabled=True,
                                  in_pool=in_pool, pool_groups=list(groups))
        pool = self.module.APIPool([ep])
        return pool, ep

    def test_join_subgroup_drops_previous_main(self):
        pool, ep = self.make_pool(["main"])
        ep.priority_by_group = {"main": 3}
        ep.priority = 3
        pool.set_pool("e1", True, groups=["subA"])
        self.assertEqual(ep.pool_groups, ["subA"])
        self.assertNotIn("main", ep.priority_by_group)

    def test_join_other_subgroup_drops_previous_subgroup(self):
        pool, ep = self.make_pool(["subB"])
        ep.priority_by_group = {"subB": 2}
        ep.disabled_groups = ["subB"]
        pool.set_pool("e1", True, groups=["subA"])
        self.assertEqual(ep.pool_groups, ["subA"])
        self.assertNotIn("subB", ep.priority_by_group)
        self.assertEqual(ep.disabled_groups, [])

    def test_join_main_drops_subgroup(self):
        pool, ep = self.make_pool(["subA"])
        pool.set_pool("e1", True, groups=["main"])
        self.assertEqual(ep.pool_groups, ["main"])

    def test_empty_groups_still_falls_back_only_for_plain_pool_entry(self):
        """无 groups 参数的普通入池保持默认 main（旧调用兼容）。"""
        pool, ep = self.make_pool([], in_pool=False)
        pool.set_pool("e1", True)
        self.assertEqual(ep.pool_groups, ["main"])


class PoolJoinRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.module = load_module(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_groups_query_is_rejected(self):
        code, body, stream = self.module.api_handler("POST", "/api/pool/e1?groups=", {})
        self.assertEqual(code, 400)
        self.assertFalse(stream)


if __name__ == "__main__":
    unittest.main()
