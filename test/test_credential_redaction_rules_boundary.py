"""Offline scanner boundaries using synthetic credentials and local source imports."""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from credential_redaction import redact_body, redact_text


class JsonBoundaryTests(unittest.TestCase):
    def test_reasoning_text_alias_is_scanned(self):
        body = {'messages': [{'role': 'assistant',
                             'reasoning_text': 'password: fixture-secret',
                             'reasoning_content': 'token: fixture-token'}]}
        clean = redact_body(body)
        self.assertNotIn('fixture-secret', str(clean))
        self.assertNotIn('fixture-token', str(clean))
        self.assertEqual(body['messages'][0]['reasoning_text'], 'password: fixture-secret')

    def test_arbitrary_json_type_values(self):
        for tag in (['string', 'null'], {'name': 'result'}, None, 7, True):
            data = {'type': tag, 'password': 'fixture-secret', 'ordinary': [None, False, 3]}
            for content in (data, json.dumps(data)):
                with self.subTest(tag=tag, encoded=isinstance(content, str)):
                    original = {'messages': [{'role': 'tool', 'content': content}]}
                    before = copy.deepcopy(original)
                    result = redact_body(original)['messages'][0]['content']
                    result = json.loads(result) if isinstance(result, str) else result
                    self.assertEqual(result, {**data, 'password': '[REDACTED_PASSWORD]'})
                    self.assertEqual(original, before)

    def test_business_types_are_data_in_all_tool_and_metadata_locations(self):
        for tag in ('file', 'image', 'image_url', 'input_image', 'audio', 'document', 'redacted_thinking'):
            data = {'type': tag, 'password': 'fixture-secret',
                    'nested': {'type': 'file', 'api_key': 'fixture-key'}}
            bodies = [
                {'messages': [{'role': 'tool', 'content': data}]},
                {'messages': [{'role': 'tool', 'content': json.dumps(data)}]},
                {'messages': [{'role': 'assistant', 'tool_calls': [{'type': 'function',
                    'function': {'name': 'read', 'arguments': json.dumps(data)}}]}]},
                {'messages': [{'role': 'assistant', 'content': [{'type': 'tool_use',
                    'id': 'call_fixed', 'name': 'read', 'input': data}]}]},
                {'messages': [{'role': 'user', 'content': [{'type': 'tool_result',
                    'tool_use_id': 'call_fixed', 'content': data}]}]},
                {'input': [{'type': 'function_call_output', 'call_id': 'call_fixed', 'output': data}]},
                {'contents': [{'parts': [{'functionResponse': {'name': 'read', 'response': data}}]}]},
                {'metadata': data},
            ]
            for body in bodies:
                with self.subTest(tag=tag, body=body):
                    clean = redact_body(body)
                    self.assertNotIn('fixture-secret', json.dumps(clean))
                    self.assertNotIn('fixture-key', json.dumps(clean))
                    self.assertEqual(clean, redact_body(clean))

    def test_business_binary_named_fields_are_scanned(self):
        body = {'metadata': {'inlineData': {'password': 'fixture-secret'},
                             'signature': 'sk-' + 'A' * 30}}
        clean = redact_body(body)
        self.assertNotIn('fixture-secret', str(clean))
        self.assertNotIn('sk-' + 'A' * 30, str(clean))

    def test_unstructured_data_stays_data_despite_protocol_like_keys(self):
        for data in ({'type': 'file', 'text': 'ordinary', 'password': 'fixture-secret'},
                     {'type': {'name': 'record'}, 'text': 'ordinary', 'password': 'fixture-secret'},
                     {'format': 'plain', 'password': 'fixture-secret'},
                     {'parts': ['ordinary'], 'password': 'fixture-secret'}):
            for content in (data, [data]):
                body = {'messages': [{'role': 'user', 'content': content}]}
                self.assertNotIn('fixture-secret', str(redact_body(body)))

    def test_binary_blocks_preserve_payload_and_scan_extra_business_fields(self):
        token = 'sk-' + 'A' * 30
        block = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + token},
                 'password': 'fixture-secret'}
        body = {'messages': [{'role': 'user', 'content': [block]}]}
        clean = redact_body(body)['messages'][0]['content'][0]
        self.assertEqual(clean['image_url'], block['image_url'])
        self.assertEqual(clean['password'], '[REDACTED_PASSWORD]')

    def test_genuine_binary_protocol_content_stays_exact(self):
        token = 'sk-' + 'A' * 30
        blocks = [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + token}},
                  {'type': 'image', 'source': {'type': 'base64', 'data': token, 'media_type': 'image/png'}},
                  {'type': 'input_file', 'file_data': token},
                  {'type': 'redacted_thinking', 'data': token}]
        body = {'messages': [{'role': 'user', 'content': blocks}],
                'contents': [{'parts': [{'inlineData': {'mimeType': 'image/png', 'data': token},
                                         'thoughtSignature': token}]}]}
        self.assertEqual(redact_body(body), body)
        # The same shape inside explicit business data remains model-visible data.
        clean = redact_body({'metadata': {'copy': blocks[0], 'password': 'fixture-secret'}})
        self.assertNotIn(token, str(clean))
        self.assertNotIn('fixture-secret', str(clean))


