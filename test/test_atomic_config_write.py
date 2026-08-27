import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_server_atomic_config_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class AtomicConfigWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.module = load_module(self.tmp_dir.name)
        self.config_file = os.path.join(self.tmp_dir.name, "api_config.json")
        self.tmp_file = os.path.join(self.tmp_dir.name, ".api_config.json.tmp")
        self.module.CONFIG_FILE = self.config_file  # type: ignore[attr-defined]

    def test_save_config_writes_complete_json(self):
        endpoints = [{"id": "one", "name": "primary"}]

        self.module.save_config(endpoints)

        with open(self.config_file, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"api_endpoints": endpoints})
        self.assertFalse(os.path.exists(self.tmp_file))

    def test_replace_failure_preserves_existing_config_and_cleans_tmp(self):
        original = {"api_endpoints": [{"id": "original"}]}
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(original, f)

        with mock.patch.object(self.module.os, "replace", side_effect=OSError("replace failed")), self.assertRaisesRegex(OSError, "replace failed"):
            self.module.save_config([{"id": "replacement"}])

        with open(self.config_file, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), original)
        self.assertFalse(os.path.exists(self.tmp_file))

    def test_concurrent_writes_leave_complete_config(self):
        payloads = [[{"id": f"endpoint-{index}", "value": "x" * 1000}] for index in range(12)]
        errors = []

        def save(payload):
            try:
                self.module.save_config(payload)
            except OSError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=save, args=(payload,)) for payload in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        with open(self.config_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertIn(saved["api_endpoints"], payloads)
        self.assertFalse(os.path.exists(self.tmp_file))


if __name__ == "__main__":
    unittest.main()
