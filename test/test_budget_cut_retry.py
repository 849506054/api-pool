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
        spec = importlib.util.spec_from_file_location("api_pool_server_test_module", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


def build_pool(module):
    first = module.Endpoint(
        id="first", name="first", base_url="https://first.example/v1",
        model="gpt-6.1-sol", priority=1, in_pool=True,
    )
    second = module.Endpoint(
        id="second", name="second", base_url="https://second.example/v1",
        model="gpt-6.1-sol", priority=2, in_pool=True,
    )
    pool = module.APIPool([first, second])
    pool._current_endpoint_id = first.id
    return pool, first, second


class RetryBackoffTests(unittest.TestCase):
    def test_rate_limit_backoff_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            self.assertEqual(module.APIPool._retry_backoff(0, "rate_limit"), 3)
            self.assertEqual(module.APIPool._retry_backoff(2, "rate_limit"), 12)
            self.assertEqual(module.APIPool._retry_backoff(3, "rate_limit"), 24)
            self.assertEqual(module.APIPool._retry_backoff(9, "rate_limit"), 24)

    def test_all_categories_share_capped_backoff(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            self.assertEqual(module.APIPool._retry_backoff(4, "temporary_policy_block"), 24)
            self.assertEqual(module.APIPool._retry_backoff(7, None), 24)
            self.assertEqual(module.APIPool._retry_backoff(3), 24)


class BudgetCutRotationTests(unittest.TestCase):
    def test_budget_cut_rotation_freezes_and_switches(self):
        """预算截断跟普通失败一样记账并冻结（2026-11-04 改口径）."""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool, first, second = build_pool(module)

            pool._rotate(
                first,
                "连接/超时错误: The read operation timed out"
                "; retry skipped: request budget exhausted",
                budget_cut=True,
            )

            self.assertGreater(first._cooldown_until, 0)
            self.assertEqual(first._fail_count, 1)
            self.assertEqual(pool._current_endpoint_id, second.id)

    def test_budget_cut_capacity_error_still_freezes(self):
        """预算截断不再绕过容量类冻结：配额不足照旧按容量冷却."""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool, first, _second = build_pool(module)

            pool._rotate(
                first,
                "HTTP 502: quota exceeded; retry skipped: request budget exhausted",
                budget_cut=True,
            )

            self.assertGreater(first._cooldown_until, 0)

    def test_plain_failure_still_freezes(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool, first, _second = build_pool(module)

            pool._rotate(first, "HTTP 500: upstream broke")

            self.assertGreater(first._cooldown_until, 0)
            self.assertEqual(first._fail_count, 1)


if __name__ == "__main__":
    unittest.main()
