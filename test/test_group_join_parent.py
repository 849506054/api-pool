"""Dedicated-child membership in mixed parents: isolated routing and API contracts."""

import copy
import json
import tempfile
import time
import unittest
from unittest.mock import patch

import test_group_join_main as main_tests

load_module = main_tests.load_module


class GroupJoinParentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load_module(self.tmp.name)
        self.pool = self.m.APIPool([])
        self.m.pool = self.pool
        self.pool._credit_view = lambda ep: None
        self.pool._load_group_defs([
            {"name": "worker", "type": "mixed", "model": "worker"},
            {"name": "cron", "type": "mixed", "model": "cron"},
            {"name": "child", "type": "dedicated", "model": "child-model"},
            {"name": "other", "type": "dedicated", "model": "other-model"},
        ])

    def ep(self, name, group, priority=1, groups=None):
        ep = main_tests.GroupJoinMainTests.endpoint(self.m, name, priority, "test-model", groups or [group])
        ep.priority_by_group = {group: priority}
        self.pool._endpoints.append(ep)
        return ep

    def join(self, child="child", parent="worker", priority=None):
        ok, msg = self.pool.set_subgroup_priority(child, parent, priority)
        self.assertTrue(ok, msg)

    def test_parent_axis_shared_child_isolation_and_idempotent_append(self):
        native = self.ep("native", "worker")
        child = self.ep("c", "child", 7)
        self.ep("o", "other", 4)
        self.join(priority=1)
        self.join("other")
        self.join(parent="cron")
        self.join(parent="main")
        before = copy.deepcopy(self.pool._group_defs)
        epoch = self.pool._get_route_epoch("worker")
        cron_epoch = self.pool._get_route_epoch("cron")
        self.join()
        self.assertEqual(before, self.pool._group_defs)
        self.assertEqual(self.pool._get_route_epoch("worker"), epoch)
        self.join(priority=1)
        self.assertGreater(self.pool._get_route_epoch("worker"), epoch)
        self.assertEqual(self.pool._get_route_epoch("cron"), cron_epoch)
        self.assertEqual(self.pool._joined_subgroups("worker"), {"child": 1, "other": 3})
        self.pool.set_group_priority(native.id, "worker", 1)
        self.assertEqual(self.pool._joined_subgroups("worker"), {"child": 2, "other": 3})
        self.assertEqual(self.pool._group_defs["child"]["priority_by_group"], {"worker": 2, "cron": 1, "main": 1})
        self.assertEqual(self.pool._ep_priority(child, "child"), 7)
        self.assertTrue(self.pool.leave_group("child", "worker")[0])
        self.assertEqual(self.pool._joined_subgroups("worker"), {"other": 2})
        self.assertEqual(self.pool._joined_subgroups("cron"), {"child": 1})

    def test_validation_and_type_change_are_atomic(self):
        for child, parent in (("worker", "main"), ("child", "child"), ("child", "other"),
                              ("vision", "worker"), ("child", "vision"), ("main", "worker"),
                              ("missing", "worker"), ("child", "missing")):
            with self.subTest(child=child, parent=parent):
                self.assertFalse(self.pool.set_subgroup_priority(child, parent, 1)[0])
        for priority in (0, -1, True, 1.5, "bad", "", [], {}):
            with self.subTest(priority=priority):
                self.assertFalse(self.pool.set_subgroup_priority("child", "worker", priority)[0])
        self.join()
        before = copy.deepcopy(self.pool._group_defs)
        self.assertFalse(self.pool.update_group("child", {"type": "mixed"})[0])
        self.assertFalse(self.pool.update_group("worker", {"type": "dedicated", "model": "worker-model"})[0])
        self.assertEqual(before, self.pool._group_defs)

    def test_two_pass_migration_map_authority_and_invalid_edges(self):
        self.pool._load_group_defs([
            {"name": "child", "type": "dedicated", "main_priority": 3},
            {"name": "other", "type": "dedicated", "main_priority": 8, "priority_by_group": {}},
            {"name": "last", "type": "dedicated", "priority_by_group": {"worker": 2, "vision": 1, "child": 1, "last": 1, "missing": 1}},
            {"name": "worker", "type": "mixed"},
        ])
        self.assertEqual(self.pool._joined_subgroups(), {"child": 3})
        self.assertEqual(self.pool._joined_subgroups("worker"), {"last": 2})
        self.assertEqual(self.pool._group_defs["child"]["priority_by_group"], {"main": 3})
        self.assertNotIn("main_priority", self.pool._group_defs["child"])
        with self.assertRaisesRegex(ValueError, "mixed|专用"):
            self.pool._load_group_defs([{"name": "legacy", "type": "mixed", "main_priority": 1}])

    def test_candidates_dedup_native_wins_and_child_disabled(self):
        shared = self.ep("shared", "worker", 3, ["worker", "child", "other"])
        shared.priority_by_group.update(child=1, other=1)
        c = self.ep("c", "child", 2, ["child", "other"])
        self.join(priority=1)
        self.join("other", priority=2)
        self.assertEqual([e.id for e in self.pool._group_sticky_candidates("worker")[0]], ["c", "shared"])
        self.assertIsNone(self.pool._axis_subgroup(shared, "worker"))
        self.assertEqual(self.pool._axis_subgroup(c, "worker"), "child")
        c.disabled_groups = ["child"]
        self.assertEqual(self.pool._axis_subgroup(c, "worker"), "other")
        self.assertEqual(len(self.pool._group_sticky_candidates("worker")[0]), 2)
        c.disabled_groups.append("other")
        self.assertEqual([e.id for e in self.pool._group_sticky_candidates("worker")[0]], ["shared"])

    def test_parent_occupancy_uses_main_resolved_pointer_and_physical_freeze(self):
        c = self.ep("c", "child")
        self.join()
        self.join(parent="main")
        self.pool._set_current("main", "child")
        self.pool._set_current("child", c.id)
        self.assertEqual(self.pool._group_sticky_candidates("worker")[0], [])
        self.assertEqual(self.pool._failover_active("worker"), [])
        self.assertEqual(self.pool._group_sticky_candidates("child")[0], [c])
        self.assertEqual(self.pool._group_all_frozen_remaining("child"), 0)
        self.pool._group_defs["main"]["idle_seconds"] = 10
        c._last_activity_by_group["main"] = time.time() - 11
        self.assertEqual(self.pool._group_sticky_candidates("worker")[0], [c])
        self.pool._acquire_inflight(c.id, "main")
        self.assertEqual(self.pool._group_sticky_candidates("worker")[0], [])
        self.pool._release_inflight(c.id, "main")

    def test_sticky_success_shared_child_owner_and_epoch(self):
        c1 = self.ep("c1", "child", 1, ["child", "other"])
        c2 = self.ep("c2", "child", 2)
        self.join()
        self.join("other")
        self.join(parent="cron")
        self.pool._set_current("cron", "other")
        self.pool._set_current("other", c1.id)
        self.pool._set_current("child", c2.id)
        self.pool._group_fallback_lock_until["worker"] = time.time() + 300
        self.assertTrue(self.pool.switch_group_to_subgroup("child", "worker")[0])
        self.assertNotIn("worker", self.pool._group_fallback_lock_until)
        self.pool._try_endpoint = main_tests.GroupJoinMainTests.ok_try
        for _ in range(2):
            result = self.pool.chat([{"role": "user", "content": "x"}], model="worker")
            self.assertEqual(result["choices"][0]["message"]["content"], "from-c2")
        self.assertEqual(self.pool._get_current("worker"), "child")
        self.assertEqual(self.pool._get_current("cron"), "other")
        epoch = self.pool._get_route_epoch("worker")
        self.pool._set_current("worker", "other")
        self.pool._on_success(c2, group="worker", expected_route_epoch=epoch)
        self.assertEqual(self.pool._get_current("worker"), "other")
        # A fallback success belongs to its actual slot, not a stale child pointer.
        native = self.ep("native", "worker")
        self.pool._on_success(native, group="worker")
        self.assertEqual(self.pool._get_current("worker"), native.id)

    def test_child_switch_during_first_parent_request(self):
        c1 = self.ep("c1", "child")
        self.ep("c2", "child", 2)
        self.ep("native", "cron")
        self.join()
        self.join(parent="cron")
        self.pool._set_current("child", c1.id)
        self.pool._set_current("cron", "native")
        self.pool._group_defs["worker"]["rotate_requests"] = 10
        epochs = {g: self.pool._get_route_epoch(g) for g in ("worker", "cron", "main")}
        self.assertIsNone(self.pool._get_current("worker"))

        def attempt(ep, *args, **kwargs):
            self.assertEqual(ep.id, c1.id)
            self.assertTrue(self.pool.switch_to_endpoint("c2", group="child"))
            self.assertGreater(self.pool._get_route_epoch("worker"), epochs["worker"])
            self.assertGreater(self.pool._get_route_epoch("cron"), epochs["cron"])
            self.assertEqual(self.pool._get_route_epoch("main"), epochs["main"])
            return main_tests.GroupJoinMainTests.ok_try(ep, *args, **kwargs)

        self.pool._try_endpoint = attempt
        result = self.pool.chat([{"role": "user", "content": "x"}], model="worker")
        self.assertEqual(result["choices"][0]["message"]["content"], "from-c1")
        self.assertEqual(c1._total_calls, 1)
        self.assertEqual(self.pool._get_current("child"), "c2")
        self.assertIsNone(self.pool._get_current("worker"))
        self.assertEqual(self.pool._get_current("cron"), "native")
        self.assertEqual(self.pool._rotate_counts.get("worker", 0), 0)
        self.assertEqual(self.pool._joined_subgroups("cron"), {"child": 2})
        self.pool._try_endpoint = main_tests.GroupJoinMainTests.ok_try
        result = self.pool.chat([{"role": "user", "content": "x"}], model="worker")
        self.assertEqual(result["choices"][0]["message"]["content"], "from-c2")
        self.assertEqual(self.pool._get_current("worker"), "child")
        self.assertEqual(self.pool._rotate_counts["worker"], 1)

    def test_leave_during_first_parent_request(self):
        c1 = self.ep("c1", "child")
        self.join()
        self.join(parent="cron")
        self.pool._set_current("child", c1.id)
        self.pool._set_current("cron", "child")
        self.pool._set_persisted("cron", "child")
        self.pool._group_defs["worker"]["rotate_requests"] = 10
        epochs = {g: self.pool._get_route_epoch(g) for g in ("worker", "cron", "main")}
        self.assertIsNone(self.pool._get_current("worker"))

        def attempt(ep, *args, **kwargs):
            self.assertEqual(ep.id, c1.id)
            status, _, _ = self.m.api_handler(
                "POST", "/api/groups/child/leave-group", {"group": "worker"})
            self.assertEqual(status, 200)
            self.assertEqual(self.pool._joined_subgroups("worker"), {})
            self.assertGreater(self.pool._get_route_epoch("worker"), epochs["worker"])
            return main_tests.GroupJoinMainTests.ok_try(ep, *args, **kwargs)

        self.pool._try_endpoint = attempt
        result = self.pool.chat([{"role": "user", "content": "x"}], model="worker")
        self.assertEqual(result["choices"][0]["message"]["content"], "from-c1")
        self.assertEqual(c1._total_calls, 1)
        self.assertIsNone(self.pool._get_current("worker"))
        self.assertIsNone(self.pool._get_persisted("worker"))
        self.assertEqual(self.pool._rotate_counts.get("worker", 0), 0)
        self.assertEqual(self.pool._get_current("child"), c1.id)
        self.assertEqual(self.pool._get_current("cron"), "child")
        self.assertEqual(self.pool._get_persisted("cron"), "child")
        self.assertEqual(self.pool._joined_subgroups("cron"), {"child": 1})
        for group in ("cron", "main"):
            self.assertEqual(self.pool._get_route_epoch(group), epochs[group])
        self.assertNotIn("worker", self.m.load_runtime_state().get("groups", {}))
        self.assertTrue(self.m.snapshot_runtime_state())
        self.assertEqual(self.m.load_runtime_state()["groups"], {"child": "c1", "cron": "child"})
        with open(self.m.CONFIG_FILE, encoding="utf-8") as handle:
            config = json.load(handle)
        child = next(g for g in config["pool_group_defs"] if g["name"] == "child")
        self.assertEqual(child["priority_by_group"], {"cron": 1})

    def test_failover_exhausts_child_ring_before_next_parent_slot(self):
        a = self.ep("a", "worker")
        self.ep("c1", "child", 1)
        c2 = self.ep("c2", "child", 2)
        self.ep("c3", "child", 3)
        z = self.ep("z", "worker", 2)
        self.join(priority=2)
        self.pool._set_current("worker", "child")
        self.pool._set_current("child", c2.id)
        calls = []

        def attempt(ep, *args, **kwargs):
            calls.append(ep.id)
            if ep.id in {"c1", "c2", "c3"}:
                return None, "HTTP 503: unavailable"
            return main_tests.GroupJoinMainTests.ok_try(ep, *args, **kwargs)

        self.pool._try_endpoint = attempt
        with patch.object(self.pool, "_probe_endpoint", side_effect=AssertionError("unexpected probe")):
            result = self.pool.chat([{"role": "user", "content": "x"}], model="worker")
        self.assertEqual(calls, ["c2", "c3", "c1", "z"])
        self.assertEqual(result["choices"][0]["message"]["content"], "from-z")
        self.assertEqual(self.pool._get_current("worker"), z.id)
        self.assertEqual(self.pool._group_all_frozen_remaining("child") > 0, True)
        self.assertTrue(a.enabled)

    def test_next_child_slot_starts_from_its_shared_sticky(self):
        n = self.ep("n", "worker")
        self.ep("c1", "child")
        c2 = self.ep("c2", "child", 2)
        self.join()
        self.pool._set_current("child", c2.id)
        ordered = self.pool._ordered_failover_candidates(n, self.pool._failover_active("worker"), group="worker")
        self.assertEqual([ep.id for ep in ordered], ["c2", "c1"])

    def test_failure_keeps_parent_child_slot_and_shared_endpoint_pointer(self):
        c1 = self.ep("c1", "child")
        c2 = self.ep("c2", "child", 2)
        self.join()
        self.pool._set_current("worker", "child")
        self.pool._set_current("child", c1.id)
        epoch = self.pool._get_route_epoch("worker")
        self.pool._rotate(c1, "HTTP 503", group="worker")
        self.assertEqual(self.pool._get_current("worker"), "child")
        self.assertEqual(self.pool._get_current("child"), c2.id)
        self.pool._on_success(c1, group="worker", expected_route_epoch=epoch)
        self.assertEqual(self.pool._get_current("child"), c2.id)

    def test_rest_rename_delete_persist_membership_and_pointer_references(self):
        self.ep("c", "child")
        api = self.m.api_handler
        for parent in ("worker", "cron"):
            self.assertEqual(api("POST", "/api/groups/child/join-group", {"group": parent})[0], 200)
            self.assertEqual(api("POST", "/api/groups/child/switch-group", {"group": parent})[0], 200)
        self.assertEqual(api("PUT", "/api/groups/worker", {"name": "jobs"})[0], 200)
        self.assertEqual(api("PUT", "/api/groups/child", {"name": "engine"})[0], 200)
        self.assertEqual(self.m.load_runtime_state()["groups"], {"jobs": "engine", "cron": "engine"})
        self.assertEqual(api("DELETE", "/api/groups/jobs", None)[0], 200)
        self.assertEqual(self.m.load_runtime_state()["groups"], {"cron": "engine"})
        self.assertEqual(api("DELETE", "/api/groups/engine", None)[0], 200)
        self.assertEqual(self.m.load_runtime_state().get("groups", {}), {})

    def test_rotation_counts_child_as_one_slot(self):
        native = self.ep("n", "worker")
        c1 = self.ep("c1", "child")
        self.ep("c2", "child", 2)
        self.join()
        self.pool._group_defs["worker"]["rotate_requests"] = 1
        self.pool._set_current("worker", "child")
        self.pool._set_current("child", c1.id)
        self.pool._on_success(c1, group="worker")
        self.assertEqual(self.pool._get_current("worker"), native.id)
        self.pool._on_success(native, group="worker")
        self.assertEqual(self.pool._get_current("worker"), "child")
        self.assertEqual(self.pool._get_current("child"), c1.id)

    def test_rename_delete_scoped_maps_and_pointers(self):
        c = self.ep("c", "child")
        n = self.ep("n", "worker")
        self.join()
        self.join(parent="cron")
        for parent in ("worker", "cron"):
            self.pool._set_current(parent, "child")
            self.pool._set_persisted(parent, "child")
        self.assertTrue(self.pool.update_group("worker", {"name": "jobs"})[0])
        self.assertEqual(self.pool._group_defs["child"]["priority_by_group"], {"jobs": 2, "cron": 1})
        self.assertIn("jobs", n.priority_by_group)
        self.assertNotIn("worker", n.priority_by_group)
        self.assertTrue(self.pool.update_group("child", {"name": "engine"})[0])
        for parent in ("jobs", "cron"):
            self.assertEqual(self.pool._get_current(parent), "engine")
            self.assertEqual(self.pool._get_persisted(parent), "engine")
        self.assertTrue(self.pool.delete_group("jobs")[0])
        self.assertEqual(self.pool._group_defs["engine"]["priority_by_group"], {"cron": 1})
        self.assertTrue(c.in_pool)
        self.assertTrue(self.pool.delete_group("engine")[0])
        self.assertIsNone(self.pool._get_current("cron"))
        self.assertIsNone(self.pool._get_persisted("cron"))

    def test_api_contract_scoped_persistence_and_chain_ids(self):
        self.ep("n", "worker")
        self.ep("c", "child")
        api = self.m.api_handler
        for priority in (None, True, 0, -1, 1.5, "2"):
            self.assertEqual(api("POST", "/api/groups/child/join-group",
                                 {"group": "worker", "priority": priority})[0], 400)
        for parent in ("worker", "cron", "main"):
            status, _, _ = api("POST", "/api/groups/child/join-group", {"group": parent})
            self.assertEqual(status, 200)
            self.assertEqual(api("POST", "/api/groups/child/switch-group", {"group": parent})[0], 200)
        with open(self.m.CONFIG_FILE, encoding="utf-8") as f:
            defs = json.load(f)["pool_group_defs"]
        self.assertTrue(all("main_priority" not in gd for gd in defs))
        self.assertEqual(next(gd for gd in defs if gd["name"] == "child")["priority_by_group"], {"worker": 2, "cron": 1, "main": 1})
        _, body, _ = api("GET", "/api/groups", None)
        groups = {gd["name"]: gd for gd in body["groups"]}
        self.assertTrue(all("priority_by_group" in gd for gd in groups.values()))
        self.assertEqual(groups["worker"]["members"], 2)
        # Ordinary parents avoid the child while main occupies it.
        self.pool._set_current("main", None)
        _, body, _ = api("GET", "/api/chain", None)
        self.assertEqual(body["groups"]["worker"]["current_subgroup"], "child")
        entries = [r for r in body["chain"] if r.get("is_subgroup_entry")]
        self.assertEqual(len(entries), 3)
        self.assertEqual(len({r["id"] for r in entries}), 3)
        for row in entries:
            self.assertEqual(row["subgroup"], "child")
            self.assertEqual(len(row["pool_groups"]), 1)
            parent = row["pool_groups"][0]
            self.assertEqual(row["id"], f"subgroup:{parent}:child")
            self.assertEqual(row["priority_by_group"], {parent: row["priority"]})
        self.assertEqual(api("POST", "/api/groups/child/leave-group", {"group": "worker"})[0], 200)
        state = self.m.load_runtime_state()["groups"]
        self.assertNotIn("worker", state)
        self.assertEqual(state["cron"], "child")
        self.assertEqual(state["main"], "child")

    def test_startup_restores_parent_and_child_pointers_and_main_rank(self):
        self.ep("m1", "main", 1)
        self.ep("m2", "main", 2)
        self.ep("c", "child")
        self.join(parent="main", priority=3)
        self.join()
        self.m._sync_to_config()
        with open(self.m.CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        child_def = next(gd for gd in saved["pool_group_defs"] if gd["name"] == "child")
        child_def.pop("priority_by_group")
        child_def["main_priority"] = 3
        with open(self.m.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(saved, f)
        self.m.save_runtime_state_groups({"main": "child", "child": "c"})
        restored = load_module(self.tmp.name).pool
        self.assertEqual(restored._get_current("main"), "child")
        self.assertEqual(restored._get_current("child"), "c")
        self.assertEqual(restored._joined_subgroups()["child"], 3)
        # Canonical ordinary-parent pointers survive the next startup as well.
        self.assertTrue(restored.set_subgroup_priority("child", "worker")[0])
        restored_module = load_module(self.tmp.name)
        restored_module.pool = restored
        restored_module._sync_to_config()
        self.m.save_runtime_state_groups({"main": "child", "worker": "child", "child": "c"})
        restored = load_module(self.tmp.name).pool
        self.assertEqual(restored._get_current("worker"), "child")
        self.assertEqual(restored._get_current("main"), "child")
        self.assertEqual(restored._get_current("child"), "c")
        self.assertEqual(restored._joined_subgroups()["child"], 3)


if __name__ == "__main__":
    unittest.main()
