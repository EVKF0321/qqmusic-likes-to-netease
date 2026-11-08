from dataclasses import asdict
import copy
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from musicbridge.core import Song, Workspace, SafetyError
from musicbridge.review import ReviewQueue, ReviewWindow
from musicbridge.searching import match_pending


def seed(store):
    songs = [Song(str(i), f'来源 {i}', ('歌手',), '原专辑', 200, i) for i in range(1, 6)]
    candidate = asdict(Song('99', '候选歌曲', ('歌手',), '候选专辑', 201, url='https://music.163.com/#/song?id=99'))
    store.data = {'source': [asdict(s) for s in songs], 'matches': {
        str(i): {'candidates': [{'song': copy.deepcopy(candidate), 'score': .75, 'reasons': []}],
                 'chosen': None, 'method': 'pending', 'error': ''} for i in range(1, 5)}, 'plan': {'stale': True}}
    store.data['matches']['2']['chosen'] = copy.deepcopy(candidate)
    store.data['matches']['2']['method'] = 'auto'
    store.data['matches']['3']['method'] = 'skipped'
    store.save()


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Workspace(self.temp.name)
        seed(self.store)

    def test_only_pending_in_original_order(self):
        q = ReviewQueue(self.store)
        self.assertEqual(q.remaining, ['1', '4'])

    def test_confirm_advances_and_persists(self):
        q = ReviewQueue(self.store)
        q.decide('1', 0)
        self.assertEqual(q.current, '4')
        self.assertEqual(q.confirmed, 1)
        restored = Workspace(self.temp.name)
        self.assertEqual(restored.data['matches']['1']['method'], 'manual')
        self.assertNotIn('plan', restored.data)
        self.assertEqual(ReviewQueue(restored).remaining, ['4'])

    def test_skip_advances(self):
        q = ReviewQueue(self.store)
        q.decide('1', skip=True)
        self.assertEqual(q.current, '4')
        self.assertEqual(q.skipped, 1)
        self.assertEqual(self.store.data['matches']['1']['method'], 'skipped')

    def test_defer_does_not_resolve(self):
        q = ReviewQueue(self.store)
        q.defer()
        self.assertEqual(q.current, '4')
        self.assertEqual(q.remaining, ['1', '4'])
        self.assertEqual(self.store.data['matches']['1']['method'], 'pending')

    def test_select_last_then_advance_wraps(self):
        q = ReviewQueue(self.store)
        q.select('4')
        q.decide('4', 0)
        self.assertEqual(q.current, '1')

    def test_empty_queue(self):
        q = ReviewQueue(self.store)
        q.decide('1', 0)
        q.decide('4', skip=True)
        self.assertIsNone(q.current)
        self.assertEqual(q.remaining, [])

    def test_stale_choice_rejected(self):
        q = ReviewQueue(self.store)
        q.select('4')
        with self.assertRaises(SafetyError):
            q.decide('1', 0)

    def test_no_selection_rejected(self):
        q = ReviewQueue(self.store)
        with self.assertRaises(SafetyError):
            q.decide('1')
        self.assertEqual(q.remaining, ['1', '4'])

    def test_save_failure_rolls_back(self):
        q = ReviewQueue(self.store)
        with patch.object(self.store, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                q.decide('1', 0)
        self.assertIsNone(self.store.data['matches']['1']['chosen'])
        self.assertIn('plan', self.store.data)
        self.assertEqual(q.current, '1')

    def test_skip_failed_search_not_searched_again(self):
        self.store.data['matches']['1']['error'] = 'old error'
        q = ReviewQueue(self.store)
        q.decide('1', skip=True)
        api = Mock()
        api.search.return_value = []
        match_pending(api, self.store)
        self.assertEqual(api.search.call_count, 1)  # Only never-searched source #5.
        self.assertEqual(self.store.data['matches']['1']['method'], 'skipped')

    def test_real_tk_confirm_skip_and_completion(self):
        root = tk.Tk()
        self.addCleanup(root.destroy)
        root.update()
        callback = Mock()
        window = ReviewWindow(root, self.store, callback)
        root.update()
        self.assertEqual(window.queue.current, '1')
        self.assertIn('来源 1', window.source_text.get())
        self.assertEqual(window.candidates.selection(), ('0',))
        window.confirm_button.invoke()
        root.update()
        self.assertEqual(window.queue.current, '4')
        self.assertIn('来源 4', window.source_text.get())
        window.skip_button.invoke()
        root.update()
        self.assertIsNone(window.queue.current)
        self.assertEqual(str(window.confirm_button['state']), 'disabled')
        window.close()
        callback.assert_called_once()
