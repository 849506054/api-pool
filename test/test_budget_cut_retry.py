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
            self.assertEqual(module.APIPool._retry_backoff(3, "rate_limit"), 20)
            self.assertEqual(module.APIPool._retry_backoff(9, "rate_limit"), 20)

    def test_other_categories_keep_exponential_backoff(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            self.assertEqual(module.APIPool._retry_backoff(4, "temporary_policy_block"), 48)
            self.assertEqual(module.APIPool._retry_backoff(7, None), 384)


class BudgetCutRotationTests(unittest.TestCase):
    def test_budget_cut_rotation_does_not_freeze_but_switches(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool, first, second = build_pool(module)

            pool._rotate(
                first,
                "HTTP 502: upstream stream error: too_many_requests"
                "; retry skipped: request budget exhausted",
                budget_cut=True,
            )

            self.assertEqual(first._cooldown_until, 0)
            self.assertEqual(first._fail_count, 0)
            self.assertEqual(first._manual_unlock_required, False)
            self.assertEqual(pool._current_endpoint_id, second.id)

    def test_budget_cut_skips_capacity_freeze(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool, first, _second = build_pool(module)

            pool._rotate(
                first,
                "HTTP 502: quota exceeded; retry skipped: request budget exhausted",
                budget_cut=True,
            )

            self.assertEqual(first._cooldown_until, 0)
            self.assertFalse(first._manual_unlock_required)

    def test_plain_failure_still_freezes(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool, first, _second = build_pool(module)

            pool._rotate(first, "HTTP 500: upstream broke")

            self.assertGreater(first._cooldown_until, 0)
            self.assertEqual(first._fail_count, 1)


if __name__ == "__main__":
    unittest.main()
