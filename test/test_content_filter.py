"""ContentFilter 单元测试。

测试使用占位词（token_a / foo_bar_baz 等），不包含真实敏感词。
真实词典验证见 test_content_filter_smoke：只检查 status 元信息，不打印原文。
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "api_pool_server.py")


def load_module():
    spec = importlib.util.spec_from_file_location("api_pool_content_filter_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = load_module()


class ContentFilterTests(unittest.TestCase):
    def _make_filter(self, dictionary, enabled=True, version="test", targets=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "content_filter.json")
        section = {
            "enabled": enabled,
            "dictionary_version": version,
            "dictionary": dictionary,
        }
        if targets is None:
            # 测试默认打开全部已知范围，便于逐项断言
            targets = list(module.ContentFilter.KNOWN_TARGETS)
        section["targets"] = targets
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"content_filter": section}, f, ensure_ascii=False)
        return module.ContentFilter(file_path=path)

    def test_string_content_replaced(self):
        f = self._make_filter({"token_a": "token a"})
        payload = {"model": "m", "messages": [{"role": "user", "content": "do token_a test"}]}
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        self.assertEqual(cleaned["messages"][0]["content"], "do token a test")
        self.assertEqual(stats["matched"], 1)
        self.assertTrue(stats["enabled"])
        self.assertEqual(stats["dictionary_version"], "test")
        # 原 payload 不被修改（深拷贝）
        self.assertEqual(payload["messages"][0]["content"], "do token_a test")

    def test_multimodal_text_replaced_image_preserved(self):
        f = self._make_filter({"token_a": "token a"})
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look at token_a"},
                        {"type": "image_url", "image_url": {"url": "http://img/x.png"}},
                    ],
                }
            ]
        }
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        content = cleaned["messages"][0]["content"]
        self.assertEqual(content[0]["text"], "look at token a")
        self.assertEqual(content[1], {"type": "image_url", "image_url": {"url": "http://img/x.png"}})
        self.assertEqual(stats["matched"], 1)

    def test_tool_role_content_replaced(self):
        f = self._make_filter({"token_a": "token a"})
        payload = {
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "result contains token_a"},
            ]
        }
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        self.assertEqual(cleaned["messages"][2]["content"], "result contains token a")
        self.assertEqual(stats["matched"], 1)

    def test_empty_dictionary_fast_path_same_object(self):
        f = self._make_filter({}, enabled=True)
        payload = {"messages": [{"role": "user", "content": "nothing to filter"}]}
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        self.assertIs(cleaned, payload)
        self.assertEqual(stats["matched"], 0)

    def test_disabled_fast_path_same_object(self):
        f = self._make_filter({"token_a": "token a"}, enabled=False)
        payload = {"messages": [{"role": "user", "content": "token_a stays"}]}
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        self.assertIs(cleaned, payload)
        self.assertEqual(stats["matched"], 0)
        self.assertFalse(stats["enabled"])

    def test_long_word_priority(self):
        f = self._make_filter({"foo_bar_baz": "F", "foo": "X"})
        cleaned, stats = f.filter_payload(
            {"messages": [{"role": "user", "content": "foo_bar_baz"}]}, return_stats=True
        )
        self.assertEqual(cleaned["messages"][0]["content"], "F")
        self.assertEqual(stats["matched"], 1)

    def test_no_double_replacement(self):
        # A->B 替换后不得用 B 再匹配
        f = self._make_filter({"a_b": "a b", "a b": "AB"})
        cleaned, stats = f.filter_payload(
            {"messages": [{"role": "user", "content": "use a_b here"}]}, return_stats=True
        )
        self.assertEqual(cleaned["messages"][0]["content"], "use a b here")
        self.assertEqual(stats["matched"], 1)

    def test_structure_tags_preserved(self):
        f = self._make_filter({"token_a": "token a"})
        payload = {
            "messages": [
                {
                    "role": "tool",
                    "content": "<untrusted_tool_result>\ntoken_a\n</untrusted_tool_result>",
                }
            ]
        }
        cleaned, _ = f.filter_payload(payload, return_stats=True)
        self.assertEqual(
            cleaned["messages"][0]["content"],
            "<untrusted_tool_result>\ntoken a\n</untrusted_tool_result>",
        )

    def test_tools_descriptions_replaced(self):
        f = self._make_filter({"token_a": "token a"})
        payload = {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "search",
                        "description": "search for token_a",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "q": {"type": "string", "description": "query with token_a"}
                            },
                            "required": ["q"],
                        },
                    },
                }
            ],
        }
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        fn = cleaned["tools"][0]["function"]
        self.assertEqual(fn["description"], "search for token a")
        self.assertEqual(fn["parameters"]["properties"]["q"]["description"], "query with token a")
        self.assertEqual(fn["name"], "search")
        self.assertEqual(stats["matched"], 2)

    def test_regex_special_chars_in_word(self):
        # 词含正则特殊字符：转义后仍能匹配且映射不回退 KeyError
        f = self._make_filter({"a.b+c": "safe"})
        cleaned, stats = f.filter_payload(
            {"messages": [{"role": "user", "content": "x a.b+c y"}]}, return_stats=True
        )
        self.assertEqual(cleaned["messages"][0]["content"], "x safe y")
        self.assertEqual(stats["matched"], 1)

    def test_tool_calls_arguments_json_string_replaced(self):
        # 历史 assistant 消息的 tool_calls arguments（JSON 字符串）含敏感词
        f = self._make_filter({"token_a": "token a"})
        payload = {
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "search", "arguments": '{"query": "find token_a now"}'}}
                    ],
                },
            ]
        }
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        args = cleaned["messages"][1]["tool_calls"][0]["function"]["arguments"]
        self.assertIn("token a", args)
        self.assertNotIn("token_a", args)
        self.assertEqual(stats["matched"], 1)

    def test_tool_calls_arguments_object_replaced(self):
        # arguments 为对象形态（某些客户端直接传 dict）
        f = self._make_filter({"token_a": "token a"})
        payload = {
            "messages": [
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "x", "arguments": {"q": "has token_a"}}}
                ]},
            ]
        }
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        self.assertEqual(cleaned["messages"][0]["tool_calls"][0]["function"]["arguments"]["q"], "has token a")
        self.assertEqual(stats["matched"], 1)

    def test_reasoning_content_replaced(self):
        # DeepSeek 等端点要求回传的推理文本
        f = self._make_filter({"token_a": "token a"})
        payload = {
            "messages": [
                {"role": "assistant", "content": "answer", "reasoning_content": "think about token_a"},
            ]
        }
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        self.assertEqual(cleaned["messages"][0]["reasoning_content"], "think about token a")
        self.assertEqual(cleaned["messages"][0]["content"], "answer")
        self.assertEqual(stats["matched"], 1)

    def test_reasoning_text_replaced(self):
        f = self._make_filter({"token_a": "token a"})
        payload = {
            "messages": [
                {"role": "assistant", "content": "answer", "reasoning_text": "note token_a here"},
            ]
        }
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        self.assertEqual(cleaned["messages"][0]["reasoning_text"], "note token a here")
        self.assertEqual(stats["matched"], 1)

    def test_message_name_replaced(self):
        f = self._make_filter({"token_a": "token a"})
        payload = {
            "messages": [
                {"role": "user", "name": "token_a_user", "content": "hi"},
            ]
        }
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        self.assertEqual(cleaned["messages"][0]["name"], "token a_user")
        self.assertEqual(stats["matched"], 1)

    def test_arguments_json_key_not_touched(self):
        # 非全量模式下 JSON key 不替换，只替换值
        f = self._make_filter({"token_a": "token a"}, targets=["messages.tool_call_arguments"])
        payload = {
            "messages": [
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "x", "arguments": '{"token_a": "value token_a"}'}}
                ]},
            ]
        }
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        args = cleaned["messages"][0]["tool_calls"][0]["function"]["arguments"]
        self.assertIn('"token_a": "value token a"', args)
        self.assertEqual(stats["matched"], 1)

    def test_default_targets_are_minimal(self):
        # 未显式配置 targets 时，只开启实测命中的两个范围
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "content_filter.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"content_filter": {"enabled": True, "dictionary": {"token_a": "token a"}}}, f)
        f2 = module.ContentFilter(file_path=path)
        st = f2.status()
        self.assertEqual(st["targets"], sorted(module.ContentFilter.DEFAULT_TARGETS))
        # content 生效
        cleaned, stats = f2.filter_payload(
            {"messages": [{"role": "tool", "content": "has token_a"}]}, return_stats=True
        )
        self.assertEqual(cleaned["messages"][0]["content"], "has token a")
        self.assertEqual(stats["matched"], 1)
        # 默认关闭的范围不生效（reasoning）
        cleaned2, stats2 = f2.filter_payload(
            {"messages": [{"role": "assistant", "content": "x", "reasoning_content": "has token_a"}]},
            return_stats=True,
        )
        self.assertEqual(cleaned2["messages"][0]["reasoning_content"], "has token_a")
        self.assertEqual(stats2["matched"], 0)

    def test_targets_can_be_narrowed(self):
        # 只开 messages.content
        f = self._make_filter({"token_a": "token a"}, targets=["messages.content"])
        payload = {
            "messages": [{"role": "tool", "content": "has token_a"}],
            "tools": [{"type": "function", "function": {"name": "x", "description": "desc token_a"}}],
        }
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        self.assertEqual(cleaned["messages"][0]["content"], "has token a")
        self.assertEqual(cleaned["tools"][0]["function"]["description"], "desc token_a")
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(f.status()["targets"], ["messages.content"])

    def test_known_regions_exclude_unproven_payload_fields(self):
        targets = [
            "messages.content",
            "messages.text_blocks",
            "messages.reasoning",
            "messages.name",
            "messages.tool_call_arguments",
        ]
        f = self._make_filter({"token_a": "token a"}, targets=targets)
        payload = {
            "model": "token_a_model",
            "metadata": "token_a_metadata",
            "messages": [
                {
                    "role": "assistant",
                    "name": "token_a_name",
                    "content": "token_a_content",
                    "reasoning_content": "token_a_reasoning",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "token_a_function",
                                "arguments": json.dumps({"q": "token_a_argument"}),
                            }
                        }
                    ],
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "token_a_tool",
                        "description": "token_a_description",
                        "parameters": {"enum": ["token_a_enum"]},
                    },
                }
            ],
        }
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        message = cleaned["messages"][0]
        self.assertEqual(message["name"], "token a_name")
        self.assertEqual(message["content"], "token a_content")
        self.assertEqual(message["reasoning_content"], "token a_reasoning")
        self.assertIn("token a_argument", message["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "token_a_function")
        self.assertEqual(cleaned["model"], "token_a_model")
        self.assertEqual(cleaned["metadata"], "token_a_metadata")
        self.assertEqual(cleaned["tools"][0]["function"]["description"], "token_a_description")
        self.assertEqual(cleaned["tools"][0]["function"]["parameters"]["enum"], ["token_a_enum"])
        self.assertEqual(stats["matched"], 4)

    def test_unknown_targets_ignored(self):
        f = self._make_filter({"token_a": "token a"},
                              targets=["messages.content", "bogus.target"])
        self.assertEqual(f.status()["targets"], ["messages.content"])

    def test_all_strings_scan_covers_every_position(self):
        # 全量扫描：工具名、参数 key、enum、name、reasoning、arguments 全部覆盖
        f = self._make_filter({"token_a": "token a"}, targets=["all_strings"])
        payload = {
            "model": "m",
            "messages": [
                {"role": "user", "content": "has token_a"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function", "function": {
                        "name": "token_a_tool", "arguments": json.dumps({"q": "val token_a"})}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "result token_a"},
            ],
            "tools": [{"type": "function", "function": {
                "name": "token_a_fn",
                "description": "desc token_a",
                "parameters": {"type": "object", "properties": {
                    "token_a_key": {"type": "string", "enum": ["token_a"], "default": "token_a"}}}}}],
        }
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        self.assertGreaterEqual(stats["matched"], 6)
        # 所有字符串值都不含敏感词
        def walk(n):
            if isinstance(n, str):
                self.assertNotIn("token_a", n)
            elif isinstance(n, dict):
                for v in n.values():
                    walk(v)
            elif isinstance(n, list):
                for it in n:
                    walk(it)
        walk(cleaned)
        # JSON key 保持原样（只替换 value）
        args = cleaned["messages"][1]["tool_calls"][0]["function"]["arguments"]
        self.assertIn('"q": "val token a"', args)

    def test_rules_support_regex_and_literal(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "content_filter.json")
        config = {
            "content_filter": {
                "enabled": True,
                "targets": ["all_strings"],
                "rules": [
                    {
                        "type": "regex",
                        "pattern": r"token[-_]a",
                        "replacement": "token a",
                    },
                    {
                        "type": "literal",
                        "pattern": "fixed prompt.",
                        "replacement": "rewritten prompt.",
                    },
                ],
            }
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f)
        flt = module.ContentFilter(file_path=path)
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": "token-a token_a token a fixed prompt.",
                }
            ]
        }
        cleaned, stats = flt.filter_payload(payload, return_stats=True)
        self.assertEqual(
            cleaned["messages"][0]["content"],
            "token a token a token a rewritten prompt.",
        )
        self.assertEqual(stats["matched"], 3)
        self.assertEqual(flt.status()["word_count"], 2)

    def test_delete_semantics(self):
        # 替换值为空串 = 删除
        f = self._make_filter({"token_a": ""})
        cleaned, stats = f.filter_payload(
            {"messages": [{"role": "user", "content": "drop token_a now"}]}, return_stats=True
        )
        self.assertEqual(cleaned["messages"][0]["content"], "drop  now")
        self.assertEqual(stats["matched"], 1)

    def test_missing_file_disables_filter(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "nonexistent.json")
        f = module.ContentFilter(file_path=path)
        self.assertFalse(f.status()["enabled"])
        payload = {"messages": [{"role": "user", "content": "x"}]}
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        self.assertIs(cleaned, payload)
        self.assertEqual(stats["matched"], 0)

    def test_invalid_config_disables_filter(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "content_filter.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{invalid json")
        f = module.ContentFilter(file_path=path)
        self.assertFalse(f.status()["enabled"])

    def test_status_metadata(self):
        f = self._make_filter({"token_a": "token a", "b_c": "b c"}, version="v9")
        st = f.status()
        self.assertTrue(st["enabled"])
        self.assertEqual(st["dictionary_version"], "v9")
        self.assertEqual(st["word_count"], 2)

    def test_reload_updates_dictionary(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "content_filter.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"content_filter": {"enabled": True, "dictionary": {"a_b": "a b"}}}, f, ensure_ascii=False)
        f = module.ContentFilter(file_path=path)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {"content_filter": {"enabled": True, "dictionary": {"c_d": "c d"}}},
                fh,
                ensure_ascii=False,
            )
        self.assertTrue(f.reload())
        cleaned, stats = f.filter_payload(
            {"messages": [{"role": "user", "content": "c_d a_b"}]}, return_stats=True
        )
        self.assertEqual(cleaned["messages"][0]["content"], "c d a_b")
        self.assertEqual(stats["matched"], 1)


class ContentFilterLazyCopyTests(unittest.TestCase):
    """lazy-copy 预扫：无命中时跳过深拷贝，直接复用原请求对象（与未启用路径一致）。

    预扫遍历范围必须与 _apply/_walk_* 完全一致，否则假阴性会漏过滤。
    """

    def _make_filter(self, dictionary, enabled=True, targets=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "content_filter.json")
        section = {
            "enabled": enabled,
            "dictionary_version": "lazy",
            "dictionary": dictionary,
        }
        if targets is None:
            targets = list(module.ContentFilter.KNOWN_TARGETS)
        section["targets"] = targets
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"content_filter": section}, f, ensure_ascii=False)
        return module.ContentFilter(file_path=path)

    def test_no_match_returns_same_object(self):
        f = self._make_filter({"token_a": "token a"})
        payload = {"model": "m", "messages": [{"role": "user", "content": "hello world"}]}
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        self.assertIs(cleaned, payload)  # 未命中：不深拷贝
        self.assertEqual(stats["matched"], 0)
        self.assertEqual(payload["messages"][0]["content"], "hello world")

    def test_match_returns_copy_original_preserved(self):
        f = self._make_filter({"token_a": "token a"})
        payload = {"messages": [{"role": "user", "content": "do token_a test"}]}
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        self.assertIsNot(cleaned, payload)  # 命中：深拷贝路径
        self.assertEqual(cleaned["messages"][0]["content"], "do token a test")
        self.assertEqual(payload["messages"][0]["content"], "do token_a test")
        self.assertEqual(stats["matched"], 1)

    def test_disabled_or_empty_returns_same_object(self):
        f = self._make_filter({"token_a": "token a"}, enabled=False)
        payload = {"messages": [{"role": "user", "content": "has token_a"}]}
        cleaned, _ = f.filter_payload(payload, return_stats=True)
        self.assertIs(cleaned, payload)  # 未启用：不深拷贝（即使有词）
        f2 = self._make_filter({})
        payload2 = {"messages": [{"role": "user", "content": "has token_a"}]}
        cleaned2, _ = f2.filter_payload(payload2, return_stats=True)
        self.assertIs(cleaned2, payload2)  # 空词典：不深拷贝

    def test_pre_scan_matches_every_target_path(self):
        # 每个 target 位置的命中都必须被预扫发现并替换（与 _apply 路径一致）
        f = self._make_filter({"token_a": "token a"})
        payload = {
            "messages": [
                {"role": "user", "content": "c token_a", "name": "n token_a"},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "t token_a"}],
                    "reasoning_content": "r token_a",
                },
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"function": {"name": "x", "arguments": '{"q": "a token_a"}'}},
                        {"function": {"name": "y", "arguments": {"q": "d token_a"}}},
                    ],
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "t",
                        "description": "desc token_a",
                        "parameters": {"properties": {"k": {"description": "p token_a"}}},
                    },
                },
                {"type": "function", "function": {"name": "t2", "description": "desc2"}},
            ],
        }
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        self.assertIsNot(cleaned, payload)
        self.assertGreaterEqual(stats["matched"], 7)
        # 所有目标位置都被替换：原文无残留
        self.assertNotIn("token_a", json.dumps(cleaned, ensure_ascii=False))

    def test_no_match_outside_target_scope_returns_same_object(self):
        # 敏感词出现在 target 范围之外：预扫不得误判为命中（不复制，原样返回）
        f = self._make_filter({"token_a": "token a"}, targets=["messages.content"])
        payload = {
            "model": "token_a_model",
            "messages": [{"role": "user", "content": "hello", "name": "token_a_name"}],
            "tools": [{"type": "function", "function": {"name": "token_a_fn", "description": "token_a_desc"}}],
        }
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        self.assertIs(cleaned, payload)
        self.assertEqual(stats["matched"], 0)

    def test_newline_word_matches_real_string_value(self):
        # 预扫基于真实字符串值（非 JSON 转义文本）：含换行的词必须命中
        word = "line1\nline2"
        f = self._make_filter({word: "joined"})
        payload = {"messages": [{"role": "user", "content": "prefix line1\nline2 suffix"}]}
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(cleaned["messages"][0]["content"], "prefix joined suffix")

    def test_tool_arguments_json_key_only_no_value_match_returns_same_object(self):
        # JSON key 不替换：仅 key 命中不算命中 → 不复制
        f = self._make_filter({"token_a": "token a"}, targets=["messages.tool_call_arguments"])
        payload = {
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"function": {"name": "x", "arguments": '{"token_a": "value ok"}'}}
                    ],
                }
            ]
        }
        cleaned, stats = f.filter_payload(payload, return_stats=True)
        self.assertIs(cleaned, payload)
        self.assertEqual(stats["matched"], 0)


class ContentFilterSmokeTests(unittest.TestCase):
    """真实 production 词典冒烟测试：只检查元信息，不打印原文。"""

    @classmethod
    def setUpClass(cls):
        cls.cf = module.content_filter

    def test_production_dict_loaded(self):
        st = self.cf.status()
        self.assertIsInstance(st["enabled"], bool)
        self.assertGreaterEqual(st["word_count"], 0)
        self.assertIsInstance(st["dictionary_version"], str)

    def test_production_does_not_break_normal_payload(self):
        payload = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}
        cleaned = self.cf.filter_payload(payload)
        self.assertEqual(cleaned["messages"][0]["content"], "hello")


if __name__ == "__main__":
    unittest.main()