class ReferenceBoundaryTests(unittest.TestCase):
    def test_cookie_mixed_reference_without_pairs(self):
        text = 'Cookie: fixture-secret-${SUFFIX}'
        self.assertNotIn('fixture-secret', redact_text(text))

    def test_mixed_shell_reference_values(self):
        for value in ('fixture-secret-${SUFFIX}', 'fixture-secret-$SUFFIX',
                      '${PREFIX}-fixture-secret', '$PREFIX-fixture-secret'):
            for text in (f'PASSWORD={value}', f'--password {value}', f'password: {value}',
                         f'password: "{value}"', f'--password="{value}"'):
                with self.subTest(text=text):
                    clean = redact_text(text)
                    self.assertNotIn('fixture-secret', clean)
                    self.assertEqual(clean, redact_text(clean))

    def test_complete_shell_and_curl_references(self):
        for text in ('PASSWORD=${DB_PASSWORD}', '--password $DB_PASSWORD', 'password: ${PASSWORD}',
                     'curl -u "$USERNAME:$PASSWORD" https://example.invalid',
                     "curl -u '${USERNAME}:${PASSWORD}' https://example.invalid",
                     'curl --user "$USERNAME:$PASSWORD" https://example.invalid'):
            with self.subTest(text=text):
                self.assertEqual(redact_text(text), text)

    def test_curl_mixed_pair_keeps_reference_and_hides_literal(self):
        for text in ('curl -u "$USERNAME:fixture-secret"',
                     'curl -u "fixture-secret:$PASSWORD"',
                     'curl --user "$USERNAME:fixture-secret-${SUFFIX}"'):
            clean = redact_text(text)
            self.assertNotIn('fixture-secret', clean)
            self.assertIn('$', clean)
            self.assertEqual(clean, redact_text(clean))

    def test_complete_local_python_expressions_compile_and_round_trip_json(self):
        expressions = [
            'os.getenv("DB_PASSWORD", "")',
            'os.environ.get("DB_PASSWORD", "")',
            'os.environ["DB_PASSWORD"]',
            'dotenv_values(".env")["DB_PASSWORD"]',
            'os.getenv("DB_PASSWORD", fallback_password)',
            'os.environ.get("DB_PASSWORD", os.getenv("FALLBACK_PASSWORD", ""))',
            'os.getenv("DB_PASSWORD", default="")',
            'os.getenv(\n    "DB_PASSWORD",\n    ""\n)',
        ]
        for expr in expressions:
            for label in ('password', 'PASSWORD'):
                code = f'{label} = {expr}\n'
                with self.subTest(code=code):
                    clean = redact_text(code)
                    compile(clean, '<local-reference>', 'exec')
                    self.assertEqual(clean, code)
                    data = {'command': code, 'password': expr}
                    body = {'messages': [{'role': 'assistant', 'tool_calls': [{'function': {
                        'name': 'run', 'arguments': json.dumps(data)}}]}]}
                    args = redact_body(body)['messages'][0]['tool_calls'][0]['function']['arguments']
                    self.assertEqual(json.loads(args), data)
                    compile(json.loads(args)['command'], '<json-local-reference>', 'exec')

    def test_ordinary_env_defaults_remain_usable(self):
        import types
        for getter in ('os.getenv', 'os.environ.get'):
            for key, fallback in (('TIMEOUT', '30'), ('LOG_LEVEL', 'INFO'),
                                  ('BASE_URL', 'https://example.invalid')):
                code = f'value = {getter}({key!r}, {fallback!r})'
                self.assertEqual(redact_text(code), code)
                body = {'messages': [{'role': 'assistant', 'tool_calls': [{'function': {
                    'name': 'run', 'arguments': json.dumps({'command': code})}}]}]}
                self.assertEqual(redact_body(body), body)
        code = 'timeout = int(os.getenv("TIMEOUT", "30"))'
        clean = redact_text(code)
        scope = {'os': types.SimpleNamespace(getenv=lambda name, default: default)}
        # Execute only the fixed synthetic source above, with a stub getenv.
        exec(compile(clean, '<ordinary-default>', 'exec'), scope)  # noqa: S102
        self.assertEqual(scope['timeout'], 30)
        nested = 'timeout = int(os.getenv("TIMEOUT", os.getenv("FALLBACK_TIMEOUT", "30")))'
        self.assertEqual(redact_text(nested), nested)

    def test_explicit_credential_context_and_known_token_defaults_still_redact(self):
        token = 'ghp_' + 'B' * 30
        for code, secret in (
            ('password = os.getenv("SETTING", "fixture-secret")', 'fixture-secret'),
            ('value = os.getenv("DB_PASSWORD", "fixture-secret")', 'fixture-secret'),
            ('value = os.getenv("SETTING", os.getenv("DB_PASSWORD", "fixture-secret"))', 'fixture-secret'),
            (f'value = os.getenv("SETTING", "{token}")', token),
            (f'value = os.getenv("SETTING", os.getenv("FALLBACK", "{token}"))', token),
        ):
            clean = redact_text(code)
            self.assertNotIn(secret, clean)
            compile(clean, '<credential-default>', 'exec')
            self.assertEqual(clean, redact_text(clean))

    def test_literal_defaults_are_redacted_and_python_remains_valid(self):
        for getter in ('os.getenv', 'os.environ.get'):
            for fallback in ('"fixture-secret"', '"fixture-secret-$SUFFIX"',
                             '"fixture-secret" + local_suffix', '"fixture-secret".strip()',
                             'os.getenv("FALLBACK_PASSWORD", "fixture-secret")'):
                code = f'password = {getter}("DB_PASSWORD", {fallback})\n'
                clean = redact_text(code)
                self.assertNotIn('fixture-secret', clean)
                compile(clean, '<redacted-default>', 'exec')
                self.assertEqual(clean, redact_text(clean))

    def test_python_composed_reference_hides_literal_only(self):
        for code in ('PASSWORD=os.environ["DB_PASSWORD"] + "fixture-secret"',
                     'password = os.getenv("DB_PASSWORD", "") + "fixture-secret" + local_suffix'):
            clean = redact_text(code)
            compile(clean, '<mixed-local-expression>', 'exec')
            self.assertNotIn('fixture-secret', clean)
            self.assertIn('DB_PASSWORD', clean)
            self.assertEqual(clean, redact_text(clean))
        code = 'headers = {"Authorization": "Bearer " + os.environ["API_TOKEN"]}'
        self.assertEqual(redact_text(code), code)
        compile(redact_text(code), '<composed-authorization>', 'exec')

    def test_reference_names_protect_only_their_own_spans(self):
        text = 'PASSWORD=os.environ["DB_PASSWORD"]\nPASSWORD="DB_PASSWORD"\nAPI_KEY=fixture-secret-${SUFFIX}'
        clean = redact_text(text)
        self.assertIn('os.environ["DB_PASSWORD"]', clean)
        self.assertIn('PASSWORD="[REDACTED_PASSWORD]"', clean)
        self.assertNotIn('fixture-secret', clean)
        token = 'ghp_' + 'B' * 30
        for text in (f'PASSWORD=${{{token}}}', f'password = os.getenv("{token}")'):
            self.assertNotIn(token, redact_text(text))


