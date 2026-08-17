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
        spec = importlib.util.spec_from_file_location("api_pool_server_test_module", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class Endpoint:
    def __init__(self, model="", base_url="", tool_call_id_prefix=""):
        self.model = model
        self.base_url = base_url
        self.tool_call_id_prefix = tool_call_id_prefix


class NonDeepSeekCompatibilityTests(unittest.TestCase):
    def test_non_deepseek_messages_drop_deepseek_reasoning_fields(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            history = [
                {
                    "role": "assistant",
                    "content": "ok",
                    "reasoning_content": "private reasoning",
                    "reasoning_text": "private reasoning text",
                }
            ]

            result = module.APIPool._messages_for_endpoint(
                history,
                Endpoint(model="gpt-5.6-sol", base_url="https://provider.example/v1"),
            )

            self.assertIsNot(result, history)
            self.assertNotIn("reasoning_content", result[0])
            self.assertNotIn("reasoning_text", result[0])
            self.assertEqual(history[0]["reasoning_content"], "private reasoning")
            self.assertEqual(history[0]["reasoning_text"], "private reasoning text")

    def test_deepseek_messages_preserve_reasoning_fields(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            history = [{"role": "assistant", "reasoning_content": "keep me"}]

            result = module.APIPool._messages_for_endpoint(
                history, Endpoint(model="deepseek-v4-flash")
            )

            self.assertIs(result, history)
            self.assertEqual(result[0]["reasoning_content"], "keep me")

    def test_chat_falls_back_to_non_deepseek_endpoint_with_target_model(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            deepseek = module.Endpoint(
                id="deepseek",
                name="deepseek",
                base_url="https://deepseek.example/v1",
                model="deepseek-v4-flash",
                priority=1,
                in_pool=True,
                use_proxy=False,
            )
            fallback = module.Endpoint(
                id="fallback",
                name="fallback",
                base_url="https://provider.example/v1",
                model="gpt-5.6-sol",
                priority=2,
                in_pool=True,
                use_proxy=False,
            )
            pool = module.APIPool([deepseek, fallback])
            attempts = []

            def fake_try(ep, payload, timeout, **kwargs):
                attempts.append((ep.id, payload))
                if ep.id == "deepseek":
                    return None, "HTTP 502: simulated gateway failure"
                return {"model": ep.model, "choices": [{"message": {"content": "ok"}}]}, ""

            pool._try_endpoint = fake_try
            history = [
                {
                    "role": "assistant",
                    "content": "previous answer",
                    "reasoning_content": "deepseek reasoning",
                    "reasoning_text": "deepseek reasoning text",
                },
                {"role": "user", "content": "continue"},
            ]

            result = pool.chat(history)

            self.assertEqual([attempt[0] for attempt in attempts], ["deepseek", "fallback"])
            self.assertEqual(attempts[0][1]["model"], "deepseek-v4-flash")
            self.assertIn("reasoning_content", attempts[0][1]["messages"][0])
            self.assertEqual(attempts[1][1]["model"], "gpt-5.6-sol")
            self.assertNotIn("reasoning_content", attempts[1][1]["messages"][0])
            self.assertNotIn("reasoning_text", attempts[1][1]["messages"][0])
            self.assertEqual(history[0]["reasoning_content"], "deepseek reasoning")
            self.assertEqual(result["model"], "gpt-5.6-sol")

    def test_tool_call_prefix_marks_deepseek_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            history = [{"role": "assistant", "reasoning_text": "keep me"}]

            result = module.APIPool._messages_for_endpoint(
                history,
                Endpoint(model="custom-model", tool_call_id_prefix="call_00_ET_"),
            )

            self.assertIs(result, history)
            self.assertEqual(result[0]["reasoning_text"], "keep me")


if __name__ == "__main__":
    unittest.main()
