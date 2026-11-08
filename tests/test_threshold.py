from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from musicbridge.core import Song, Workspace, rank
from musicbridge.searching import reconfirm_cached


class ThresholdTests(unittest.TestCase):
    def test_strict_boundary(self):
        source=Song('q','Song',('Artist',))
        candidate=Song('n','Song',('Artist',))
        for value, expected in ((.8,False),(.8001,True),(.81,True),(.79,False)):
            with self.subTest(value=value), patch('musicbridge.core.score',return_value=(value,[])):
                self.assertEqual(bool(rank(source,[candidate])['chosen']),expected)

    def test_cached_choices_applied_without_search(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Workspace(folder)
            source=Song('q','Song',('Artist',),'Album',200)
            candidate=Song('n','Song',('Artist',),'Album',0)
            store.data={'source':[asdict(source)],'matches':{'q':{'candidates':[{'song':asdict(candidate)}],'chosen':None,'method':'pending','error':''}}, 'plan':{'old':True}}
            self.assertEqual(reconfirm_cached(store),1)
            self.assertEqual(store.data['matches']['q']['chosen']['id'],'n')
            self.assertNotIn('plan',store.data)
            self.assertTrue(list((Path(folder)/'backups').glob('matching-policy-*.json')))
            self.assertEqual(reconfirm_cached(store),0)

    def test_preserve_manual_skip_and_metadata_guard(self):
        for extra in ({'method':'manual'}, {'method':'skipped'}, {'metadata_issues':[{'field':'artists.name'}]}, {'error':'network'}):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as folder:
                store=Workspace(folder)
                source=Song('q','Song',('Artist',),'Album',200)
                candidate=Song('n','Song',('Artist',),'Album',200)
                match={'candidates':[{'song':asdict(candidate)}],'chosen':None,'method':'pending','error':'',**extra}
                store.data={'source':[asdict(source)],'matches':{'q':match}}
                self.assertEqual(reconfirm_cached(store),0)
                self.assertIsNone(store.data['matches']['q']['chosen'])
