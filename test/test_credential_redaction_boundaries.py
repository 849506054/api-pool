"""HTTP regressions for credential-redaction ownership and Responses boundaries."""
import copy
import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'credential_boundary_fixture', ROOT / 'test' / 'test_credential_redaction.py')
assert SPEC is not None and SPEC.loader is not None
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)
fixture.__dict__['SCRATCH'] = Path('/opt/data/cache/scratch/apipool-credential-redaction/wire-fix')
fixture.SCRATCH.mkdir(parents=True, exist_ok=True)

REASON_ONLY = (
    b'data: {"choices":[{"delta":{"reasoning_content":"thinking"},"finish_reason":null}]}\n\n'
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    b'data: [DONE]\n\n'
)
LOCAL_ERROR = {
    'message': 'API Pool credential redaction unavailable',
    'type': 'service_unavailable', 'code': 'credential_redaction_failed',
}


class BoundaryWireTests(fixture.WireTests):
    def penalty_state(self):
        return [(ep.id, ep._fail_count, ep._total_failures, ep._cooldown_until,
                 ep._cooldown_reason, ep._health, ep._last_error)
                for ep in self.mod.pool._endpoints]

    def check_vision_failure(self, stream):
        self.switch(True)
        for group in ('main', 'boundary-group'):
            for occupied in (False, True):
                with self.subTest(group=group, occupied=occupied, stream=stream):
                    vision = self.endpoint('vision', 'openai', 2)
                    vision.pool_groups = ['vision']
                    vision.is_vision = True
                    self.ep.is_vision = False
                    self.ep.pool_groups = [group]
                    pool = self.mod.APIPool([self.ep, vision])
                    self.mod.pool = pool
                    if occupied:
                        # Other requests in this group, another group, and other endpoints.
                        pool._last_pool_activity = self.mod.time.time()
                        for ep_id, owner in ((self.ep.id, group), (self.ep.id, group),
                                             (self.ep.id, 'peer'), ('vision', 'peer'),
                                             ('unrelated-endpoint', 'peer')):
                            pool._acquire_inflight(ep_id, owner)
                    before = copy.deepcopy(pool._inflight_owner)
                    before_state = self.penalty_state()
                    body = {'model': group, 'stream': stream, 'messages': [
                        {'role': 'user', 'content': [
                            {'type': 'text', 'text': 'describe image'},
                            {'type': 'image_url', 'image_url': {
                                'url': 'data:image/png;base64,ZmFrZS1pbWFnZQ=='}}]}]}
                    with mock.patch.object(self.mod, 'redact_body',
                                           side_effect=ValueError(fixture.SECRET)) as scan, \
                            mock.patch.object(pool, '_try_endpoint', wraps=pool._try_endpoint) as send, \
                            mock.patch.object(pool, '_release_inflight', wraps=pool._release_inflight) as release:
                        status, response = self.request(body, '/v1/chat/completions')
                    self.assertEqual(status, 503, response)
                    self.assertEqual(json.loads(response), {'error': LOCAL_ERROR})
                    self.assertNotIn(fixture.SECRET, response)
                    self.assertEqual(scan.call_count, 1)
                    self.assertEqual(send.call_count, 1)
                    self.assertIs(send.call_args.args[0], vision)
                    self.assertEqual(self.captures, [])
                    self.assertEqual(self.penalty_state(), before_state)
                    self.assertEqual(pool._inflight_owner, before)
                    release.assert_called_once_with(self.ep.id, group)

    def test_vision_sync_releases_only_its_group_ownership(self):
        self.check_vision_failure(False)

    def test_vision_stream_releases_only_its_group_ownership(self):
        self.check_vision_failure(True)

    def failed_event(self, response):
        events = [json.loads(line[6:]) for line in response.splitlines()
                  if line.startswith('data: ') and line != 'data: [DONE]']
        types = [event.get('type') for event in events]
        self.assertEqual(types.count('response.failed'), 1, types)
        self.assertEqual(types[-1], 'response.failed')
        self.assertNotIn('response.completed', types)
        self.assertEqual(response.count('data: [DONE]'), 1)
        failed = events[-1]['response']
        self.assertEqual(failed['status'], 'failed')
        return failed['error']

    def check_responses_retry_failure(self, route):
        self.switch(True)
        self.ep.max_retries = 1
        self.ep._health = 'ok'  # The first business frame marks success before lazy retry.
        type(self).responder = lambda _p, _b, _n: (200, REASON_ONLY)
        scanner = self.mod.redact_body
        scans = 0

        def scan(body):
            nonlocal scans
            scans += 1
            if scans == 2:
                raise ValueError(fixture.SECRET)
            return scanner(body)

        before = self.penalty_state()
        with mock.patch.object(self.mod, 'redact_body', side_effect=scan):
            status, response = self.request(
                {'input': 'password: ' + fixture.SECRET, 'stream': True}, route)
        self.assertEqual(status, 200, response)  # The first stream already sent HTTP headers.
        self.assertEqual(scans, 2)
        self.assertEqual(len(self.captures), 1)
        self.assertNotIn(fixture.SECRET.encode(), self.captures[0][1])
        self.assertNotIn(fixture.SECRET, response)
        self.assertEqual(self.penalty_state(), before)
        self.assertEqual(self.failed_event(response), {**LOCAL_ERROR, 'param': None, 'status': 503})

    def test_responses_pool_retry_keeps_local_code_and_status(self):
        self.check_responses_retry_failure('/v1/responses')

    def test_responses_direct_retry_keeps_local_code_and_status(self):
        self.check_responses_retry_failure('/endpoints/one/v1/responses')

    def check_responses_upstream_error(self, route):
        self.switch(True)
        for code in ('fixture_upstream_error', 'credential_redaction_failed'):
            with self.subTest(code=code):
                upstream_error = {'message': 'fixture upstream failure',
                                  'type': 'upstream_error', 'code': code, 'status': 502}
                wire = (b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                        + b'data: ' + json.dumps({'error': upstream_error}).encode()
                        + b'\n\ndata: [DONE]\n\n')
                type(self).responder = lambda _p, _b, _n, wire=wire: (200, wire)
                before_sends = len(self.captures)
                status, response = self.request({'input': 'ordinary text', 'stream': True}, route)
                self.assertEqual(status, 200, response)
                self.assertEqual(len(self.captures), before_sends + 1)
                # Golden contract: ordinary upstream fields still use the existing conversion.
                self.assertEqual(self.failed_event(response), {
                    'message': 'fixture upstream failure', 'type': 'server_error',
                    'param': None, 'code': None})

    def test_responses_pool_ordinary_upstream_error_contract(self):
        self.check_responses_upstream_error('/v1/responses')

    def test_responses_direct_ordinary_upstream_error_contract(self):
        self.check_responses_upstream_error('/endpoints/one/v1/responses')


def load_tests(_loader, _tests, _pattern):
    # Reuse fixture lifecycle/helpers while selecting only this file's regressions.
    return unittest.TestSuite(BoundaryWireTests(name) for name in sorted(
        name for name in BoundaryWireTests.__dict__ if name.startswith('test_')))


if __name__ == '__main__':
    unittest.main()
