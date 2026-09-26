"""子组整组加入 main 路由 E2E 测试（2026-09-27 新功能）。

语义：子组整组作为 main 的一个成员参与业务（相当于 apipool 内切 model），
在 main 优先级轴上占一个名次（main_priority），名次段内沿用子组自身优先级；
整组内全挂才在 main 序列继续向后 fallback。手动切换指向子组整体（口径A）。

覆盖：
- 加入/调序/移出（set_main_priority / leave_main）
- main 候选梯队排序（原生优先、子组按 main_priority 分段、段内沿用子组优先级）
- 整组全挂降下一名次
- 跨多子组去重
- 手动切到子组整体（下钻选端点、子组当前端点变化自动跟随）
- 删/改名子组同步 main_priority 与 main 指针
- persist/restore：main 指针=子组名
- 零回归：无子组加入时行为不变
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_joinmain_test_{os.getpid()}_{id(threading.current_thread())}"
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


class GroupJoinMainTests(unittest.TestCase):
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

    @staticmethod
    def ok_try(ep, payload, timeout, **kwargs):
        return {"choices": [{"message": {"content": f"from-{ep.name}"}}]}, ""

    def make_pool(self, module, endpoints, subgroups=None):
        pool = module.APIPool(endpoints)
        # 给子组一个 def（否则派生态也可，但显式更清晰）
        for name, gd in (subgroups or {}).items():
            pool._group_defs[name] = gd
        return pool

    # ── 加入 / 调序 / 移出 ──

    def test_join_assigns_default_last_priority(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [m1, b1],
                                  {"bg": {"type": "mixed", "model": "bg"}})
            ok, _ = pool.set_main_priority("bg", None)
            self.assertTrue(ok)
            # main 原生最大名次=1 → 默认末位=2
            self.assertEqual(pool._joined_subgroups(), {"bg": 2})

    def test_join_explicit_priority_and_reorder(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [b1], {"bg": {"type": "mixed", "model": "bg"}})
            # 统一轴 insert-at-position：轴上只有 bg 一个条目，名次 3 越界 → clamp 到 1
            self.assertTrue(pool.set_main_priority("bg", 3)[0])
            self.assertEqual(pool._joined_subgroups(), {"bg": 1})
            # 调序到 1（唯一条目）
            self.assertTrue(pool.set_main_priority("bg", 1)[0])
            self.assertEqual(pool._joined_subgroups(), {"bg": 1})

    def test_join_explicit_priority_with_native_endpoints(self):
        """带原生端点时显式名次走 insert-at-position：其余条目位移、名次唯一连续。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [m1, b1], {"bg": {"type": "mixed", "model": "bg"}})
            # 轴：[m1(#1)]，bg 插到名次 1 → m1 被挤到 #2
            self.assertTrue(pool.set_main_priority("bg", 1)[0])
            self.assertEqual(pool._joined_subgroups(), {"bg": 1})
            self.assertEqual(pool._ep_priority(m1, "main"), 2)
            # bg 改到名次 2 → m1 回到 #1
            self.assertTrue(pool.set_main_priority("bg", 2)[0])
            self.assertEqual(pool._joined_subgroups(), {"bg": 2})
            self.assertEqual(pool._ep_priority(m1, "main"), 1)

    def test_join_rejects_builtin_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = self.make_pool(module, [self.endpoint(module, "m1", 1, "x")])
            self.assertFalse(pool.set_main_priority("main", 1)[0])
            self.assertFalse(pool.set_main_priority("vision", 1)[0])
            self.assertFalse(pool.set_main_priority("nope", 1)[0])

    def test_leave_main_clears(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [b1], {"bg": {"type": "mixed", "model": "bg"}})
            pool.set_main_priority("bg", 2)
            self.assertTrue(pool.leave_main("bg")[0])
            self.assertEqual(pool._joined_subgroups(), {})
            # 重复移出报未加入
            self.assertFalse(pool.leave_main("bg")[0])

    # ── 统一优先级轴不变量（2026-09-27 修正）──

    def test_main_axis_priority_unique_and_continuous(self):
        """子组与端点共享 main 优先级轴：名次唯一且连续 1..N。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            m2 = self.endpoint(module, "m2", 2, "glm-5.3")
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [m1, m2, b1], {"bg": {"type": "mixed", "model": "bg"}})
            pool.set_main_priority("bg", 3)
            # 轴上：m1(#1) m2(#2) bg(#3)
            slots = pool._main_axis_slots()
            self.assertEqual([p for _, _, p in slots], [1, 2, 3])
            self.assertEqual(sorted(p for _, _, p in slots), list(range(1, len(slots) + 1)))

    def test_endpoint_reorder_displaces_subgroup(self):
        """端点改序会把同轴的子组一起挤开（统一轴 insert-at-position）。

        旧实现子组单开一套编号，端点改序完全不知子组存在 → 子组名次撞号/跳号。
        """
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            m2 = self.endpoint(module, "m2", 2, "glm-5.3")
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [m1, m2, b1], {"bg": {"type": "mixed", "model": "bg"}})
            pool.set_main_priority("bg", 2)  # 轴：m1(#1) bg(#2) m2(#3)
            # 把 m2 插到名次 1 → m2 #1，其余顺延：m1→#2，bg→#3
            pool.set_group_priority("m2", "main", 1)
            self.assertEqual(pool._ep_priority(m2, "main"), 1)
            self.assertEqual(pool._ep_priority(m1, "main"), 2)
            self.assertEqual(pool._joined_subgroups()["bg"], 3)
            # 名次唯一连续
            slots = pool._main_axis_slots()
            self.assertEqual(sorted(p for _, _, p in slots), [1, 2, 3])

    def test_subgroup_reorder_displaces_endpoint(self):
        """子组改序同样把端点挤开（对称性：互为一等成员）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            m2 = self.endpoint(module, "m2", 2, "glm-5.3")
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [m1, m2, b1], {"bg": {"type": "mixed", "model": "bg"}})
            pool.set_main_priority("bg", None)  # 末位 #3
            self.assertEqual(pool._joined_subgroups()["bg"], 3)
            # bg 插到名次 1 → m1→#2, m2→#3
            pool.set_main_priority("bg", 1)
            self.assertEqual(pool._joined_subgroups()["bg"], 1)
            self.assertEqual(pool._ep_priority(m1, "main"), 2)
            self.assertEqual(pool._ep_priority(m2, "main"), 3)

    def test_member_count_includes_joined_subgroup(self):
        """main 成员数 = 原生端点数 + 已加入子组数（子组算 1 个成员）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            m2 = self.endpoint(module, "m2", 2, "glm-5.3")
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            b2 = self.endpoint(module, "b2", 2, "ds", groups=["bg"])
            pool = self.make_pool(module, [m1, m2, b1, b2], {"bg": {"type": "mixed", "model": "bg"}})
            native = sum(1 for e in pool._endpoints if e.in_pool and "main" in pool._ep_groups(e))
            pool.set_main_priority("bg", None)
            expected = native + len(pool._joined_subgroups())
            self.assertEqual(native, 2)      # 原生只有 m1/m2
            self.assertEqual(expected, 3)    # + bg 一个成员
            # 子组内 2 个端点不展开计入
            self.assertNotEqual(expected, 4)

    # ── main 候选梯队排序 ──

    def test_main_candidates_tiered_order(self):
        """main 原生端点优先，子组成员按 main_priority 分段、段内沿用子组优先级。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            m2 = self.endpoint(module, "m2", 2, "glm-5.3")
            # bg: 两个端点，组内优先级 b2<b1（b2 优先）
            b1 = self.endpoint(module, "b1", 2, "ds", groups=["bg"])
            b2 = self.endpoint(module, "b2", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [m1, m2, b1, b2],
                                  {"bg": {"type": "mixed", "model": "bg"}})
            pool.set_main_priority("bg", 3)  # bg 段名次 3（排在 m1=1, m2=2 之后）
            cands, starved = pool._group_sticky_candidates("main")
            self.assertFalse(starved)
            self.assertEqual([e.id for e in cands], ["m1", "m2", "b2", "b1"])

    def test_multi_subgroup_segments(self):
        """多子组按各自 main_priority 分段。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            c1 = self.endpoint(module, "c1", 1, "gpt", groups=["gpt"])
            pool = self.make_pool(module, [m1, b1, c1],
                                  {"bg": {"type": "mixed", "model": "bg"},
                                   "gpt": {"type": "mixed", "model": "gpt"}})
            pool.set_main_priority("gpt", 2)
            pool.set_main_priority("bg", 3)
            cands, _ = pool._group_sticky_candidates("main")
            # m1(名次1) → gpt(名次2) → bg(名次3)
            self.assertEqual([e.id for e in cands], ["m1", "c1", "b1"])

    def test_cross_membership_dedup(self):
        """端点同属 main 原生与子组 → 只算原生一份（不翻倍）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            shared = self.endpoint(module, "shared", 1, "ds", groups=["main", "bg"])
            b1 = self.endpoint(module, "b1", 2, "ds", groups=["bg"])
            pool = self.make_pool(module, [shared, b1],
                                  {"bg": {"type": "mixed", "model": "bg"}})
            pool.set_main_priority("bg", 5)
            cands, _ = pool._group_sticky_candidates("main")
            ids = [e.id for e in cands]
            self.assertEqual(ids.count("shared"), 1)
            self.assertEqual(ids, ["shared", "b1"])

    def test_whole_subgroup_down_when_all_cooldown(self):
        """子组整段全部冷却 → 从 main 候选消失，轮到下一名次。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [m1, b1],
                                  {"bg": {"type": "mixed", "model": "bg"}})
            pool.set_main_priority("bg", 2)  # bg 段名次 2（在 m1 之后）
            # 正常：两者都在候选，m1 领先（名次1），b1 其后（名次2）
            self.assertEqual([e.id for e in pool._group_sticky_candidates("main")[0]], ["m1", "b1"])
            # b1 冷却 → bg 整段消失，只剩 m1
            import time as _t
            b1._cooldown_until = _t.time() + 999
            self.assertEqual([e.id for e in pool._group_sticky_candidates("main")[0]], ["m1"])

    # ── 手动切到子组整体（口径A）──

    def test_switch_main_to_subgroup_downchain(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            b1 = self.endpoint(module, "b1", 2, "ds", groups=["bg"])
            b2 = self.endpoint(module, "b2", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [m1, b1, b2],
                                  {"bg": {"type": "mixed", "model": "bg"}})
            pool.set_main_priority("bg", 2)
            self.assertTrue(pool.switch_main_to_subgroup("bg")[0])
            # main 指针=子组名
            self.assertEqual(pool._get_manual("main"), "bg")
            # 下钻：子组内最高优先级可用端点 b2
            self.assertEqual(pool._resolve_subgroup_current("bg"), "b2")
            # 请求真正落到子组端点
            pool._try_endpoint = self.ok_try
            r = pool.chat([{"role": "user", "content": "x"}], model="api-pool")
            self.assertIn(r["choices"][0]["message"]["content"], ("from-b2", "from-b1"))
            # 关键回归（2026-09-27）：请求成功后 main 指针必须仍指向子组名，
            # 不得被下钻出的端点 id 覆盖，否则下一次请求自动跳回 main 原生端点。
            self.assertEqual(pool._get_manual("main"), "bg")
            self.assertEqual(pool._get_current("main"), "bg")
            # 第二个请求仍走子组（不被回写成 m1）
            r2 = pool.chat([{"role": "user", "content": "y"}], model="api-pool")
            self.assertIn(r2["choices"][0]["message"]["content"], ("from-b2", "from-b1"))
            self.assertEqual(pool._get_manual("main"), "bg")

    def test_switch_subgroup_follows_current_endpoint_change(self):
        """子组当前端点变化时，main 手动指向子组自动跟随（不锁死具体端点）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            b2 = self.endpoint(module, "b2", 2, "ds", groups=["bg"])
            pool = self.make_pool(module, [b1, b2], {"bg": {"type": "mixed", "model": "bg"}})
            pool.set_main_priority("bg", 1)
            pool.switch_main_to_subgroup("bg")
            # 子组自身当前指向 b2 → main 跟随 b2
            pool._set_current("bg", "b2")
            self.assertEqual(pool._resolve_subgroup_current("bg"), "b2")
            # 子组当前改到 b1 → main 跟随 b1
            pool._set_current("bg", "b1")
            self.assertEqual(pool._resolve_subgroup_current("bg"), "b1")

    def test_switch_rejects_not_joined(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [b1], {"bg": {"type": "mixed", "model": "bg"}})
            self.assertFalse(pool.switch_main_to_subgroup("bg")[0])

    # ── 删 / 改名子组同步 ──

    def test_delete_joined_subgroup_clears_main_pointer(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [m1, b1], {"bg": {"type": "mixed", "model": "bg"}})
            pool.set_main_priority("bg", 2)
            pool.switch_main_to_subgroup("bg")
            self.assertEqual(pool._get_manual("main"), "bg")
            pool.delete_group("bg")
            self.assertIsNone(pool._get_manual("main"))
            self.assertIsNone(pool._get_current("main"))
            self.assertEqual(pool._joined_subgroups(), {})

    def test_rename_joined_subgroup_follows(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [b1, m1], {"bg": {"type": "mixed", "model": "bg"}})
            pool.set_main_priority("bg", 2)
            pool.switch_main_to_subgroup("bg")
            ok, newname = pool.update_group("bg", {"name": "bg2"})
            self.assertTrue(ok)
            self.assertEqual(newname, "bg2")
            self.assertEqual(pool._joined_subgroups(), {"bg2": 2})
            # main 指针跟随改名
            self.assertEqual(pool._get_manual("main"), "bg2")

    # ── persist / restore ──

    def test_persist_restore_main_pointer_is_subgroup(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [m1, b1], {"bg": {"type": "mixed", "model": "bg"}})
            pool.set_main_priority("bg", 2)
            pool.switch_main_to_subgroup("bg")
            # 落盘 main→bg
            self.assertTrue(module.save_runtime_state_groups({"main": "bg", "bg": "b1"}))
            state = module.load_runtime_state()
            self.assertEqual(state.get("groups", {}).get("main"), "bg")

    def test_restore_main_pointer_from_subgroup_member_endpoint_id(self):
        """重启恢复：runtime_state 里 main 存的是「已加入子组成员端点 id」时，
        必须恢复为该子组名，而不是 WARN 忽略（2026-09-27 修复）。

        旧版本请求成功后会把这个下钻端点 id 落盘；该端点不属于 main 原生成员，
        通用校验 `grp in _ep_groups(ep)` 会判「不存在或不可用」→ 重启丢失子组指针。
        """
        with tempfile.TemporaryDirectory() as tmp_path:
            # 1) 配置：m1 属 main；b1 属子组 bg（bg 已加入 main，名次 2）
            with open(os.path.join(tmp_path, "api_config.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "api_endpoints": [
                        {"id": "m1", "name": "m1", "base_url": "http://127.0.0.1:1",
                         "api_key": "t", "model": "glm", "in_pool": True, "pool_groups": ["main"]},
                        {"id": "b1", "name": "b1", "base_url": "http://127.0.0.1:1",
                         "api_key": "t", "model": "ds", "in_pool": True, "pool_groups": ["bg"]},
                    ],
                    "pool_group_defs": [
                        {"name": "bg", "type": "mixed", "model": "bg", "main_priority": 2},
                    ],
                }, f)
            # 2) 运行态：main 指针 = 子组成员端点 id（旧格式残留）
            with open(os.path.join(tmp_path, "api_runtime_state.json"), "w", encoding="utf-8") as f:
                json.dump({"groups": {"main": "b1", "bg": "b1"}}, f)
            # 3) import 触发模块级恢复
            module = load_module(tmp_path)
            pool = module.pool
            self.assertIn("bg", pool._joined_subgroups())
            # 恢复为子组名（而非被 WARN 忽略 / 或锁死在端点 id 上）
            self.assertEqual(pool._get_manual("main"), "bg")
            self.assertEqual(pool._get_current("main"), "bg")
            # 下钻仍解析到同一端点，且后续请求走该子组
            self.assertEqual(pool._resolve_subgroup_current("bg"), "b1")

    # ── 整组冻结同步（main 列表子组条目）──

    def test_group_all_frozen_remaining_shortest_endpoint(self):
        """组内端点全部冷却 → 整组冻结秒数 = 最短剩余冻结端点（最早恢复者）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            b2 = self.endpoint(module, "b2", 2, "ds", groups=["bg"])
            pool = self.make_pool(module, [b1, b2], {"bg": {"type": "mixed", "model": "bg"}})
            pool.set_main_priority("bg", 2)
            # 有可用端点 → 0
            self.assertEqual(pool._group_all_frozen_remaining("bg"), 0)
            # 全部冷却：b1 剩 1200s，b2 剩 3600s → 取最短 1200（int 截断允许 ±1）
            now = time.time()
            b1._cooldown_until = now + 1200
            b2._cooldown_until = now + 3600
            self.assertIn(pool._group_all_frozen_remaining("bg"), (1199, 1200))

    def test_group_all_frozen_remaining_partial_available(self):
        """组内仍有可用端点 → 未整组冻结，返回 0。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            b2 = self.endpoint(module, "b2", 2, "ds", groups=["bg"])
            pool = self.make_pool(module, [b1, b2], {"bg": {"type": "mixed", "model": "bg"}})
            b1._cooldown_until = time.time() + 900
            # b2 仍可用
            self.assertEqual(pool._group_all_frozen_remaining("bg"), 0)

    def test_group_all_frozen_remaining_expired_cooldown(self):
        """冷却已过期的端点不算冻结证据（无可用候选但冷却都过期 → 0）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [b1], {"bg": {"type": "mixed", "model": "bg"}})
            b1._cooldown_until = time.time() - 10  # 已过期
            self.assertEqual(pool._group_all_frozen_remaining("bg"), 0)

    def test_switch_main_to_frozen_subgroup_rejected(self):
        """整组冻结的子组拒绝手动切换（与普通端点冻结后不可切到同语义）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [m1, b1], {"bg": {"type": "mixed", "model": "bg"}})
            pool.set_main_priority("bg", 2)
            b1._cooldown_until = time.time() + 600
            ok, msg = pool.switch_main_to_subgroup("bg")
            self.assertFalse(ok)
            self.assertIn("全部冻结", msg)
            # 指针未被写入
            self.assertIsNone(pool._get_manual("main"))

    def test_switch_main_to_healthy_subgroup_allowed(self):
        """有可用端点的子组可正常手动切换（冻结守卫不影响正常路径）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [m1, b1], {"bg": {"type": "mixed", "model": "bg"}})
            pool.set_main_priority("bg", 2)
            ok, _ = pool.switch_main_to_subgroup("bg")
            self.assertTrue(ok)
            self.assertEqual(pool._get_manual("main"), "bg")

    # ── 零回归 ──

    def test_no_join_main_behaves_identically(self):
        """无子组加入时 main 候选与旧行为一致（原生成员，无子组混入）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            m2 = self.endpoint(module, "m2", 2, "glm-5.3")
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [m1, m2, b1],
                                  {"bg": {"type": "mixed", "model": "bg"}})
            # 未加入 main
            self.assertEqual(pool._joined_subgroups(), {})
            cands, _ = pool._group_sticky_candidates("main")
            self.assertEqual(sorted(e.id for e in cands), ["m1", "m2"])
            # bg 请求不受影响
            self.assertEqual([e.id for e in pool._group_sticky_candidates("bg")[0]], ["b1"])

    def test_config_roundtrip_main_priority(self):
        """main_priority 随 config 持久化并在重载后恢复。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1", 1, "ds", groups=["bg"])
            pool = self.make_pool(module, [b1], {"bg": {"type": "mixed", "model": "bg"}})
            pool.set_main_priority("bg", 4)
            # 模拟落盘/重载 defs
            defs = [{"name": "main", "type": "mixed", "model": "api-pool"},
                    {"name": "bg", "type": "mixed", "model": "bg", "main_priority": 4}]
            pool2 = module.APIPool([self.endpoint(module, "b1", 1, "ds", groups=["bg"])])
            pool2._load_group_defs(defs)
            self.assertEqual(pool2._joined_subgroups(), {"bg": 4})


if __name__ == "__main__":
    unittest.main()