class CjkInlineLabelTests(unittest.TestCase):
    def test_inline_cjk_labels_redact_ascii_values(self):
        cases = (
            '账号 svc-acct-9f3a，密码 pw-9Kd2026#v1',
            '用户名 zhangsan01 密码 Aa12345678',
            '登录账号 ops@example.invalid 口令 Zx9!qwe',
            '令牌 abcdef1234567890',
        )
        for text in cases:
            clean = redact_text(text)
            with self.subTest(text=text):
                for secret in ('svc-acct-9f3a', 'pw-9Kd2026#v1', 'zhangsan01', 'Aa12345678',
                               'ops@example.invalid', 'Zx9!qwe', 'abcdef1234567890'):
                    self.assertNotIn(secret, clean)
                self.assertIn('[REDACTED_', clean)
                self.assertEqual(clean, redact_text(clean))

    def test_inline_cjk_labels_leave_prose_and_references(self):
        for text in ('密码 忘记了，请联系管理员',
                     '账号 密码 都没有填写',
                     '账号 $DB_USER，密码 $DB_PASSWORD',
                     '密码 请从 .env 文件读取'):
            self.assertEqual(redact_text(text), text)

    def test_inline_cjk_labels_redact_inside_tool_arguments(self):
        arguments = json.dumps({'command': 'curl 登录时使用 账号 svc-acct-77 密码 pw-77Aa'})
        body = {'messages': [{'role': 'assistant', 'tool_calls': [{'function': {
            'name': 'run', 'arguments': arguments}}]}]}
        clean = redact_body(body)['messages'][0]['tool_calls'][0]['function']['arguments']
        self.assertNotIn('svc-acct-77', clean)
        self.assertNotIn('pw-77Aa', clean)
        self.assertIsInstance(json.loads(clean), dict)


