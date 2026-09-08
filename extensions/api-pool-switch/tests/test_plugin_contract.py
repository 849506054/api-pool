"""插件加载与注册契约 — plugin.yaml / __init__.py / handler 签名一致性。

运行: python3 tests/test_plugin_contract.py
"""

import importlib.util
import os
import sys
import unittest
import yaml

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location("api_pool_switch_contract", os.path.join(PLUGIN_DIR, "__init__.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class ContractTests(unittest.TestCase):
    def test_manifest_roundtrip(self):
        with open(os.path.join(PLUGIN_DIR, "plugin.yaml"), encoding="utf-8") as fh:
            manifest = yaml.safe_load(fh)
        self.assertEqual(manifest["name"], "api-pool-switch")
        self.assertTrue(manifest["version"].startswith("1.3"))
        self.assertTrue(any(c["name"] == "endpoint" for c in manifest["commands"]))

    def test_register_exports(self):
        self.assertIn("register", mod.__all__)
        self.assertTrue(callable(mod.register))
        self.assertIn("handle_endpoint", dir(mod))

    def test_handler_sig_compatible(self):
        """gateway 分发只传一个位置参数 raw_args；context 必须可省略。"""
        import inspect
        sig = inspect.signature(mod.handle_endpoint)
        params = list(sig.parameters.values())
        self.assertLessEqual(len(params), 2)
        self.assertEqual(params[0].name, "raw_args")
        self.assertEqual(params[1].default, None)  # context=None

    def test_menu_async_path_uses_to_thread(self):
        """picker 菜单路径（_run_endpoint_menu/_open_endpoint_menu）的 API 调用必须 to_thread。

        handle_endpoint 的 switch/check/health 分支保持同步 _api_get 是旧代码
        既有模式（gateway 在 async 调用点直接 await 结果），不在此约束内。
        """
        import ast
        tree = ast.parse(open(os.path.join(PLUGIN_DIR, "__init__.py"), encoding="utf-8").read())
        src_lines = open(os.path.join(PLUGIN_DIR, "__init__.py"), encoding="utf-8").read().splitlines()
        offenders = []
        in_menu_fn = False
        for i, line in enumerate(src_lines, 1):
            stripped = line.strip()
            if stripped.startswith(("async def _run_endpoint_menu", "async def _open_endpoint_menu", "async def _open_group_menu", "async def _send_second_level")):
                in_menu_fn = True
            elif stripped.startswith("def ") or stripped.startswith("async def "):
                in_menu_fn = False
            if in_menu_fn and "_api_get(" in stripped and "to_thread" not in stripped and not stripped.startswith("async def"):
                offenders.append((i, stripped))
        self.assertEqual(offenders, [])

    def test_registered_command_exists_in_manifest(self):
        with open(os.path.join(PLUGIN_DIR, "plugin.yaml"), encoding="utf-8") as fh:
            manifest = yaml.safe_load(fh)
        cmd_names = {c["name"] for c in manifest["commands"]}
        self.assertIn("endpoint", cmd_names)


if __name__ == "__main__":
    unittest.main()
