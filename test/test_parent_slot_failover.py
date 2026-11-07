"""Parent slots keep their identity across shared physical endpoint membership."""

import tempfile
import time
import unittest
from unittest.mock import patch

import test_group_join_main as main_tests


class ParentSlotFailoverTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.m = main_tests.load_module(tmp.name)
        self.pool = self.m.APIPool([])
        self.m.pool = self.pool
        self.pool._load_group_defs([
            {"name": "telegram", "type": "mixed"},
            {"name": "pool-gpt6a", "type": "dedicated"},
            {"name": "pool-gpt6.1s", "type": "dedicated"},
        ])
        self.calls = []
        self.failures = set()
        self.pool._try_endpoint = self.attempt
        self.probe = patch.object(self.pool, "_probe_endpoint", side_effect=AssertionError("unexpected probe"))
        self.probe.start()
        self.addCleanup(self.probe.stop)

    def ep(self, name, priorities):
        ep = main_tests.GroupJoinMainTests.endpoint(self.m, name, 1, "test-model", list(priorities))
        ep.priority_by_group = dict(priorities)
        ep.health_mode = "none"
        self.pool._endpoints.append(ep)
        return ep

    def golden(self, parent="main", all_shared=False):
        for name, rank in (("CodeBuddy", 1), ("Tokenrhythm", 2)):
            self.ep(name, {parent: rank})
        self.ag = self.ep("Ag", {parent: 3, "pool-gpt6.1s": 1})
        self.agp = self.ep("AgP", {parent: 4, "pool-gpt6.1s": 2})
        if not all_shared:
            self.agz = self.ep("AgZ", {"pool-gpt6.1s": 3})
        self.p = self.ep("P", {"pool-gpt6a": 1})
        self.z = self.ep("Z", {"pool-gpt6a": 2})
        for child in ("pool-gpt6a", "pool-gpt6.1s"):
            self.assertTrue(self.pool.set_subgroup_priority(child, parent)[0])
        self.pool._set_current(parent, "pool-gpt6a")
        self.pool._set_current("pool-gpt6a", "P")
        self.pool._set_current("pool-gpt6.1s", "Ag")
        self.failures = {"P", "Z", "AgZ"}

    def attempt(self, ep, *args, **kwargs):
        self.calls.append(ep.id)
        if ep.id in self.failures:
            return None, "HTTP 400: content blocked"
        return main_tests.GroupJoinMainTests.ok_try(ep, *args, **kwargs)

    def chat(self, group="main"):
        return self.pool.chat([{"role": "user", "content": "x"}], model=group)

    def test_next_whole_slot_uses_shared_sticky_member(self):
        self.golden()
        result = self.chat()
        self.assertEqual(self.calls, ["P", "Z", "Ag"])
        self.assertEqual(result["choices"][0]["message"]["content"], "from-Ag")
        self.assertEqual(self.pool._get_current("main"), "pool-gpt6.1s")
        self.chat()
        self.assertEqual(self.calls, ["P", "Z", "Ag", "Ag"])

    def test_cold_unique_member_keeps_whole_slot_eligible(self):
        self.golden()
        self.agz._cooldown_until = time.time() + 600
        self.chat()
        self.assertEqual(self.calls, ["P", "Z", "Ag"])

    def test_telegram_uses_same_parent_axis(self):
        self.golden("telegram")
        self.chat("telegram")
        self.assertEqual(self.calls, ["P", "Z", "Ag"])
        self.assertEqual(self.pool._get_current("telegram"), "pool-gpt6.1s")
        self.assertIsNone(self.pool._get_current("main"))

    def test_explicit_all_shared_child_selection(self):
        self.golden(all_shared=True)
        self.assertTrue(self.pool.switch_main_to_subgroup("pool-gpt6.1s")[0])
        self.assertEqual(self.pool._resolve_subgroup_current("pool-gpt6.1s", "main"), "Ag")
        self.chat()
        self.assertEqual(self.calls, ["Ag"])
        self.assertEqual(self.pool._get_current("main"), "pool-gpt6.1s")

    def test_shared_child_exhaustion_then_parent_tail_then_wrap(self):
        self.golden()
        self.ep("Tail", {"main": 7})
        self.failures.update({"Ag", "AgP", "Tail"})
        self.chat()
        self.assertEqual(self.calls, ["P", "Z", "Ag", "AgP", "AgZ", "Tail", "CodeBuddy"])

    def test_shared_member_failure_stays_in_child_and_recovers_as_child(self):
        self.golden()
        self.pool.switch_main_to_subgroup("pool-gpt6.1s")
        self.failures = {"Ag"}
        self.chat()
        self.assertEqual(self.calls, ["Ag", "AgP"])
        self.assertEqual(self.pool._get_current("main"), "pool-gpt6.1s")
        self.assertEqual(self.pool._get_current("pool-gpt6.1s"), "AgP")
        self.assertIn("main", self.ag._recovery_groups)
        self.agp.deferrable = False
        self.pool._background_probe(self.ag)
        self.assertEqual(self.pool._get_current("main"), "pool-gpt6.1s")
        self.assertEqual(self.pool._get_current("pool-gpt6.1s"), "Ag")

    def test_already_tried_native_is_skipped_inside_child(self):
        self.golden()
        self.pool._set_current("main", "Ag")
        self.failures.update({"Ag", "AgP"})
        self.chat()
        self.assertEqual(self.calls, ["Ag", "AgP", "P", "Z", "AgZ", "CodeBuddy"])
        self.assertEqual(len(self.calls), len(set(self.calls)))

    def test_late_shared_success_preserves_manual_parent_selection(self):
        self.golden()
        self.pool.switch_main_to_subgroup("pool-gpt6.1s")

        def attempt(ep, *args, **kwargs):
            self.pool.switch_to_endpoint("Tokenrhythm", group="main")
            return self.attempt(ep, *args, **kwargs)

        self.pool._try_endpoint = attempt
        self.chat()
        self.assertEqual(self.calls, ["Ag"])
        self.assertEqual(self.pool._get_current("main"), "Tokenrhythm")

    def test_deferred_shared_recovery_retains_child_slot(self):
        self.golden()
        self.pool.switch_main_to_subgroup("pool-gpt6.1s")
        self.failures = {"Ag"}
        self.chat()
        self.agp.deferrable = True
        self.pool._background_probe(self.ag)
        self.assertIn("main", self.ag._defer_until_by_group)
        self.agp.deferrable = False
        self.pool._reconcile_deferred()
        self.assertEqual(self.pool._get_current("main"), "pool-gpt6.1s")
        self.assertEqual(self.pool._get_current("pool-gpt6.1s"), "Ag")
        self.assertNotIn("main", self.ag._recovery_slots)

    def test_manual_switch_cancels_shared_recovery_slot(self):
        self.golden()
        self.pool.switch_main_to_subgroup("pool-gpt6.1s")
        self.failures = {"Ag"}
        self.chat()
        self.pool.switch_to_endpoint("Tokenrhythm", group="main")
        self.pool._background_probe(self.ag)
        self.assertEqual(self.pool._get_current("main"), "Tokenrhythm")
        self.assertNotIn("main", self.ag._recovery_slots)

    def test_cooldown_snapshot_restores_shared_slot(self):
        self.golden()
        self.pool.switch_main_to_subgroup("pool-gpt6.1s")
        self.failures = {"Ag"}
        self.chat()
        self.agp.deferrable = False
        self.m._sync_to_config()
        self.assertTrue(self.m.snapshot_runtime_state())
        restored = main_tests.load_module(self.m.os.path.dirname(self.m.CONFIG_FILE))
        self.pool = restored.pool
        ag = self.pool.get_endpoint("Ag")
        self.assertIn("main", ag._recovery_groups)
        self.pool._background_probe(ag)
        self.assertEqual(self.pool._get_current("main"), "pool-gpt6.1s")
        self.assertEqual(self.pool._get_current("pool-gpt6.1s"), "Ag")

    def test_shared_child_switch_propagates_to_other_parent(self):
        self.golden()
        self.pool.set_subgroup_priority("pool-gpt6.1s", "telegram")
        self.pool._set_current("telegram", "pool-gpt6.1s")
        self.pool.switch_main_to_subgroup("pool-gpt6.1s")
        self.failures = {"Ag"}
        epoch = self.pool._get_route_epoch("telegram")
        self.chat()
        self.assertGreater(self.pool._get_route_epoch("telegram"), epoch)
        self.assertEqual(self.pool._get_current("telegram"), "pool-gpt6.1s")
        self.pool._group_defs["main"]["idle_seconds"] = 10
        self.agp._last_activity_by_group["main"] = time.time() - 11
        self.chat("telegram")
        self.assertEqual(self.calls, ["Ag", "AgP", "AgP"])

    def test_late_failure_uses_local_slot_without_overwriting_new_epoch(self):
        self.golden()
        self.pool.switch_main_to_subgroup("pool-gpt6.1s")
        self.failures = {"Ag"}

        def attempt(ep, *args, **kwargs):
            if ep.id == "Ag":
                self.pool.switch_to_endpoint("Tokenrhythm", group="main")
            return self.attempt(ep, *args, **kwargs)

        self.pool._try_endpoint = attempt
        self.chat()
        self.assertEqual(self.calls, ["Ag", "AgP"])
        self.assertEqual(self.pool._get_current("main"), "Tokenrhythm")
        self.assertNotIn("main", self.ag._recovery_groups)

    def test_cold_child_pointer_advances_to_next_parent_slot_at_entry(self):
        self.golden()
        self.p._cooldown_until = self.z._cooldown_until = time.time() + 600
        self.assertEqual(self.pool._group_pointer_target("main"), "pool-gpt6.1s")
        self.chat()
        self.assertEqual(self.calls, ["Ag"])

    def test_two_children_share_member_and_keep_selected_child(self):
        self.golden()
        self.ag.pool_groups.append("pool-gpt6a")
        self.ag.priority_by_group["pool-gpt6a"] = 3
        self.pool._set_current("pool-gpt6a", "Ag")
        self.chat()
        self.assertEqual(self.calls, ["Ag"])
        self.assertEqual(self.pool._get_current("main"), "pool-gpt6a")

    def test_dedicated_direct_request_retains_own_ring(self):
        self.golden()
        self.failures = {"Ag"}
        self.chat("pool-gpt6.1s")
        self.assertEqual(self.calls, ["Ag", "AgP"])
        self.assertEqual(self.pool._get_current("pool-gpt6.1s"), "AgP")
        self.assertEqual(self.pool._get_current("main"), "pool-gpt6a")

    def test_probe_failure_keeps_shared_child_context(self):
        self.golden()
        self.pool.switch_main_to_subgroup("pool-gpt6.1s")

        def attempt(ep, *args, **kwargs):
            self.calls.append(ep.id)
            if ep.id == "Ag":
                return None, "connection reset"
            return main_tests.GroupJoinMainTests.ok_try(ep, *args, **kwargs)

        self.pool._try_endpoint = attempt
        self.agp.health_mode = "models"
        self.pool._probe_endpoint.side_effect = None
        self.pool._probe_endpoint.return_value = (False, "HTTP 502: probe unavailable")
        self.chat()
        self.assertEqual(self.calls, ["Ag", "AgZ"])
        self.assertEqual(self.pool._get_current("main"), "pool-gpt6.1s")
        self.assertEqual(self.agp._recovery_slots.get("main"), "pool-gpt6.1s")

    def test_empty_stream_callback_retains_slot_and_epoch(self):
        self.golden()
        self.pool.switch_main_to_subgroup("pool-gpt6.1s")
        for late in (False, True):
            with self.subTest(late=late):
                self.pool._set_current("main", "pool-gpt6.1s")
                self.pool._set_current("pool-gpt6.1s", "Ag")
                self.ag._cooldown_until = 0
                epoch = self.pool._get_route_epoch("main")
                stream = self.pool._guard_empty_stream(
                    iter([b"data: [DONE]\n\n"]), self.ag, "main", "empty-test", epoch,
                    route_slot="pool-gpt6.1s",
                )
                if late:
                    self.pool.switch_to_endpoint("Tokenrhythm", group="main")
                list(stream)
                self.assertEqual(self.pool._get_current("main"),
                                 "Tokenrhythm" if late else "pool-gpt6.1s")
                if not late:
                    self.assertEqual(self.pool._get_current("pool-gpt6.1s"), "AgP")

    def test_pure_endpoint_parent_keeps_native_ring(self):
        self.ep("A", {"main": 1})
        self.ep("B", {"main": 2})
        self.ep("C", {"main": 3})
        self.pool._set_current("main", "B")
        self.failures = {"B", "C"}
        self.chat()
        self.assertEqual(self.calls, ["B", "C", "A"])
        self.assertEqual(self.pool._get_current("main"), "A")


if __name__ == "__main__":
    unittest.main()