class YamlBoundaryTests(unittest.TestCase):
    def test_credential_block_styles_indents_and_chomping(self):
        for marker in ('|', '>', '|-', '>+', '|2', '>2-', '|-2'):
            for indent in ('', '  '):
                for eol in ('\n', '\r\n'):
                    text = eol.join([indent + 'DB_PASSWORD: ' + marker + ' # local credential',
                                     '', indent + '  fixture-secret', indent + '  second-line',
                                     '', indent + 'next: safe', ''])
                    clean = redact_text(text)
                    self.assertNotIn('fixture-secret', clean)
                    self.assertNotIn('second-line', clean)
                    self.assertIn(indent + 'DB_PASSWORD: ' + marker + ' # local credential' + eol, clean)
                    self.assertIn(indent + 'next: safe' + eol, clean)
                    self.assertEqual(clean, redact_text(clean))

    def test_yaml_quoted_and_sequence_keys_preserve_sibling(self):
        for key in ('"DB_PASSWORD"', "'password'", '密码'):
            text = f'- {key}: >-\n    fixture-secret\n    second-line\n  next: safe\n- ordinary: 1\n'
            clean = redact_text(text)
            self.assertNotIn('fixture-secret', clean)
            self.assertNotIn('second-line', clean)
            self.assertIn('  next: safe\n- ordinary: 1\n', clean)
            self.assertEqual(clean, redact_text(clean))

    def test_yaml_plain_shapes_and_empty_blocks(self):
        for text in ('name: ordinary\nnotes: |\n  first\n  second\nnext: safe\n',
                     'password: |\nnext: safe\n', 'password: >-\n\nnext: safe\n'):
            self.assertEqual(redact_text(text), text)

    def test_yaml_block_inside_json_tool_arguments(self):
        yaml = 'DB_PASSWORD: |\n  fixture-secret\n  second-line\nnext: safe\n'
        body = {'messages': [{'role': 'assistant', 'tool_calls': [{'function': {
            'name': 'write', 'arguments': json.dumps({'content': yaml})}}]}]}
        clean = redact_body(body)
        content = json.loads(clean['messages'][0]['tool_calls'][0]['function']['arguments'])['content']
        self.assertNotIn('fixture-secret', content)
        self.assertNotIn('second-line', content)
        self.assertIn('next: safe\n', content)


if __name__ == '__main__':
    unittest.main()
