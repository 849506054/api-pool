"""推理佐证口径测试（reasoning_detected）。

背景（2026-09-27）：不同上游用不同字段名承载推理——
- step/deepseek：delta.reasoning_content
- OpenRouter 系（Cline 中转的 mimo 等）：delta.reasoning 与
  delta.reasoning_details[]（{type:reasoning.text}）
- 部分上游只把推理量计入 usage.completion_tokens_details.reasoning_tokens，
  不回思考正文

池侧只认单一字段 + 只认文本，会把真实在推理的上游误报为「未见推理」。
本测试锁死统一口径：文本证据或计数证据任一成立即 detected=1。
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_reasoning_detect_{os.getpid()}_{id(threading.current_thread())}"
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


class ReasoningDetectedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.module = load_module(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def detected(self, text, tokens, collected=None):
        return self.module.APIPool._reasoning_detected(text, tokens, collected)

    # ── 文本证据 ──

    def test_text_evidence_detected(self):
        for field_value in ("Let me think", "先想一下", " ", "0"):
            # 空白字符串不算证据（strip 后为空）；其余任意非空文本均算
            expect = 1 if str(field_value).strip() else 0
            self.assertEqual(self.detected(field_value, None), expect, field_value)

    def test_reasoning_content_field_form(self):
        """step/deepseek 形态：delta.reasoning_content 累积进 final_reasoning_text。"""
        self.assertEqual(self.detected("17 - 9 = ?  by step.", None), 1)

    def test_reasoning_field_form(self):
        """OpenRouter 系形态：delta.reasoning 累积进 final_reasoning_text。"""
        self.assertEqual(self.detected("Let me think", None), 1)

    def test_reasoning_details_form(self):
        """reasoning_details[] 形态：text 已提取累积。"""
        self.assertEqual(self.detected("Let me think", None), 1)

    # ── 字段形态识别（_chunk_reasoning_text）──

    def test_chunk_reasoning_content_field(self):
        f = self.module.APIPool._chunk_reasoning_text
        self.assertEqual(f({"reasoning_content": "先想"}), "先想")
        self.assertEqual(f({"reasoning_content": ""}), "")
        self.assertEqual(f({"reasoning_content": None}), "")

    def test_chunk_reasoning_field_openrouter_form(self):
        """OpenRouter 系（Cline 中转的 mimo）用 delta.reasoning 承载思考文本。"""
        f = self.module.APIPool._chunk_reasoning_text
        self.assertEqual(f({"reasoning": "Let me think"}), "Let me think")
        self.assertEqual(f({"reasoning": None}), "")

    def test_chunk_reasoning_details_form(self):
        f = self.module.APIPool._chunk_reasoning_text
        self.assertEqual(
            f({"reasoning_details": [{"type": "reasoning.text", "text": "分步"}]}),
            "分步",
        )
        # 非 reasoning.text 类型的条目不计入
        self.assertEqual(
            f({"reasoning_details": [{"type": "reasoning.encrypted", "data": "xx"}]}),
            "",
        )
        # 多段拼接
        self.assertEqual(
            f({"reasoning_details": [
                {"type": "reasoning.text", "text": "a"},
                {"type": "reasoning.text", "text": "b"},
            ]}),
            "ab",
        )

    def test_chunk_reasoning_priority_order(self):
        """reasoning_content 优先于 reasoning（同帧并存时不重复累积）。"""
        f = self.module.APIPool._chunk_reasoning_text
        self.assertEqual(
            f({"reasoning_content": "A", "reasoning": "B",
               "reasoning_details": [{"type": "reasoning.text", "text": "C"}]}),
            "A",
        )

    def test_chunk_reasoning_bad_input(self):
        f = self.module.APIPool._chunk_reasoning_text
        self.assertEqual(f(None), "")
        self.assertEqual(f("not a dict"), "")
        self.assertEqual(f({}), "")
        self.assertEqual(f({"reasoning_details": "not a list"}), "")
        # 非字符串的 reasoning 值不得抛异常
        self.assertEqual(f({"reasoning": {"nested": 1}}), "")

    # ── 计数证据（无文本但 usage 报了推理 token）──

    def test_token_evidence_detected(self):
        """上游只报 reasoning_tokens、不回思考正文（mimo/Cline 中转）→ 仍应 detected=1。"""
        self.assertEqual(self.detected("", 15), 1)
        self.assertEqual(self.detected("", 1305), 1)
        self.assertEqual(self.detected(None, 1), 1)

    def test_token_zero_is_not_evidence(self):
        self.assertEqual(self.detected("", 0), 0)
        self.assertEqual(self.detected(None, 0), 0)
        self.assertEqual(self.detected("", None), 0)

    def test_token_bad_type_is_not_evidence(self):
        """上游回非数字（None/字符串）不得抛异常，按无证据处理。"""
        self.assertEqual(self.detected("", "abc"), 0)
        self.assertEqual(self.detected("", [1]), 0)

    def test_text_wins_over_zero_tokens(self):
        """有文本但计数为 0（step 实测）：文本证据成立，仍 detected=1。"""
        self.assertEqual(self.detected("先想一下", 0), 1)

    # ── 三态：没采集到（2026-09-27）──
    #
    # 背景：有 usage 不等于采到了有效响应。上游提前终止时 usage 齐全但
    # completion/reasoning 双空（实测某 8 分钟窗口 19 条 step 记录里 13 条
    # 空 completion）。旧口径把这些记成 detected=0，界面显示「✗未见推理」，
    # 把「没抓到」伪装成「确实没推理」。现在必须记 None → 界面 —。

    def test_not_collected_returns_none(self):
        self.assertIsNone(self.detected("", 0, collected=False))
        self.assertIsNone(self.detected(None, None, collected=False))

    def test_not_collected_overrides_positive_evidence(self):
        """调用方明确说没采到时，即使有残留证据也不得记 1。"""
        self.assertIsNone(self.detected("有思考内容", 99, collected=False))

    def test_default_collected_keeps_old_behaviour(self):
        """不传 collected 视为采集成功，老调用点行为不变。"""
        self.assertEqual(self.detected("先想一下", 0), 1)
        self.assertEqual(self.detected("", 0), 0)

    def test_collected_true_is_same_as_default(self):
        self.assertEqual(self.detected("", 0, collected=True), 0)
        self.assertEqual(self.detected("想", 0, collected=True), 1)


if __name__ == "__main__":
    unittest.main()
