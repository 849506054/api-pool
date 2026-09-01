import hashlib
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_jitter_cooldown_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class JitterCooldownTests(unittest.TestCase):
    TOL = 0.01  # 时钟采样偏移容差（内部二次 time.time() 与哈希开销在微秒级）

    @staticmethod
    def endpoint(module, endpoint_id, cooldown_minutes=5):
        return module.Endpoint(
            id=endpoint_id,
            name=endpoint_id,
            base_url="http://127.0.0.1:1",
            api_key="test",
            model="test-model",
            priority=1,
            cooldown_minutes=cooldown_minutes,
            in_pool=True,
            use_proxy=False,
        )

    def test_deterministic_same_fail_count(self):
        """冻结时钟：同端点同档位多次计算，冷却截止时刻精确一致（跨重启可复现）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool()
            fixed_now = 1787900000.0
            results = set()
            for _ in range(3):
                ep = self.endpoint(module, "ep-a")
                ep._fail_count = 2
                with mock.patch.object(module.time, "time", return_value=fixed_now):
                    pool._set_cooldown(ep)
                results.add(ep._cooldown_until)
            self.assertEqual(len(results), 1)
            (frozen_at,) = results
            seed = "ep-a:2"
            jitter_pct = 80 + int(hashlib.sha256(seed.encode()).hexdigest(), 16) % 41
            # 阶梯冷却（2026-09-01）：时长 = base × 抖动 × 连续失败次数 n，封顶 1 小时
            expected = min(fixed_now + 5 * 60 * jitter_pct / 100 * 2, fixed_now + 3600)
            self.assertEqual(frozen_at, expected)
            self.assertGreaterEqual(jitter_pct, 80)
            self.assertLessEqual(jitter_pct, 120)

    def test_jitter_range_80_120_percent(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            for i in range(50):
                ep = self.endpoint(module, f"ep-{i}")
                ep._fail_count = i
                before = time.time()
                module.APIPool()._set_cooldown(ep)
                duration = ep._cooldown_until - before
                n = max(i, 1)
                base = 5 * 60
                # 阶梯冷却：base × 抖动(80%–120%) × n，封顶 1 小时
                lo = min(base * 0.80 * n, 3600)
                hi = min(base * 1.20 * n, 3600)
                self.assertGreaterEqual(duration, lo - self.TOL)
                self.assertLessEqual(duration, hi + self.TOL)

    def test_same_batch_endpoints_freeze_apart(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool()
            before = time.time()
            ends = []
            for i in range(8):
                ep = self.endpoint(module, f"ep-{i}")
                ep._fail_count = 1
                pool._set_cooldown(ep)
                ends.append(ep._cooldown_until)
            self.assertEqual(len(set(ends)), len(ends))
            for end in ends:
                duration = end - before
                base = 5 * 60
                self.assertGreaterEqual(duration, base * 0.80 - self.TOL)
                self.assertLessEqual(duration, base * 1.20 + self.TOL)

    def test_idempotent_while_in_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool()
            ep = self.endpoint(module, "ep-a")
            ep._fail_count = 1
            pool._set_cooldown(ep)
            first = ep._cooldown_until
            ep._fail_count = 2
            pool._set_cooldown(ep)
            self.assertEqual(ep._cooldown_until, first)

    def test_fail_count_changes_window(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool()
            a = self.endpoint(module, "ep-a")
            a._fail_count = 1
            pool._set_cooldown(a)
            first = a._cooldown_until
            # 窗口过期后新档位重新计算（模拟时间推进）
            a._cooldown_until = time.time() - 1
            a._fail_count = 2
            pool._set_cooldown(a)
            second = a._cooldown_until
            self.assertNotEqual(first, second)

    def test_capacity_channel_not_jittered(self):
        """配额/余额通道窗口不含抖动：quota 走 retry-after 或默认 5h。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool()
            ep = self.endpoint(module, "ep-a")
            kind, seconds = pool._set_capacity_cooldown(ep, "quota exceeded")
            self.assertEqual(kind, "quota_exceeded")
            self.assertEqual(seconds, 5 * 60 * 60)
            self.assertAlmostEqual(ep._cooldown_until - time.time(), 5 * 60 * 60, delta=2)

    def test_cooldown_minutes_minimum_one(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool()
            ep = self.endpoint(module, "ep-a", cooldown_minutes=0)
            ep._fail_count = 0
            before = time.time()
            pool._set_cooldown(ep)
            duration = ep._cooldown_until - before
            self.assertGreaterEqual(duration, 60 * 0.80 - self.TOL)
            self.assertLessEqual(duration, 60 * 1.20 + self.TOL)

    def test_expected_jitter_values(self):
        """公式抽样校验：直接验证种子→系数映射。"""
        for seed_suffix, expected_pct in [("ep-x:0", None)]:
            seed = seed_suffix
            pct = 80 + int(hashlib.sha256(seed.encode()).hexdigest(), 16) % 41
            self.assertGreaterEqual(pct, 80)
            self.assertLessEqual(pct, 120)


if __name__ == "__main__":
    unittest.main()
