"""Runtime log levels retain request/terminal/error anchors and gate verbose detail."""
import ast
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(os.environ.get("API_POOL_TEST_SOURCE", Path(__file__).resolve().parents[1] / "api_pool_server.py"))


class RuntimeLogNoiseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="runtime-log-noise-")
        old_cwd = os.getcwd()
        old_db = os.environ.get("API_POOL_DB_DIR")
        try:
            os.chdir(cls.tmp.name)
            os.environ["API_POOL_DB_DIR"] = cls.tmp.name
            spec = importlib.util.spec_from_file_location("pool_runtime_log_noise", SOURCE)
            assert spec is not None and spec.loader is not None
            cls.mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = cls.mod
            spec.loader.exec_module(cls.mod)
        finally:
            os.chdir(old_cwd)
            if old_db is None:
                os.environ.pop("API_POOL_DB_DIR", None)
            else:
                os.environ["API_POOL_DB_DIR"] = old_db
        cls.tree = ast.parse(SOURCE.read_text())
        cls.calls = [node for node in ast.walk(cls.tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Name) and node.func.id == "sys_log"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.mod.sys_logger.clear_logs()
        self.mod._DEBUG_LOGGING = False
        self.mod.pool = self.mod.APIPool([])

    def emit(self, text, level="INFO", group=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mod.sys_log(text, level, group=group)
        return buf.getvalue()

    def matching(self, needle):
        return [call for call in self.calls if call.args and needle in ast.unparse(call.args[0])]

    def test_debug_disabled_omits_stdout_and_ring(self):
        self.assertEqual(self.emit("debug detail", "DEBUG"), "")
        self.assertEqual(self.mod.sys_logger.history, [])

    def test_debug_enabled_visible_in_stdout_and_ring(self):
        self.mod._DEBUG_LOGGING = True
        self.assertIn("debug detail", self.emit("debug detail", "DEBUG"))
        self.assertEqual([item["level"] for item in self.mod.sys_logger.history], ["DEBUG"])

    def test_debug_enabled_preserves_group_ring_filter_and_stdout(self):
        self.mod._DEBUG_LOGGING = True
        self.mod.pool._group_defs["main"]["log_level"] = "error"
        self.assertIn("debug detail", self.emit("debug detail", "DEBUG", "main"))
        self.assertEqual(self.mod.sys_logger.history, [])
        self.emit("warning", "WARN", "main")
        self.assertEqual([item["level"] for item in self.mod.sys_logger.history], ["WARN"])
        self.mod.pool._group_defs["main"]["log_level"] = "silent"
        self.assertIn("failure", self.emit("failure", "ERROR", "main"))
        self.assertEqual(len(self.mod.sys_logger.history), 1)

    def test_info_warning_error_anchors_visible_with_debug_off(self):
        for level, text in (("INFO", "请求成功"), ("INFO", "流式响应结束"),
                            ("WARN", "降级"), ("ERROR", "失败")):
            self.assertIn(text, self.emit(text, level))
        self.assertEqual([item["level"] for item in self.mod.sys_logger.history], ["INFO", "INFO", "WARN", "ERROR"])

    def test_verbose_business_events_use_debug(self):
        for needle in ("Responses 图片", "图片原样转发", "敏感字过滤命中", "health_mode=none", "终极兜底锁定中"):
            with self.subTest(needle=needle):
                calls = self.matching(needle)
                # Startup summary is a configuration anchor, separate from the guard detail.
                if needle == "health_mode=none":
                    calls = [call for call in calls if "池自身出站" in ast.unparse(call)]
                self.assertTrue(calls, needle)
                self.assertTrue(all(isinstance(call.args[1], ast.Constant) and call.args[1].value == "DEBUG"
                                    for call in calls), [ast.unparse(call) for call in calls])

    def test_stream_opening_success_level_depends_on_actual_generator(self):
        calls = [call for call in self.matching("请求成功") if "request_tag" in ast.unparse(call)]
        self.assertEqual(len(calls), 1)
        level = calls[0].args[1]
        self.assertIsInstance(level, ast.IfExp)
        self.assertEqual(level.body.value, "DEBUG")
        self.assertEqual(level.orelse.value, "INFO")
        self.assertIn("GeneratorType", ast.unparse(level.test))
        for needle in ("流式响应结束", "收到 API 请求", "启动图片解析"):
            self.assertTrue(self.matching(needle))
            self.assertTrue(all(call.args[1].value == "INFO" for call in self.matching(needle)))

    def test_image_log_scans_guarded_by_debug_flag(self):
        for needle in ("图片原样转发", "Responses 图片"):
            for call in self.matching(needle):
                guarded = [node for node in ast.walk(self.tree) if isinstance(node, ast.If)
                           and "_DEBUG_LOGGING" in ast.unparse(node.test) and call in list(ast.walk(node))]
                self.assertTrue(guarded, needle)


if __name__ == "__main__":
    unittest.main()
