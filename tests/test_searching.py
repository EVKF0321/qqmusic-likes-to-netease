from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from musicbridge.core import Song, Workspace, SafetyError
from musicbridge.providers import Netease
from musicbridge.searching import RequestFailure, match_pending


def response(data, returncode=0):
    return subprocess.CompletedProcess([], returncode, 'MB_RESULT:' + json.dumps(data) + '\n', '')


class SearchingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Workspace(self.tmp.name)
        self.store.data['source'] = [asdict(Song(str(i), 'Test', ('Artist',), 'Album', 200, i)) for i in range(1, 5)]
        self.api = Netease('.', diagnostic_path=Path(self.tmp.name) / 'diagnostics.jsonl')
        self.api.wait = Mock()

    def test_timeout_retries_with_backoff(self):
        ok = response({'code': 200, 'result': {'songs': []}})
        with patch('musicbridge.providers.subprocess.run', side_effect=[subprocess.TimeoutExpired('node',45), ok]):
            self.assertEqual(self.api.call('cloudsearch', keywords='private-name')['code'], 200)
        self.assertIn(10, [x.args[0] for x in self.api.wait.call_args_list])
        record = Path(self.tmp.name, 'diagnostics.jsonl').read_text()
        self.assertIn('timeout', record)
        self.assertNotIn('private-name', record)

    def test_three_failures_then_transient_error(self):
        with patch('musicbridge.providers.subprocess.run', side_effect=subprocess.TimeoutExpired('node',45)) as run:
            with self.assertRaises(RequestFailure) as ctx:
                self.api.call('cloudsearch')
        self.assertEqual(run.call_count, 3)
        self.assertTrue(ctx.exception.transient)
        self.assertIn(25, [x.args[0] for x in self.api.wait.call_args_list])

    def test_restriction_stops_without_retry(self):
        data = response({'bridge_error':True,'detail':{'kind':'restricted','http_status':429,'upstream_code':429}},1)
        with patch('musicbridge.providers.subprocess.run', return_value=data) as run:
            with self.assertRaises(RequestFailure) as ctx:
                self.api.call('cloudsearch')
        self.assertEqual(run.call_count, 1)
        self.assertFalse(ctx.exception.transient)
        self.assertEqual(ctx.exception.kind, 'restricted')
        self.assertTrue(ctx.exception.rate_limited)

    def test_likelist_requests_are_spaced(self):
        ok = response({'code': 200, 'ids': []})
        with (patch('musicbridge.providers.subprocess.run', return_value=ok),
              patch('musicbridge.providers.time.monotonic', return_value=100)):
            self.api.call('likelist')
            self.api.call('likelist')
        self.assertEqual([call.args[0] for call in self.api.wait.call_args_list], [0, 12])

    def test_write_never_retried(self):
        with patch('musicbridge.providers.subprocess.run', side_effect=subprocess.TimeoutExpired('node',45)) as run:
            with self.assertRaises(RequestFailure) as ctx:
                self.api.call('like', write=True, id='1', like='true')
        self.assertEqual(run.call_count, 1)
        self.assertFalse(ctx.exception.transient)

    def test_like_error_retains_upstream_500_code_for_verification(self):
        error = response({'bridge_error': True, 'detail': {
            'kind': 'upstream', 'http_status': 500, 'upstream_code': 500,
        }}, 1)
        with patch('musicbridge.providers.subprocess.run', return_value=error) as run:
            with self.assertRaises(RequestFailure) as ctx:
                self.api.like('1')
        self.assertEqual(run.call_count, 1)
        self.assertEqual((ctx.exception.kind, ctx.exception.status, ctx.exception.code),
                         ('upstream', 500, 500))

    def test_marker_in_song_title(self):
        data = {'code':200,'title':'MB_RESULT:inside a title'}
        with patch('musicbridge.providers.subprocess.run', return_value=response(data)):
            self.assertEqual(self.api.call('cloudsearch'), data)

    def test_transient_song_failure_continues_and_resumes(self):
        source = Song.load(self.store.data['source'][0])
        api = Mock()
        api.search.side_effect = [RequestFailure('timeout', 'timeout', True), [source], [source], [source]]
        result = match_pending(api, self.store)
        self.assertEqual(result, {'completed':3,'failed':1})
        self.assertIn('error', self.store.data['matches']['1'])
        api.search.side_effect = None
        api.search.return_value = [source]
        api.search.reset_mock()
        self.assertEqual(match_pending(api,self.store)['completed'], 1)
        self.assertEqual(api.search.call_count,1)
        self.assertFalse(self.store.data['matches']['1'].get('error'))

    def test_consecutive_failures_circuit_breaker(self):
        api=Mock()
        api.search.side_effect=RequestFailure('network', 'network',True)
        with self.assertRaises(SafetyError):
            match_pending(api,self.store)
        self.assertEqual(api.search.call_count,3)
        self.assertEqual(len(self.store.data['matches']),3)

    def test_auth_failure_stops_immediately(self):
        api=Mock()
        api.search.side_effect=RequestFailure('login required','auth')
        with self.assertRaises(RequestFailure):
            match_pending(api,self.store)
        self.assertEqual(api.search.call_count,1)

    def test_manual_choice_unchanged(self):
        chosen=self.store.data['source'][0]
        self.store.data['matches']={'1':{'chosen':chosen,'method':'manual','candidates':[]}}
        api=Mock()
        api.search.return_value=[]
        match_pending(api,self.store)
        self.assertEqual(self.store.data['matches']['1']['chosen'],chosen)
        self.assertEqual(api.search.call_count,3)

    def test_pause_interrupts_backoff(self):
        api=Netease('.',stopped=lambda:True)
        with self.assertRaises(RequestFailure) as ctx:
            api.wait(25)
        self.assertEqual(ctx.exception.kind,'cancelled')
