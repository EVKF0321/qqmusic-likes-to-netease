from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from musicbridge.core import Song, Workspace
from musicbridge.providers import Netease, MetadataError, ne_song
from musicbridge.searching import match_pending, RequestFailure
from musicbridge.diagnostics import describe_error


def raw(**changes):
    return {'id': 123, 'name': '作品', 'ar': [{'name': '歌手'}], 'al': {'name': '专辑'}, 'dt': 200000, **changes}


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = Netease('.')
        self.source = Song('q', '作品', ('歌手',), '专辑', 200, 1)

    def test_missing_artist_name(self):
        with self.assertRaises(MetadataError) as ctx:
            ne_song(raw(ar=[{'id': 0}]))
        self.assertEqual(ctx.exception.field, 'artists.name')

    def test_missing_collaborator_rejected(self):
        with self.assertRaises(MetadataError):
            ne_song(raw(ar=[{'name': '歌手'}, {'id': 9}]))

    def test_null_artist(self):
        for ar in (None, [], [None], [{'name': None}], [{'name': ''}]):
            with self.subTest(ar=ar), self.assertRaises(MetadataError):
                ne_song(raw(ar=ar))

    def test_missing_identity_fields(self):
        for patch in ({'id':None}, {'id':0}, {'name':None}, {'name':''}):
            with self.subTest(patch=patch), self.assertRaises(MetadataError):
                ne_song(raw(**patch))

    def test_bad_duration(self):
        for value in ('bad', float('nan'), float('inf'), -1, True):
            with self.subTest(value=value), self.assertRaises(MetadataError):
                ne_song(raw(dt=value))

    def test_optional_missing_values(self):
        song = ne_song(raw(al=None, dt=None, alia=None))
        self.assertEqual(song.album, '')
        self.assertEqual(song.seconds, 0)

    def test_legacy_fields(self):
        parsed = ne_song({'id':1,'name':'作品','artists':[{'name':'歌手'}], 'album':{'name':'专辑'},'duration':200000})
        self.assertEqual(parsed.seconds,200)

    def test_mixed_candidates_continue(self):
        self.api.call = Mock(return_value={'result':{'songs':[raw(ar=[{'id':0}]),raw()]}})
        results = self.api.search(self.source)
        self.assertEqual(len(results),2)  # Two query forms; ranking deduplicates IDs.
        self.assertEqual(len(results.metadata_issues),2)
        self.assertTrue(all(s.artists == ('歌手',) for s in results))

    def test_mixed_candidates_disable_auto_confirmation(self):
        self.api.call = Mock(return_value={'result':{'songs':[raw(ar=[{'id':0}]),raw()]}})
        store=Workspace(self.temp.name)
        store.data['source']=[asdict(self.source)]
        self.assertEqual(match_pending(self.api,store)['completed'],1)
        match=store.data['matches']['q']
        self.assertIsNone(match['chosen'])
        self.assertEqual(len(match['candidates']),1)
        self.assertEqual(len(match['metadata_issues']),2)

    def test_all_invalid_remain_retryable(self):
        self.api.call = Mock(return_value={'result':{'songs':[raw(ar=[{'id':0}])]}})
        with self.assertRaises(RequestFailure) as ctx:
            self.api.search(self.source)
        self.assertEqual(ctx.exception.kind,'metadata')
        self.assertTrue(ctx.exception.transient)

    def test_malformed_search_envelope(self):
        for result in (None, {'songs':None}):
            self.api.call = Mock(return_value={'result':result})
            with self.assertRaises(RequestFailure):
                self.api.search(self.source)

    def test_empty_results_legitimate(self):
        self.api.call=Mock(return_value={'result':{'songs':[],'songCount':0}})
        self.assertEqual(self.api.search(self.source),[])

    def test_diagnostics_do_not_capture_values_or_locals(self):
        secret='MUSIC_U=private-token'
        try:
            raise KeyError(secret)
        except KeyError as exc:
            message=describe_error(exc,self.temp.name)
        record=Path(self.temp.name,'program-errors.jsonl').read_text('utf-8')
        self.assertNotIn(secret,message+record)
        self.assertIn('test_metadata.py',record)
        self.assertEqual(json.loads(record)['exception_type'],'KeyError')
