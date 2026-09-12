"""整组 fallback 手动「立即切回」（UI 点击 ↩main / ⏸待回切 徽标，2026-09-12）。

契约：`POST /api/groups/<name>/clear-fallback` → `pool.clear_group_fallback(group)`
①清该组回切锁（滑动空闲窗口立即期满）②清该组端点的冷却/冻结/失败态 → 下一个请求真正
落回本组（只清锁会让仍全冷的端点立刻重新 fallback，点击等于空操作）。
端点 defer 不动：那是当前工作对象的缓存保护、非失败态，且端点可属多组。
handler 随后以当前内存态精确覆盖 runtime state 的 `cooldowns` / `group_fallback` 键，
避免崩溃重启（无 SIGTERM 快照）把已解除的锁与冷却从磁盘复活。
「待回切」= 锁已期满、尚无该组请求回组试探（`_group_fallback_pending`，不落盘）。
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
        name = f"api_pool_fallback_unlock_{_load_seq}_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        os.chdir(previous_cwd)


class GroupFallbackUnlockTests(unittest.TestCase):
    @staticmethod
    def endpoint(module, endpoint_id, priority=1, groups=("main",)):
        return module.Endpoint(
            id=endpoint_id, name=endpoint_id, base_url="http://127.0.0.1:1",
            api_key="test", model="mdl", priority=priority, in_pool=True,
            use_proxy=False, cooldown_minutes=5, pool_groups=list(groups),
        )

    @staticmethod
    def poison(ep, now):
        """把端点置为「观测到的不健康态」：冷却 + 失败计数 + 余额冻结 + defer。"""
        ep._cooldown_until = now + 300
        ep._cooldown_reason = "HTTP 429"
        ep._fail_count = 3
        ep._last_error = "HTTP 429"
        ep._manual_unlock_required = True
        ep._health = "bad"
        ep._defer_until = now + 600  # 缓存保护：不属于失败态，切回时不应被清

    def test_clear_resets_group_members_only_and_clears_lock(self):
        """清锁 + 只重置该组成员的冷却/冻结/失败态；别组端点与 defer 不受影响。"""
        with tempfile.TemporaryDirectory() as tmp_path, loaded_module(tmp_path) as module:
            now = time.time()
            mine = self.endpoint(module, "ep1", groups=("pool-gpt",))
            other = self.endpoint(module, "ep2", priority=2, groups=("pool-other",))
            self.poison(mine, now)
            self.poison(other, now)
            module.pool = module.APIPool([mine, other])
            module.pool._group_fallback_lock_until["pool-gpt"] = now + 120
            module.pool._group_fallback_pending["pool-gpt"] = True

            result = module.pool.clear_group_fallback("pool-gpt")

            self.assertEqual({"lock_cleared": True, "endpoints_reset": 1}, result)
            # 路由判定口径：锁已失效 → 该组请求不再借道 main
            self.assertFalse(
                module.pool._group_fallback_lock_until.get("pool-gpt", 0) > time.time()
            )
            self.assertNotIn("pool-gpt", module.pool._group_fallback_pending)
            # 该组端点：断言重置为待验证
            self.assertEqual(0, mine._cooldown_until)
            self.assertEqual(0, mine._fail_count)
            self.assertFalse(mine._manual_unlock_required)
            self.assertEqual("unknown", mine._health)
            # defer 是缓存保护，不是失败态 → 保留
            self.assertGreater(mine._defer_until, time.time())
            # 别组端点不受影响
            self.assertGreater(other._cooldown_until, time.time())
            self.assertTrue(other._manual_unlock_required)

    def test_clear_without_lock_still_resets_members(self):
        """「待回切」阶段（锁已期满）点击：无锁也重置端点，不是空操作。"""
        with tempfile.TemporaryDirectory() as tmp_path, loaded_module(tmp_path) as module:
            ep = self.endpoint(module, "ep1", groups=("pool-gpt",))
            self.poison(ep, time.time())
            module.pool = module.APIPool([ep])
            module.pool._group_fallback_pending["pool-gpt"] = True

            result = module.pool.clear_group_fallback("pool-gpt")

            self.assertEqual({"lock_cleared": False, "endpoints_reset": 1}, result)
            self.assertNotIn("pool-gpt", module.pool._group_fallback_pending)
            self.assertEqual(0, ep._cooldown_until)

    def test_manual_switch_back_lands_next_request_on_group(self):
        """端到端：组 fallback 后点击「立即切回」→ 下一个该组请求真的落回本组端点。"""
        with tempfile.TemporaryDirectory() as tmp_path, loaded_module(tmp_path) as module:
            bg = self.endpoint(module, "bg1", groups=("pool-bg",))
            main_ep = self.endpoint(module, "main1", priority=1, groups=("main",))
            module.pool = module.APIPool([bg, main_ep])
            calls = []

            def fake_try(ep, payload, timeout, **kwargs):
                calls.append(ep.id)
                return {"choices": [{"message": {"content": "ok"}}]}, ""

            module.pool._try_endpoint = fake_try
            module.pool._probe_endpoint = lambda ep: (True, "")
            bg._cooldown_until = time.time() + 300  # 组内仅有的端点冷却 → 入口 fallback

            module.pool.chat([{"role": "user", "content": "1"}], model="pool-bg")
            self.assertEqual(["main1"], calls)
            self.assertGreater(module.pool._group_fallback_lock_until.get("pool-bg", 0), time.time())
            self.assertTrue(module.pool._group_fallback_pending.get("pool-bg"))

            module.pool.clear_group_fallback("pool-bg")
            calls.clear()
            module.pool.chat([{"role": "user", "content": "2"}], model="pool-bg")
            self.assertEqual(["bg1"], calls)
            self.assertNotIn("pool-bg", module.pool._group_fallback_pending)

    def test_lock_expiry_returns_to_group_and_clears_pending(self):
        """期满（未点击）时：本组请求自己也回组试探，⏸待回切状态随之退场。"""
        with tempfile.TemporaryDirectory() as tmp_path, loaded_module(tmp_path) as module:
            bg = self.endpoint(module, "bg1", groups=("pool-bg",))
            main_ep = self.endpoint(module, "main1", priority=1, groups=("main",))
            module.pool = module.APIPool([bg, main_ep])
            calls = []
            module.pool._try_endpoint = lambda ep, payload, timeout, **kw: (
                calls.append(ep.id), ({"choices": [{"message": {"content": "ok"}}]}, ""))[1]
            module.pool._probe_endpoint = lambda ep: (True, "")
            module.pool._group_fallback_lock_until["pool-bg"] = time.time() - 1
            module.pool._group_fallback_pending["pool-bg"] = True

            module.pool.chat([{"role": "user", "content": "1"}], model="pool-bg")

            self.assertEqual(["bg1"], calls)
            self.assertNotIn("pool-bg", module.pool._group_fallback_pending)
            self.assertFalse(module.pool._group_fallback_lock_until.get("pool-bg", 0) > time.time())

    def test_cleared_lock_and_cooldown_not_resurrected_after_restart(self):
        """handler 落盘语义：重启只恢复仍在锁的组与仍在冷的端点。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            with loaded_module(tmp_path) as module:
                now = time.time()
                mine = self.endpoint(module, "ep1", groups=("pool-gpt",))
                other = self.endpoint(module, "ep2", priority=2, groups=("pool-other",))
                self.poison(mine, now)
                self.poison(other, now)
                module.pool = module.APIPool([mine, other])
                module._sync_to_config()
                module.pool._group_fallback_lock_until["pool-gpt"] = now + 120
                module.pool._group_fallback_lock_until["pool-other"] = now + 120
                self.assertTrue(module.snapshot_runtime_state())

                self.assertTrue(module.pool.clear_group_fallback("pool-gpt")["lock_cleared"])
                self.assertTrue(module.save_runtime_state_groups(
                    {},
                    cooldowns=module._collect_cooldown_state(),
                    fallback=module._collect_fallback_state(),
                ))
                with open(module.RUNTIME_STATE_FILE, encoding="utf-8") as handle:
                    state = json.load(handle)
                self.assertNotIn("pool-gpt", state.get("group_fallback", {}).get("locks", {}))
                self.assertIn("pool-other", state.get("group_fallback", {}).get("locks", {}))
                self.assertNotIn("ep1", state.get("cooldowns", {}))
                self.assertIn("ep2", state.get("cooldowns", {}))

            with loaded_module(tmp_path) as module2:
                self.assertFalse(
                    module2.pool._group_fallback_lock_until.get("pool-gpt", 0) > time.time()
                )
                self.assertGreater(
                    module2.pool._group_fallback_lock_until.get("pool-other", 0), time.time()
                )
                restored = {ep.id: ep for ep in module2.pool._endpoints}
                self.assertEqual(0, restored["ep1"]._cooldown_until)
                self.assertGreater(restored["ep2"]._cooldown_until, time.time())


if __name__ == "__main__":
    unittest.main()
