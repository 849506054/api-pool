"""Synthetic credential fixtures; all I/O uses scratch and loopback servers."""
import copy
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path('/opt/data/cache/scratch/apipool-credential-redaction/backend')
SCRATCH.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))


def load_pool(directory):
    source = Path(os.environ.get('API_POOL_TEST_SOURCE', ROOT / 'api_pool_server.py'))
    spec = importlib.util.spec_from_file_location('credential_pool_fixture', source)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    cwd = os.getcwd()
    original_open = open

    def isolated_open(file, *args, **kwargs):
        path = Path(file).resolve()
        if path.name == 'content_filter.json':
            raise FileNotFoundError('isolated private configuration')
        if path.name == '.env' or (path.suffix in ('.json', '.db') and not path.is_relative_to(SCRATCH)):
            raise AssertionError('fixture attempted private file access')
        return original_open(file, *args, **kwargs)

    try:
        os.chdir(directory)
        with mock.patch.dict(os.environ, {'API_POOL_DB_DIR': str(directory)}), \
                mock.patch.object(threading.Thread, 'start'), \
                mock.patch('builtins.open', side_effect=isolated_open):
            spec.loader.exec_module(mod)
        mod.CONFIG_FILE = str(directory / 'api_config.json')
        mod.RUNTIME_STATE_FILE = str(directory / 'api_runtime_state.json')
    finally:
        os.chdir(cwd)
    return mod


class GoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        cls.mod = load_pool(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_default_management_contract(self):
        self.assertEqual(self.mod.api_handler('GET', '/api/credential-redaction', {}),
                         (200, {'credential_redaction': {'enabled': False}}, False))

    def test_golden_one_way_values(self):
        from credential_redaction import redact_body
        original = {'model': 'sk-model-should-stay', 'messages': [{'role': 'user',
                    'content': '账号: fixture-user 密码: "fixture-password" API_KEY=fixture-api-value'}]}
        source = copy.deepcopy(original)
        redacted = redact_body(original)
        text = redacted['messages'][0]['content']
        for secret in ('fixture-user', 'fixture-password', 'fixture-api-value'):
            self.assertNotIn(secret, text)
        self.assertEqual(source, original)
        self.assertEqual(redacted['model'], original['model'])
        self.assertEqual(redact_body(redacted), redacted)


USAGE = {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}
SECRET = 'fixture-password-937!'


def fixture_payload(stream=False):
    return {'model': 'm', 'stream': stream, 'messages': [
        {'role': 'system', 'content': 'PASSWORD="' + SECRET + '"'},
        {'role': 'assistant', 'content': 'thinking password: ' + SECRET,
         'reasoning_content': 'token: fixture-reasoning-token',
         'tool_calls': [{'id': 'call_fixed', 'type': 'function', 'function': {
             'name': 'read_credentials', 'arguments': json.dumps({
                 'password': SECRET, 'username': 'fixture-account',
                 'nested': {'api_key': 'fixture-args-key'},
                 'command': 'password = os.environ["DB_PASSWORD"]'})}}]},
        {'role': 'tool', 'tool_call_id': 'call_fixed',
         'content': json.dumps({'password': SECRET, 'result': 'ordinary'})},
        {'role': 'user', 'content': [{'type': 'text', 'text': '密码：' + SECRET}]}],
        'tools': [{'type': 'function', 'function': {'name': 'read_credentials',
            'description': 'API_KEY=fixture-description-key', 'parameters': {
                'type': 'object', 'properties': {'password': {'type': 'string',
                    'description': 'password: ' + SECRET, 'enum': ['password', 'token']}},
                'required': ['password']}}}],
        'metadata': {'password': SECRET, 'id': 'fixed-uuid', 'note': 'token: fixture-meta-token'}}


class WireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        cls.mod = load_pool(Path(cls.tmp.name))
        cls.captures = []
        cls.responder = None

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                body = json.loads(raw)
                cls.captures.append((self.path, raw, dict(self.headers)))
                if cls.responder:
                    custom = cls.responder(self.path, body, len(cls.captures))
                else:
                    custom = None
                if custom:
                    status, data = custom
                else:
                    status, data = 200, cls.response(self.path, body)
                self.send_response(status)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Content-Type', 'text/event-stream' if body.get('stream')
                                 or ':streamGenerateContent' in self.path else 'application/json')
                self.end_headers()
                self.wfile.write(data)

        cls.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        cls.proxy = ThreadingHTTPServer(('127.0.0.1', 0), cls.mod.Handler)
        for server in (cls.upstream, cls.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @classmethod
    def response(cls, path, body):
        text = 'response password: unchanged-response-secret'
        if body.get('stream') or ':streamGenerateContent' in path:
            if path.endswith('/messages'):
                frames = [
                    {'type': 'message_start', 'message': {'usage': {'input_tokens': 2}}},
                    {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                    {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}},
                    {'type': 'content_block_stop', 'index': 0},
                    {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 1}},
                    {'type': 'message_stop'}]
            elif path.endswith('/responses'):
                frames = [{'type': 'response.output_text.delta', 'delta': text},
                          {'type': 'response.completed', 'response': {'output': [], 'usage': {'input_tokens': 2, 'output_tokens': 1}}}]
            elif ':streamGenerateContent' in path:
                frames = [{'candidates': [{'content': {'parts': [{'text': text}]}, 'finishReason': 'STOP'}],
                           'usageMetadata': {'promptTokenCount': 2, 'candidatesTokenCount': 1, 'totalTokenCount': 3}}]
            else:
                frames = [{'choices': [{'delta': {'content': text}, 'finish_reason': None}]},
                          {'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': USAGE}]
            return b''.join(b'data: ' + json.dumps(f).encode() + b'\n\n' for f in frames) + b'data: [DONE]\n\n'
        if path.endswith('/messages'):
            result = {'content': [{'type': 'text', 'text': text}], 'stop_reason': 'end_turn',
                      'usage': {'input_tokens': 2, 'output_tokens': 1}}
        elif path.endswith('/responses'):
            result = {'id': 'resp_fixture', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}],
                      'usage': {'input_tokens': 2, 'output_tokens': 1}}
        elif ':generateContent' in path:
            result = {'candidates': [{'content': {'parts': [{'text': text}]}, 'finishReason': 'STOP'}],
                      'usageMetadata': {'promptTokenCount': 2, 'candidatesTokenCount': 1, 'totalTokenCount': 3}}
        else:
            result = {'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}], 'usage': USAGE}
        return json.dumps(result).encode()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.proxy, cls.upstream):
            server.shutdown()
            server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.captures.clear()
        type(self).responder = None
        self.ep = self.endpoint('one', 'openai', 1)
        self.mod.pool = self.mod.APIPool([self.ep])
        self.mod.save_credential_redaction({'credential_redaction': {'enabled': False}})
        self.log = mock.patch.object(self.mod.chat_logger, 'add_log').start()
        self.usage = mock.patch.object(self.mod.token_tracker, 'add_usage').start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.mod.APIPool, '_retry_backoff', return_value=0).start()

    def endpoint(self, name, protocol, priority):
        return self.mod.Endpoint(id=name, name=name, base_url=f'http://127.0.0.1:{self.upstream.server_port}/v1',
                                 api_key='fixture-real-upstream-key', model='m', protocol=protocol, priority=priority,
                                 max_retries=0, health_mode='none', use_proxy=False, in_pool=True)

    def request(self, body=None, path='/endpoints/one/v1/chat/completions', method='POST'):
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=10)
        try:
            conn.request(method, path, body=json.dumps(body or {}).encode(),
                         headers={'Content-Type': 'application/json', 'User-Agent': 'credential-fixture/1'})
            response = conn.getresponse()
            return response.status, response.read().decode()
        finally:
            conn.close()

    def switch(self, enabled):
        status, text = self.request({'credential_redaction': {'enabled': enabled}},
                                    '/api/credential-redaction', 'PUT')
        self.assertEqual(status, 200, text)
        self.assertEqual(json.loads(text), {'credential_redaction': {'enabled': enabled}})

    def state(self):
        return [(e._fail_count, e._total_failures, e._cooldown_until, e._health)
                for e in self.mod.pool._endpoints]

    def test_four_protocols_stream_and_nonstream_capture(self):
        self.switch(True)
        for protocol in ('openai', 'responses', 'anthropic', 'gemini'):
            for stream in (False, True):
                with self.subTest(protocol=protocol, stream=stream):
                    self.ep.protocol = protocol
                    body = fixture_payload(stream)
                    before = copy.deepcopy(body)
                    self.log.reset_mock()
                    status, response = self.request(body)
                    self.assertEqual(status, 200, response)
                    self.assertIn('unchanged-response-secret', response)
                    wire = self.captures[-1][1].decode()
                    for literal in (SECRET, 'fixture-account', 'fixture-args-key', 'fixture-description-key'):
                        self.assertNotIn(literal, wire)
                    self.assertIn('read_credentials', wire)
                    self.assertIn('DB_PASSWORD', wire)
                    self.assertEqual(body, before)
                    self.assertTrue(self.log.called)
                    self.assertIn(SECRET, self.log.call_args.args[2])
                    headers = {k.lower(): v for k, v in self.captures[-1][2].items()}
                    self.assertEqual(headers['authorization'], 'Bearer fixture-real-upstream-key')

    def test_off_bytes_exact_on_then_off(self):
        self.mod.set_client_headers({'User-Agent': 'credential-fixture'})
        self.addCleanup(self.mod.clear_client_headers)
        body = fixture_payload()
        for enabled in (False, True, False):
            self.switch(enabled)
            self.mod.pool._try_endpoint(self.ep, body, 5)
            raw = self.captures[-1][1]
            if enabled:
                self.assertNotIn(SECRET.encode(), raw)
            else:
                self.assertEqual(raw, json.dumps(body).encode())

    def test_http_retry_reads_hot_switch_each_send(self):
        self.ep.max_retries = 1

        def responder(_path, _body, index):
            if index == 1:
                self.mod.save_credential_redaction({'credential_redaction': {'enabled': True}})
                return 500, b'{"error":{"message":"synthetic retry"}}'
        type(self).responder = responder
        self.assertEqual(self.request(fixture_payload())[0], 200)
        self.assertEqual(len(self.captures), 2)
        self.assertIn(SECRET.encode(), self.captures[0][1])
        self.assertNotIn(SECRET.encode(), self.captures[1][1])

    def test_first_packet_retry_safe(self):
        self.switch(True)
        self.ep.max_retries = 1
        type(self).responder = lambda _p, _b, n: (200, b'data: {"error":{"message":"rate limit"}}\n\n') if n == 1 else None
        self.ep.retry_on_rate_limit = True
        self.assertEqual(self.request(fixture_payload(True))[0], 200)
        self.assertEqual(len(self.captures), 2)
        self.assertTrue(all(SECRET.encode() not in raw for _, raw, _ in self.captures))

    def test_heterogeneous_fallback_and_retry_safe(self):
        self.switch(True)
        self.ep.max_retries = 1
        second = self.endpoint('two', 'responses', 2)
        self.mod.pool = self.mod.APIPool([self.ep, second])
        type(self).responder = lambda p, _b, _n: (500, b'{"error":{"message":"synthetic failure"}}') if p.endswith('completions') else None
        status, response = self.request(fixture_payload(), '/v1/chat/completions')
        self.assertEqual(status, 200, response)
        self.assertEqual(len(self.captures), 3)
        self.assertTrue(self.captures[-1][0].endswith('/responses'))
        self.assertTrue(all(SECRET.encode() not in raw for _, raw, _ in self.captures))

    def test_local_failure_503_zero_send_no_penalty(self):
        self.switch(True)
        self.mod.pool = self.mod.APIPool([self.ep, self.endpoint('two', 'gemini', 2)])
        for route in ('/v1/chat/completions', '/endpoints/one/v1/chat/completions', '/v1/responses'):
            for stream in (False, True):
                before = self.state()
                body = fixture_payload(stream) if not route.endswith('responses') else {'input': 'password: ' + SECRET, 'stream': stream}
                with mock.patch.object(self.mod, 'redact_body', side_effect=ValueError(SECRET)):
                    status, response = self.request(body, route)
                self.assertEqual(status, 503, response)
                self.assertNotIn(SECRET, response)
                self.assertEqual(self.captures, [])
                self.assertEqual(self.state(), before)

    def test_retry_failure_stops_before_second_send(self):
        self.switch(True)
        self.ep.max_retries = 2
        scanner = self.mod.redact_body
        type(self).responder = lambda _p, _b, _n: (500, b'{"error":{"message":"synthetic retry"}}')
        calls = 0

        def scan(body):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise ValueError(SECRET)
            return scanner(body)
        before = self.state()
        with mock.patch.object(self.mod, 'redact_body', side_effect=scan):
            status, response = self.request(fixture_payload(), '/v1/chat/completions')
        self.assertEqual(status, 503, response)
        self.assertEqual(len(self.captures), 1)
        self.assertEqual(self.state(), before)

    def test_empty_stream_retry_failure_terminal_event_without_penalty(self):
        self.switch(True)
        self.ep.max_retries = 1
        # Reasoning is a business frame but not a usable final answer: exercises lazy replay.
        type(self).responder = lambda _p, _b, _n: (200, b'data: {"choices":[{"delta":{"reasoning_content":"thinking"},"finish_reason":null}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        scanner = self.mod.redact_body
        scans = 0

        def scan(body):
            nonlocal scans
            scans += 1
            if scans == 2:
                raise ValueError(SECRET)
            return scanner(body)
        self.ep._health = 'ok'  # the initial business frame has already marked success
        before = self.state()
        with mock.patch.object(self.mod, 'redact_body', side_effect=scan):
            status, response = self.request(fixture_payload(True), '/v1/chat/completions')
        self.assertEqual(status, 200)  # SSE headers already sent before lazy replay.
        self.assertIn('credential_redaction_failed', response)
        self.assertIn('503', response)
        self.assertEqual(len(self.captures), 1)
        self.assertEqual(self.state(), before)

    def test_profile_identity_body_precedes_scanning(self):
        self.switch(True)
        self.ep.client_profile = 'fixture'
        self.mod.pool._client_profiles = {'fixture': {'headers': {'User-Agent': 'fixture-profile'}}}
        original = self.mod.pool._apply_profile_body_identity

        def identity(body, ep):
            body = original(body, ep)
            body['metadata'] = {'password': SECRET}
            return body
        with mock.patch.object(self.mod.pool, '_apply_profile_body_identity', side_effect=identity):
            self.assertEqual(self.request(fixture_payload())[0], 200)
        self.assertNotIn(SECRET.encode(), self.captures[-1][1])

    def test_switch_persist_reload_other_saves_and_validation(self):
        self.switch(True)
        self.mod._sync_to_config()
        document = json.loads(Path(self.mod.CONFIG_FILE).read_text())
        self.assertEqual(document['credential_redaction'], {'enabled': True})
        self.mod._credential_redaction_enabled = False
        self.assertEqual(self.mod.load_credential_redaction(), {'credential_redaction': {'enabled': True}})
        for section in (None, {}, {'enabled': 1}, {'enabled': 'false'}, {'enabled': True, 'x': 1}, []):
            self.assertEqual(self.request({'credential_redaction': section}, '/api/credential-redaction', 'PUT')[0], 400)
        for body in ({}, [], {'enabled': True}, {'credential_redaction': {'enabled': False}, 'extra': 1}):
            self.assertEqual(self.request(body, '/api/credential-redaction', 'PUT')[0], 400)
        self.assertEqual(self.request(path='/api/credential-redaction', method='GET')[0], 200)
        self.assertTrue(self.mod.get_credential_redaction()['credential_redaction']['enabled'])

    def test_write_failure_retains_file_and_runtime(self):
        self.switch(True)
        before = Path(self.mod.CONFIG_FILE).read_bytes()
        with mock.patch.object(self.mod.os, 'replace', side_effect=OSError('synthetic write failure')):
            status, _ = self.request({'credential_redaction': {'enabled': False}}, '/api/credential-redaction', 'PUT')
        self.assertEqual(status, 500)
        self.assertEqual(Path(self.mod.CONFIG_FILE).read_bytes(), before)
        self.assertTrue(self.mod.get_credential_redaction()['credential_redaction']['enabled'])

    def test_invalid_stored_switch_blocks_requests(self):
        path = Path(self.mod.CONFIG_FILE)
        before = path.read_text()
        try:
            for document in ('{"credential_redaction":{"enabled":"false"}}', '[]', '{invalid'):
                path.write_text(document)
                with self.assertRaises(self.mod.CredentialRedactionError):
                    self.mod.load_credential_redaction()
                self.assertEqual(self.request(fixture_payload())[0], 503)
                self.assertEqual(len(self.captures), 0)
        finally:
            path.write_text(before)
            self.mod.load_credential_redaction()

    def test_save_serialization_and_publish_after_replace(self):
        self.switch(True)
        entered = threading.Event()
        release = threading.Event()
        replace = self.mod.os.replace

        def slow_replace(source, target):
            entered.set()
            self.assertTrue(release.wait(5))
            replace(source, target)
        with ThreadPoolExecutor(max_workers=2) as executor, \
                mock.patch.object(self.mod.os, 'replace', side_effect=slow_replace):
            change = executor.submit(self.mod.save_credential_redaction,
                                     {'credential_redaction': {'enabled': False}})
            self.assertTrue(entered.wait(5))
            self.assertTrue(self.mod._credential_redaction_enabled)
            save = executor.submit(self.mod._sync_to_config)
            release.set()
            change.result(timeout=5)
            save.result(timeout=5)
        self.assertFalse(self.mod.get_credential_redaction()['credential_redaction']['enabled'])
        self.assertEqual(json.loads(Path(self.mod.CONFIG_FILE).read_text())['credential_redaction'], {'enabled': False})

    def test_scanning_outside_routing_lock_and_payload_unmodified(self):
        self.switch(True)
        self.mod.set_client_headers({'User-Agent': 'credential-fixture'})
        self.addCleanup(self.mod.clear_client_headers)
        scanner = self.mod.redact_body
        acquired = []

        def scan(body):
            def lock_check():
                ok = self.mod.pool._lock.acquire(timeout=1)
                acquired.append(ok)
                if ok:
                    self.mod.pool._lock.release()
            thread = threading.Thread(target=lock_check)
            thread.start()
            thread.join(2)
            return scanner(body)
        for protocol in ('openai', 'anthropic', 'responses', 'gemini'):
            self.ep.protocol = protocol
            body = fixture_payload()
            # Existing Anthropic bridge adds cache_control to text block lists.
            body['messages'][-1]['content'] = 'password: ' + SECRET
            before = copy.deepcopy(body)
            with mock.patch.object(self.mod, 'redact_body', side_effect=scan):
                result, error = self.mod.pool._try_endpoint(self.ep, body, 5)
            self.assertIsNotNone(result, error)
            self.assertEqual(body, before)
        self.assertEqual(acquired, [True] * 4)

    def test_disabled_fast_path_and_hot_disable_during_scan(self):
        raw = json.dumps(fixture_payload()).encode()
        with mock.patch.object(self.mod, 'redact_body', side_effect=AssertionError('scanner called')):
            self.assertIs(self.mod._credential_redacted_data(raw), raw)
        self.switch(True)
        scanner = self.mod.redact_body

        def scan(body):
            self.mod.save_credential_redaction({'credential_redaction': {'enabled': False}})
            return scanner(body)
        with mock.patch.object(self.mod, 'redact_body', side_effect=scan):
            self.assertEqual(self.mod._credential_redacted_data(raw), raw)

    def test_gemini_signed_function_arguments_on_wire(self):
        self.switch(True)
        self.ep.protocol = 'gemini'
        body = fixture_payload()
        call_id = self.mod._gemini_remember_tool_call('read_credentials', 'fixture-signature')
        body['messages'][1]['tool_calls'][0]['id'] = call_id
        body['messages'][2]['tool_call_id'] = call_id
        status, response = self.request(body)
        self.assertEqual(status, 200, response)
        wire = json.loads(self.captures[-1][1])
        calls = [part['functionCall'] for item in wire['contents'] for part in item['parts'] if 'functionCall' in part]
        self.assertTrue(calls, wire)
        self.assertEqual(calls[0]['args']['password'], '[REDACTED_PASSWORD]')
        self.assertIn('fixture-signature', str(wire))

    def test_temperature_shape_retry_failure_bubbles(self):
        self.switch(True)
        scanner = self.mod.redact_body
        scans = 0

        def scan(body):
            nonlocal scans
            scans += 1
            if scans == 2:
                raise ValueError(SECRET)
            return scanner(body)
        type(self).responder = lambda _p, _b, _n: (400, b'{"error":{"message":"temperature unsupported"}}')
        before = self.state()
        body = fixture_payload()
        body['temperature'] = 0.1
        with mock.patch.object(self.mod, 'redact_body', side_effect=scan):
            status, response = self.request(body)
        self.assertEqual(status, 503, response)
        self.assertEqual(len(self.captures), 1)
        self.assertEqual(self.state(), before)

    def test_stream_stall_replay_failure_terminal_event(self):
        self.switch(True)
        self.ep.protocol = 'anthropic'
        type(self).responder = lambda _p, _b, _n: (200, b'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"call_fixture","name":"read_credentials","input":{}}}\n\n')
        self.ep.max_retries = 1
        scanner = self.mod.redact_body
        opener = self.mod._open_identity_request
        scans = 0

        def open_with_timeout(*args, **kwargs):
            response = opener(*args, **kwargs)
            readline = response.readline
            reads = 0

            def read_then_timeout(*args, **kwargs):
                nonlocal reads
                reads += 1
                if reads > 1:
                    raise self.mod.socket.timeout()
                return readline(*args, **kwargs)
            response.readline = read_then_timeout
            return response

        def scan(body):
            nonlocal scans
            scans += 1
            if scans == 2:
                raise ValueError(SECRET)
            return scanner(body)
        self.ep._health = 'ok'
        before = self.state()
        with mock.patch.object(self.mod, 'redact_body', side_effect=scan), \
                mock.patch.object(self.mod, '_open_identity_request', side_effect=open_with_timeout):
            status, response = self.request(fixture_payload(True))
        self.assertEqual(status, 200, response)
        self.assertIn('credential_redaction_failed', response)
        self.assertEqual(len(self.captures), 1)
        self.assertEqual(self.state(), before)

    def test_concurrent_request_isolation(self):
        self.switch(True)

        def call(index):
            text = f'ordinary-{index} password: fixture-private-{index}'
            return index, self.request({'messages': [{'role': 'user', 'content': text}]})
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(call, range(12)))
        self.assertTrue(all(status == 200 for _, (status, _) in results))
        seen = [json.loads(raw)['messages'][0]['content'] for _, raw, _ in self.captures]
        self.assertEqual(set(seen), {f'ordinary-{i} password: [REDACTED_PASSWORD]' for i in range(12)})


