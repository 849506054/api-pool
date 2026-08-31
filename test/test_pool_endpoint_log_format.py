"""池内端点日志格式测试。"""

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
        name = f"api_pool_log_format_test_{os.getpid()}_{id(tmp_path)}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class PoolEndpointLogFormatTests(unittest.TestCase):
    def test_endpoint_label_without_and_with_model(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = module.Endpoint(name="Opencode", model="deepseek-v4-flash")
            self.assertEqual(module.APIPool._endpoint_log_label(ep, "api-pool-bg"), "[api-pool-bg]Opencode")
            self.assertEqual(
                module.APIPool._endpoint_log_label(ep, "api-pool-bg", ep.model),
                "[api-pool-bg]Opencode: deepseek-v4-flash",
            )

    def test_request_and_success_logs_use_unified_format(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = module.Endpoint(
                id="opencode", name="Opencode", api_key="k", site_id="s",
                model="deepseek-v4-flash", priority=1, in_pool=True,
                pool_groups=["api-pool-bg"],
            )
            pool = module.APIPool([ep])
            pool._group_defs["api-pool-bg"] = {"type": "mixed", "model": "api-pool-bg"}
            pool._try_endpoint = lambda *args, **kwargs: ({"choices": [{"message": {"content": "ok"}}]}, "")
            logs = []
            original = module.sys_log
            setattr(module, "sys_log", lambda msg, level="INFO": logs.append((msg, level)))
            try:
                pool.chat([{"role": "user", "content": "hi"}], model="api-pool-bg", request_id="reqtest")
            finally:
                setattr(module, "sys_log", original)
            self.assertIn(
                ("[req=reqtest] 收到 API 请求，尝试请求端点 '[api-pool-bg]Opencode: deepseek-v4-flash'", "INFO"),
                logs,
            )
            self.assertIn(("[req=reqtest] 端点 '[api-pool-bg]Opencode' 请求成功", "INFO"), logs)
            self.assertTrue(all("延迟: 正常" not in msg for msg, _ in logs))


if __name__ == "__main__":
    unittest.main()
