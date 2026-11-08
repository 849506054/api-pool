"""Exercise the production HTTP handler on loopback with isolated response fixtures."""
import ast
import gzip
import hashlib
import http.client
import json
import os
import tempfile
import threading
import unittest
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('API_POOL_TEST_SOURCE', ROOT / 'api_pool_server.py'))


def load_handler():
    tree = ast.parse(SOURCE.read_text())
    names = {'_ui_accepts_gzip', '_ui_html_representation', '_load_gui_html', 'Handler'}
    nodes: list[ast.stmt] = [node for node in tree.body
                            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    namespace = {'BaseHTTPRequestHandler': BaseHTTPRequestHandler, 'gzip': gzip, 'os': os,
                 'hashlib': hashlib, 'json': json, 'lru_cache': lru_cache,
                 '_UI_GZIP_MIN_BYTES': 1024, 'urlparse': urlparse,
                 '_ENDPOINT_PATH': mock.Mock(), 'set_client_headers': mock.Mock(),
                 'clear_client_headers': mock.Mock(), 'clear_log_group': mock.Mock()}
    namespace['_ENDPOINT_PATH'].fullmatch.return_value = None
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), namespace)  # noqa: S102
    return namespace


class UITransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_handler()
        cls.html = (ROOT / 'static/index.html').read_text()
        cls.payload = {'endpoints': [{'id': i, 'name': '测试端点', 'model': 'm'} for i in range(100)]}
        cls.ns['_load_gui_html'] = lambda: cls.html
        cls.ns['api_handler'] = lambda *_args: (200, cls.payload, False)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), cls.ns['Handler'])
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, path='/', headers=None, method='GET', body=None):
        connection = http.client.HTTPConnection('127.0.0.1', int(self.server.server_address[1]), timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        connection.close()
        return result

    def test_html_gzip_and_identity_match_original(self):
        raw = self.html.encode()
        status, headers, body = self.request(headers={'Accept-Encoding': 'gzip, br'})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('Content-Encoding'), 'gzip')
        self.assertEqual(gzip.decompress(body), raw)
        self.assertLess(len(body), len(raw) // 2)
        self.assertEqual(int(headers['Content-Length']), len(body))
        self.assertEqual(headers['Vary'], 'Accept-Encoding')
        status, identity_headers, identity = self.request(headers={'Accept-Encoding': 'identity'})
        self.assertEqual(status, 200)
        self.assertNotIn('Content-Encoding', identity_headers)
        self.assertEqual(identity, raw)
        self.assertEqual(identity_headers['ETag'], headers['ETag'])

    def test_conditional_get_has_zero_body(self):
        _, headers, _ = self.request()
        for value in (headers['ETag'], headers['ETag'].removeprefix('W/'),
                      '"other", ' + headers['ETag'], '*'):
            with self.subTest(value=value):
                status, returned, body = self.request('/index.html', {'If-None-Match': value,
                                                                      'Accept-Encoding': 'gzip'})
                self.assertEqual(status, 304)
                self.assertEqual(body, b'')
                self.assertEqual(returned['ETag'], headers['ETag'])
                self.assertEqual(returned['Cache-Control'], 'private, no-cache')

    def test_html_change_invalidates_validator_and_compression_cache(self):
        _, headers, _ = self.request()
        changed = self.html + '<!-- changed -->'
        with mock.patch.dict(self.ns, {'_load_gui_html': lambda: changed}):
            status, fresh, body = self.request(headers={'If-None-Match': headers['ETag'],
                                                       'Accept-Encoding': 'gzip'})
        self.assertEqual(status, 200)
        self.assertNotEqual(fresh['ETag'], headers['ETag'])
        self.assertEqual(gzip.decompress(body), changed.encode())
        self.assertIs(self.ns['_ui_html_representation'](changed),
                      self.ns['_ui_html_representation'](changed))

    def test_disk_hot_update_invalidates_etag(self):
        namespace = load_handler()
        scratch = Path('/opt/data/cache/scratch/apipool-ui-transfer')
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            page = Path(directory) / 'index.html'
            page.write_text(self.html)
            namespace.update(GUI_FILE=str(page), _gui_html_cache=None, _gui_html_mtime=0)
            original = namespace['_load_gui_html']()
            old_etag = namespace['_ui_html_representation'](original)[2]
            mtime = page.stat().st_mtime
            changed = self.html + '<!-- hot update -->'
            page.write_text(changed)
            os.utime(page, (mtime + 2, mtime + 2))
            current = namespace['_load_gui_html']()
            self.assertEqual(current, changed)
            self.assertNotEqual(namespace['_ui_html_representation'](current)[2], old_etag)

    def test_accept_encoding_quality(self):
        for value, expected in [('', False), ('br', False), ('gzip', True),
                                ('gzip;q=0, *;q=1', False), ('*;q=0.5', True),
                                ('GZip; q=0.2', True), ('gzip;q=bad', False),
                                ('gzip;q=2', False), ('gzip;q=0, br;q=1', False)]:
            with self.subTest(value=value):
                _, headers, _ = self.request(headers={'Accept-Encoding': value})
                self.assertEqual(headers.get('Content-Encoding') == 'gzip', expected)

    def test_management_json_compresses_without_caching_live_state(self):
        _, headers, body = self.request('/api/endpoints', {'Accept-Encoding': 'gzip'})
        self.assertEqual(headers.get('Content-Encoding'), 'gzip')
        self.assertEqual(json.loads(gzip.decompress(body)), self.payload)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('ETag', headers)
        with mock.patch.dict(self.ns, {'api_handler': lambda *_: (200, {'next': True}, False)}):
            _, headers, body = self.request('/api/endpoints', {'Accept-Encoding': 'gzip'})
            self.assertEqual(json.loads(body), {'next': True})
            self.assertNotIn('Content-Encoding', headers)

    def test_model_json_and_management_post_keep_original_representation(self):
        for path, method, request_body in [('/v1/models', 'GET', None),
                                           ('/api/groups', 'POST', '{}'),
                                           ('/v1/chat/completions', 'POST', '{}')]:
            with self.subTest(path=path, method=method):
                _, headers, body = self.request(path, {'Accept-Encoding': 'gzip'}, method, request_body)
                self.assertNotIn('Content-Encoding', headers)
                self.assertNotIn('Vary', headers)
                self.assertEqual(json.loads(body), self.payload)

    def test_sse_stream_keeps_exact_frames(self):
        frames = [b'data: {"text":"one"}\n\n', b'data: [DONE]\n\n']
        for path, method, request_body in [('/api/stream-fixture', 'GET', None),
                                           ('/v1/chat/completions', 'POST', '{}')]:
            with self.subTest(path=path):
                with mock.patch.dict(self.ns, {'api_handler': lambda *_: (200, iter(frames), True)}):
                    status, headers, body = self.request(path, {'Accept-Encoding': 'gzip'},
                                                         method, request_body)
                self.assertEqual(status, 200)
                self.assertEqual(body, b''.join(frames))
                self.assertTrue(headers['Content-Type'].startswith('text/event-stream'))
                self.assertNotIn('Content-Encoding', headers)


if __name__ == '__main__':
    unittest.main()