class RuleTests(unittest.TestCase):
    def test_label_variants_and_known_tokens(self):
        from credential_redaction import redact_text
        cases = ['密码："fixture-secret"', '账号=fixture-user', 'username: fixture-user',
                 'DB_PASSWORD=fixture-secret', 'api-key: fixture-secret', 'access_token: fixture-secret',
                 'clientSecret: fixture-secret', '--password fixture-secret', '--api-key=fixture-secret',
                 '账号=alice', 'username=alice', 'user: alice', 'SESSION_ID=fixture-secret',
                 'curl -u alice:fixture-secret', '密码为 fixture-secret',
                 'Authorization: Bearer fixture-secret', 'token: fixture-secret',
                 'password: \"escaped\\\"fixture-secret\"',
                 '```json\n{\"password\":\"fixture-secret\"}\n```',
                 'https://fixture-user:fixture-secret@example.invalid/path',
                 'Authorization: Bearer fixture-secret', 'Authorization: Basic Zml4dHVyZTpzZWNyZXQ=',
                 'Cookie: sid=fixture-secret; session=fixture-second',
                 'sk-proj-' + 'A' * 30, 'ghp_' + 'B' * 30, 'eyJ' + 'A' * 12 + '.' + 'B' * 16 + '.' + 'C' * 16]
        for text in cases:
            with self.subTest(text=text):
                result = redact_text(text)
                self.assertNotEqual(result, text)
                self.assertNotIn('fixture-secret', result)
                self.assertEqual(result, redact_text(result))

    def test_partial_reference_cannot_hide_literal(self):
        from credential_redaction import redact_text
        text = 'password: "fixture-secret-${SUFFIX}"'
        self.assertNotIn('fixture-secret', redact_text(text))

    def test_mixed_references_hide_adjacent_literals(self):
        from credential_redaction import redact_text
        for text in ('https://$USERNAME:fixture-secret@example.invalid/',
                     'Cookie: sid=$SID; auth=fixture-secret',
                     'curl -u $USERNAME:fixture-secret'):
            with self.subTest(text=text):
                clean = redact_text(text)
                self.assertNotIn('fixture-secret', clean)
                self.assertIn('$', clean)
                self.assertEqual(clean, redact_text(clean))

    def test_environment_code_references_and_ordinary_values(self):
        from credential_redaction import redact_body, redact_text
        cases = ['password = os.environ["DB_PASSWORD"]', 'token = os.getenv("API_TOKEN")',
                 'password = password_var', 'login(password=password)', 'password = config.password',
                 'api_key=local_key', 'password=DB_PASSWORD', 'token=value',
                 'curl -H "Authorization: Bearer $TOKEN" --password "$PASSWORD"',
                 'PASSWORD=${DB_PASSWORD}', '普通姓名 王五 192.0.2.1 /opt/test 123e4567-e89b-12d3-a456-426614174000',
                 '[REDACTED_PASSWORD]', 'API_TOKEN', 'password: ${PASSWORD}']
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(redact_text(text), text)
        body = {'metadata': {'password': '${DB_PASSWORD}', 'token': 'os.getenv("API_TOKEN")'}}
        self.assertEqual(redact_body(body), body)
        clean = redact_text('import os\npassword = os.getenv(\"DB_PASSWORD\")\ntoken = os.environ[\"API_TOKEN\"]\n')
        compile(clean, '<synthetic-env-code>', 'exec')
        self.assertIn('os.getenv(\"DB_PASSWORD\")', clean)

    def test_response_format_schema_and_tool_results(self):
        from credential_redaction import redact_body
        schema = {'type': 'object', 'properties': {'password': {'type': 'string',
                  'description': 'password: fixture-description', 'enum': ['fixture-enum']}},
                  'required': ['password']}
        original = {'text': {'format': {'type': 'json_schema', 'name': 'fixed', 'schema': schema}},
                    'messages': [{'role': 'tool', 'content': {'password': 'fixture-result'}}],
                    'contents': [{'role': 'user', 'parts': [{'inlineData': {'mimeType': 'image/png', 'data': 'sk-' + 'A' * 30}}]}]}
        result = redact_body(original)
        result_schema = result['text']['format']['schema']
        self.assertEqual(result_schema['required'], ['password'])
        self.assertEqual(result_schema['properties']['password']['enum'], ['fixture-enum'])
        self.assertNotIn('fixture-description', str(result_schema))
        self.assertNotIn('fixture-result', str(result))
        self.assertEqual(result['contents'], original['contents'])

    def test_schema_protocol_binary_and_arguments(self):
        from credential_redaction import redact_body
        token = 'sk-' + 'A' * 30
        schema = {'type': 'object', 'properties': {token: {'type': 'string', 'enum': [token],
                  'description': 'password: fixture-description'}}, 'required': [token]}
        body = {'model': token, 'messages': [{'role': 'assistant', 'id': token,
            'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + token}},
                        {'type': 'thinking', 'thinking': 'password: fixture-thinking', 'signature': token}],
            'tool_calls': [{'id': token, 'type': 'function', 'function': {'name': token,
                'arguments': json.dumps({'password': 'a"b\\c', 'name': 'ordinary', 'key': token})}}]}],
            'tools': [{'type': 'function', 'function': {'name': token, 'parameters': schema}}]}
        result = redact_body(body)
        self.assertEqual(result['model'], token)
        self.assertEqual(result['messages'][0]['id'], token)
        self.assertEqual(result['messages'][0]['content'][0], body['messages'][0]['content'][0])
        self.assertEqual(result['messages'][0]['content'][1]['signature'], token)
        self.assertNotIn('fixture-thinking', str(result))
        function = result['messages'][0]['tool_calls'][0]['function']
        self.assertEqual(function['name'], token)
        self.assertEqual(json.loads(function['arguments'])['password'], '[REDACTED_PASSWORD]')
        clean_schema = result['tools'][0]['function']['parameters']
        self.assertEqual(clean_schema['required'], schema['required'])
        self.assertEqual(clean_schema['properties'][token]['enum'], [token])
        self.assertNotIn('fixture-description', str(clean_schema))
        self.assertEqual(result, redact_body(result))


if __name__ == '__main__':
    unittest.main()
