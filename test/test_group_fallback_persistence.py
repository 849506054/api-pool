"""组级 fallback 锁状态持久化（2026-09-06）。

背景：组 fallback 回切锁（A0 子组→main 滑动窗口）与 main 组 prio99 终极兜底锁此前均为
纯内存态，重启丢失——锁丢失导致重启后子组第一波请求先撞子组再 fallback（多一次失败）。
历史 fallback 累计计数已于 2026-09-06 移除（UI 徽标改实时锁定语义 ↩main），快照不再
收集/恢复 counts；旧快照残留 counts 键在启动恢复时剔除并回写，避免跨重启残留累计数。
契约：SIGTERM 快照写入 `group_fallback` 键（locks/fallback_locks），启动恢复未过期锁
（按绝对时间戳恢复剩余窗口）；显式写盘（手动切换等）不传 fallback 时保留文件既有值；
残留组键自愈清理。
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")

_load_seq = 0


@contextmanager
def loaded_module(tmp_path):
    """在 tmp_path 为进程 cwd 期间加载模块；退出时恢复 cwd（临时目录删除前）。"""
    global _load_seq
    _load_seq += 1
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_fallback_persist_{_load_seq}_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        os.chdir(previous_cwd)


class FallbackPersistenceTests(unittest.TestCase):
    @staticmethod
    def endpoint(module, endpoint_id, priority=1, groups=("main",)):
        return module.Endpoint(
            id=endpoint_id, name=endpoint_id, base_url="http://127.0.0.1:1",
            api_key="test", model="mdl", priority=priority, in_pool=True,
            use_proxy=False, cooldown_minutes=5, pool_groups=list(groups),
        )

    def test_snapshot_persists_locks_only(self):
        """SIGTERM 快照把未过期锁写入 group_fallback 键；不含已移除的 counts。"""
        with tempfile.TemporaryDirectory() as tmp_path, loaded_module(tmp_path) as module:
            ep = self.endpoint(module, "ep1")
            module.pool = module.APIPool([ep])
            module.pool._group_fallback_lock_until["pool-gpt"] = time.time() + 120
            module.pool._fallback_lock_until_by_group[module.APIPool.MAIN_GROUP] = time.time() + 60
            self.assertTrue(module.snapshot_runtime_state())
            with open(module.RUNTIME_STATE_FILE, encoding="utf-8") as handle:
                state = json.load(handle)
            fb = state.get("group_fallback", {})
            self.assertNotIn("counts", fb)
            self.assertGreater(fb.get("locks", {}).get("pool-gpt", 0), time.time())
            self.assertGreater(
                fb.get("fallback_locks", {}).get(module.APIPool.MAIN_GROUP, 0), time.time()
            )

    def test_snapshot_excludes_expired_locks(self):
        """过期锁不落盘。"""
        with tempfile.TemporaryDirectory() as tmp_path, loaded_module(tmp_path) as module:
            ep = self.endpoint(module, "ep1")
            module.pool = module.APIPool([ep])
            module.pool._group_fallback_lock_until["expired"] = time.time() - 5
            module.pool._group_fallback_lock_until["active"] = time.time() + 30
            self.assertTrue(module.snapshot_runtime_state())
            with open(module.RUNTIME_STATE_FILE, encoding="utf-8") as handle:
                fb = json.load(handle).get("group_fallback", {})
            self.assertEqual(list(fb.get("locks", {})), ["active"])
            self.assertNotIn("fallback_locks", fb)

    def test_explicit_save_without_fallback_preserves_existing_key(self):
        """显式写盘（手动切换风格，不传 fallback）不得抹掉文件既有 fallback 键。"""
        with tempfile.TemporaryDirectory() as tmp_path, loaded_module(tmp_path) as module:
            ep = self.endpoint(module, "ep1")
            module.pool = module.APIPool([ep])
            module.pool._group_fallback_lock_until["pool-gpt"] = time.time() + 120
            self.assertTrue(module.snapshot_runtime_state())

            # 模拟手动切换写盘：只传 groups + cooldowns，不传 fallback
            self.assertTrue(
                module.save_runtime_state_groups(
                    {module.APIPool.MAIN_GROUP: "ep1"},
                    cooldowns=module._collect_cooldown_state(),
                )
            )
            with open(module.RUNTIME_STATE_FILE, encoding="utf-8") as handle:
                fb = json.load(handle).get("group_fallback", {})
            self.assertGreater(fb.get("locks", {}).get("pool-gpt", 0), time.time())

    def test_startup_restores_unexpired_locks(self):
        """启动恢复：未过期锁按绝对时间戳恢复。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            with loaded_module(tmp_path) as module:
                ep = self.endpoint(module, "ep1", groups=("pool-gpt",))
                module.pool = module.APIPool([ep])
                module._sync_to_config()
                module.pool._group_fallback_lock_until["pool-gpt"] = time.time() + 120
                self.assertTrue(module.snapshot_runtime_state())

            # 模拟重启：重新加载模块（新进程语义），同目录读 runtime_state
            with loaded_module(tmp_path) as module2:
                restored_lock = module2.pool._group_fallback_lock_until.get("pool-gpt", 0)
                self.assertGreater(restored_lock, time.time())
                self.assertLess(restored_lock, time.time() + 120)

    def test_startup_drops_expired_locks(self):
        """过期锁不恢复，且回写剔除。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            with loaded_module(tmp_path) as module:
                ep = self.endpoint(module, "ep1", groups=("pool-gpt",))
                module.pool = module.APIPool([ep])
                module._sync_to_config()
                module.pool._group_fallback_lock_until["pool-gpt"] = time.time() - 5
                self.assertTrue(module.snapshot_runtime_state())

            with loaded_module(tmp_path) as module2:
                self.assertFalse(
                    module2.pool._group_fallback_lock_until.get("pool-gpt", 0) > time.time()
                )
                with open(module2.RUNTIME_STATE_FILE, encoding="utf-8") as handle:
                    fb = json.load(handle).get("group_fallback", {})
                self.assertNotIn("locks", fb)

    def test_startup_cleans_stale_group_keys(self):
        """组已不存在时，fallback 锁键剔除并回写（残留自愈）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            with loaded_module(tmp_path) as module:
                ep = self.endpoint(module, "ep1", groups=("pool-gpt",))
                module.pool = module.APIPool([ep])
                module._sync_to_config()
                module.pool._group_fallback_lock_until["pool-gpt"] = time.time() + 120
                module.pool._group_fallback_lock_until["gone-group"] = time.time() + 120
                self.assertTrue(module.snapshot_runtime_state())

            with loaded_module(tmp_path) as module2:
                self.assertGreater(
                    module2.pool._group_fallback_lock_until.get("pool-gpt", 0), time.time()
                )
                self.assertFalse(
                    module2.pool._group_fallback_lock_until.get("gone-group", 0) > time.time()
                )
                with open(module2.RUNTIME_STATE_FILE, encoding="utf-8") as handle:
                    fb = json.load(handle).get("group_fallback", {})
                self.assertNotIn("gone-group", fb.get("locks", {}))
                self.assertIn("pool-gpt", fb.get("locks", {}))

    def test_startup_ignores_legacy_counts(self):
        """旧快照残留 counts 键不恢复、不回写（历史累计计数已移除）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            with loaded_module(tmp_path) as module:
                ep = self.endpoint(module, "ep1", groups=("pool-gpt",))
                module.pool = module.APIPool([ep])
                module._sync_to_config()
                # 手工构造旧版快照：counts + 未过期锁
                module.save_runtime_state_groups(
                    {module.APIPool.MAIN_GROUP: "ep1"},
                    cooldowns={},
                    replace_groups=True,
                    fallback={
                        "counts": {"pool-gpt": 3},
                        "locks": {"pool-gpt": time.time() + 120},
                    },
                )

            with loaded_module(tmp_path) as module2:
                # counts 不再恢复（属性已移除），锁正常恢复
                self.assertFalse(hasattr(module2.pool, "_group_fallback_count"))
                self.assertGreater(
                    module2.pool._group_fallback_lock_until.get("pool-gpt", 0), time.time()
                )
                with open(module2.RUNTIME_STATE_FILE, encoding="utf-8") as handle:
                    fb = json.load(handle).get("group_fallback", {})
                self.assertNotIn("counts", fb)
                self.assertGreater(fb.get("locks", {}).get("pool-gpt", 0), time.time())


if __name__ == "__main__":
    unittest.main()
