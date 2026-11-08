import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from dataclasses import asdict

from musicbridge.core import (Song, SafetyError, RequestFailure, Workspace, Engine,
                              collect_pages, score, rank, verify, backup)
from musicbridge.providers import ne_song
from musicbridge.vault import Vault


def song(sid, title='测试歌曲', artists=('歌手甲',), album='专辑', seconds=200, **kw):
    return Song(str(sid), title, artists, album, seconds, **kw)


class Fake:
    account = 'mine'
    def __init__(self, order=(), direction='prepend'):
        self.order, self.direction, self.writes = list(order), direction, []
        self.fail = None
    def read_likes(self):
        return [song(x) for x in self.order]
    def like(self, sid):
        self.writes.append(sid)
        if sid not in self.order:
            if self.direction == 'prepend':
                self.order.insert(0, sid)
            else:
                self.order.append(sid)
        if sid == self.fail:
            raise OSError('lost response after write')
    def reorder(self, order):
        self.order = list(order)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ws = Workspace(self.temp.name)
        self.ws.data = {'source': [asdict(song(x, position=i + 1)) for i, x in enumerate('abc')],
                        'source_confirmed': True, 'matches': {x: {'chosen': asdict(song(x)), 'method': 'auto'} for x in 'abc'},
                        'evidence': {'direction': 'prepend', 'account': 'mine', 'reorder': True, 'time': time.time()}}
        self.sleep = patch('musicbridge.core.time.sleep')
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def test_large_pages(self):
        rows = [song(x) for x in range(1051)]
        got = collect_pages(lambda p, n: (rows[(p-1)*n:p*n], len(rows)))
        self.assertEqual(len(got), 1051)
        self.assertEqual(got[-1].position, 1051)

    def test_duplicate_page_rejected(self):
        with self.assertRaises(SafetyError):
            collect_pages(lambda p, n: ([song(1)], 2))

    def test_truncated_page_rejected(self):
        with self.assertRaises(SafetyError):
            collect_pages(lambda p, n: ([song(1)] if p == 1 else [], 2))

    def test_total_change_rejected(self):
        with self.assertRaises(SafetyError):
            collect_pages(lambda p, n: ([song(p)], 3 if p == 1 else 4))

    def test_unicode_match(self):
        self.assertGreaterEqual(score(song(1, 'ＡＢＣ！'), song(2, 'abc'))[0], .90)

    def test_wrong_artist(self):
        self.assertLess(score(song(1), song(2, artists=('另一位',)))[0], .6)

    def test_versions_rejected(self):
        for version in ['Live', 'Remix', 'Cover', 'Instrumental', 'Acoustic', 'Demo', 'Remastered', 'Sped up']:
            with self.subTest(version=version):
                self.assertLess(score(song(1), song(2, title='测试歌曲 (' + version + ')'))[0], .6)

    def test_live_album_rejected(self):
        self.assertLess(score(song(1), song(2, album='Live 演唱会'))[0], .6)

    def test_multiple_artists(self):
        self.assertLess(score(song(1, artists=('甲', '乙')), song(2, artists=('甲',)))[0], .9)

    def test_duration_rejected(self):
        self.assertLess(score(song(1), song(2, seconds=260))[0], .9)

    def test_missing_duration_follows_user_threshold(self):
        self.assertEqual(rank(song(1), [song(2, seconds=0)])['chosen']['id'], '2')

    def test_close_candidates_follow_user_threshold(self):
        self.assertEqual(rank(song(1), [song(2), song(3)])['chosen']['id'], '2')

    def test_best_not_first(self):
        self.assertEqual(rank(song(1), [song(2, artists=('错',)), song(3)])['chosen']['id'], '3')

    def test_duplicate_candidates_not_ambiguous(self):
        self.assertEqual(rank(song(1), [song(2), song(2)])['chosen']['id'], '2')

    def test_prepend_reverse_operations(self):
        api = Fake(['old'])
        e = Engine(api, self.ws)
        self.assertEqual(e.plan()['operations'], ['c', 'b', 'a'])
        report = e.run(True)
        self.assertEqual(api.order, ['a', 'b', 'c', 'old'])
        self.assertTrue(report['complete_success'])
        self.assertTrue(report['original_likes_preserved'])

    def test_append_operations(self):
        api = Fake(['old'], 'append')
        self.ws.data['evidence']['direction'] = 'append'
        e = Engine(api, self.ws)
        self.assertEqual(e.plan()['operations'], ['a', 'b', 'c'])
        self.assertTrue(e.run(True)['verification']['relative_order_ok'])

    def test_existing_likes_no_duplicate(self):
        api = Fake(['b'])
        e = Engine(api, self.ws)
        e.plan()
        report = e.run(True)
        self.assertNotIn('b', api.writes)
        self.assertFalse(report['complete_success'])
        self.assertTrue(report['new_songs_order']['relative_order_ok'])

    def test_sync_reorders_without_unlike(self):
        api = Fake(['b', 'c', 'a'])
        e = Engine(api, self.ws)
        e.plan('sync')
        self.assertTrue(e.run(True)['complete_success'])
        self.assertEqual(api.order, list('abc'))

    def test_sync_extras_blocked(self):
        with self.assertRaises(SafetyError):
            Engine(Fake(['extra']), self.ws).plan('sync')

    def test_unproven_order_blocked(self):
        self.ws.data.pop('evidence')
        with self.assertRaises(SafetyError):
            Engine(Fake(), self.ws).plan()

    def test_expired_evidence_blocked(self):
        self.ws.data['evidence']['time'] -= 90000
        with self.assertRaises(SafetyError):
            Engine(Fake(), self.ws).plan()

    def test_account_mismatch_blocked(self):
        self.ws.data['evidence']['account'] = 'someone-else'
        with self.assertRaises(SafetyError):
            Engine(Fake(), self.ws).plan()

    def test_manual_approval_required(self):
        e = Engine(Fake(), self.ws)
        e.plan()
        with self.assertRaises(SafetyError):
            e.run()

    def test_concurrent_change_before_run(self):
        api = Fake()
        e = Engine(api, self.ws)
        e.plan()
        api.order.append('external')
        with self.assertRaises(SafetyError):
            e.run(True)
        self.assertEqual(api.writes, [])

    def test_mapping_change_blocked(self):
        e = Engine(Fake(), self.ws)
        e.plan()
        self.ws.data['matches']['a']['chosen'] = asdict(song('different'))
        with self.assertRaises(SafetyError):
            e.run(True)

    def test_resume_ambiguous_write(self):
        api = Fake()
        api.fail = 'c'
        e = Engine(api, self.ws)
        e.plan()
        self.assertEqual(e.run(True)['status'], 'paused')
        self.assertEqual(api.order, ['c'])
        # New process loads durable state, reconciles actual likes, replans.
        restored = Workspace(self.temp.name)
        api.fail = None
        e = Engine(api, restored)
        self.assertEqual(e.plan()['operations'], ['b', 'a'])
        self.assertTrue(e.run(True)['complete_success'])
        self.assertEqual(api.writes.count('c'), 1)

    def test_migration_shares_fresh_read_between_spaced_writes(self):
        api = Fake()
        engine = Engine(api, self.ws)
        engine.plan()
        clock = [0.0]
        events = []
        like = api.like

        def timed_like(sid):
            events.append(('write', clock[0], sid))
            like(sid)

        def ordered_ids():
            events.append(('read', clock[0], tuple(api.order)))
            return api.order.copy()

        def advance(seconds):
            clock[0] += seconds

        api.ordered_ids = ordered_ids
        with (patch.object(api, 'like', side_effect=timed_like),
              patch('musicbridge.core.time.monotonic', side_effect=lambda: clock[0]),
              patch('musicbridge.core.time.sleep', side_effect=advance)):
            self.assertTrue(engine.run(True)['complete_success'])
        self.assertEqual(events, [
            ('read', 0, ()), ('write', 0, 'c'),
            ('read', 20, ('c',)), ('write', 20, 'b'),
            ('read', 40, ('b', 'c')), ('write', 40, 'a'),
            ('read', 40, ('a', 'b', 'c')),
        ])

    def test_target_change_during_wait_blocks_next_write(self):
        api = Fake()
        engine = Engine(api, self.ws)
        engine.plan()
        clock = [0.0]

        def advance(seconds):
            clock[0] += seconds
            if api.writes and 'external' not in api.order:
                api.order.insert(0, 'external')

        with (patch('musicbridge.core.time.monotonic', side_effect=lambda: clock[0]),
              patch('musicbridge.core.time.sleep', side_effect=advance)):
            report = engine.run(True)
        self.assertEqual(api.writes, ['c'])
        self.assertEqual(report['status'], 'paused')
        self.assertEqual(report['pending_write']['id'], 'c')

    def test_read_throttle_after_write_waits_then_resumes_without_duplicate(self):
        api = Fake()
        engine = Engine(api, self.ws)
        engine.plan()
        checked_after_write = False

        def ordered_ids():
            nonlocal checked_after_write
            if api.writes and not checked_after_write:
                checked_after_write = True
                raise RequestFailure('405', 'restricted', code=405)
            return api.order.copy()

        api.ordered_ids = ordered_ids
        with patch.object(engine, 'cooldown', wraps=engine.cooldown) as cooldown:
            report = engine.run(True)
        cooldown.assert_called_once_with(120)
        self.assertEqual(engine.write_interval, 30)
        self.assertTrue(report['final_verified'])
        self.assertEqual(report['newly_confirmed'], 3)
        self.assertEqual(api.writes, ['c', 'b', 'a'])
        self.assertNotIn('inflight', self.ws.data)

    def test_rejected_write_reads_baseline_before_retry(self):
        api = Fake()
        engine = Engine(api, self.ws)
        engine.plan()
        events = []
        like = api.like

        def submit(sid):
            events.append(('write', sid))
            if len(events) == 2:
                raise RequestFailure('405', 'restricted', code=405)
            like(sid)

        def ordered_ids():
            events.append(('read', tuple(api.order)))
            return api.order.copy()

        api.ordered_ids = ordered_ids
        with patch.object(api, 'like', side_effect=submit), patch.object(engine, 'cooldown') as cooldown:
            self.assertTrue(engine.run(True)['complete_success'])
        cooldown.assert_called_once_with(120)
        self.assertEqual(events[:5], [('read', ()), ('write', 'c'), ('read', ()),
                                      ('write', 'c'), ('read', ('c',))])

    def test_persistent_restriction_stops_with_pending_checkpoint(self):
        api = Fake()
        engine = Engine(api, self.ws)
        engine.plan()

        def ordered_ids():
            if api.writes:
                raise RequestFailure('405', 'restricted', code=405)
            return api.order.copy()

        api.ordered_ids = ordered_ids
        with (patch.object(api, 'read_likes', wraps=api.read_likes) as read_likes,
              patch.object(engine, 'cooldown') as cooldown):
            report = engine.run(True)
        self.assertEqual([call.args[0] for call in cooldown.call_args_list], [120, 240, 480, 960])
        self.assertEqual(read_likes.call_count, 1)
        self.assertEqual(api.writes, ['c'])
        self.assertEqual(report['status'], 'unverified')
        self.assertEqual(report['pending_write']['id'], 'c')
        self.assertEqual(Workspace(self.temp.name).data['inflight']['id'], 'c')

    def test_repeat_import_no_writes(self):
        api = Fake(list('abc'))
        e = Engine(api, self.ws)
        e.plan()
        self.assertTrue(e.run(True)['complete_success'])
        self.assertEqual(api.writes, [])

    def test_unapplied_like_with_500_pauses_and_resumes_in_order(self):
        api = Fake(['old'])
        engine = Engine(api, self.ws)
        engine.plan()
        like = api.like

        def submit(sid):
            if sid == 'b':
                raise RequestFailure('写入返回 500', 'upstream', code=500)
            like(sid)

        with patch.object(api, 'like', side_effect=submit) as submitted:
            report = engine.run(True)
        self.assertEqual([call.args[0] for call in submitted.call_args_list], ['c', 'b'])
        self.assertEqual(api.order, ['c', 'old'])
        self.assertEqual(report['status'], 'paused')
        self.assertEqual(report['newly_confirmed'], 1)
        self.assertEqual(report['blocked_song'], 'b')
        self.assertIsNone(report['pending_write'])
        self.assertFalse(report['complete_success'])
        restored = Workspace(self.temp.name)
        journal = restored.data['journal']
        self.assertEqual([(entry['id'], entry['confirmed']) for entry in journal],
                         [('c', True), ('b', False)])
        resume = Engine(api, restored)
        self.assertEqual(resume.plan()['operations'], ['b', 'a'])
        self.assertTrue(resume.run(True)['complete_success'])
        self.assertEqual(api.order, ['a', 'b', 'c', 'old'])

    def test_like_applied_despite_500_is_confirmed_once(self):
        api = Fake()
        engine = Engine(api, self.ws)
        engine.plan()
        like = api.like

        def submit(sid):
            like(sid)
            if sid == 'b':
                raise RequestFailure('写入返回 500', 'upstream', status=500)

        with patch.object(api, 'like', side_effect=submit):
            report = engine.run(True)
        self.assertEqual(api.writes, ['c', 'b', 'a'])
        self.assertIsNone(report['blocked_song'])
        self.assertTrue(report['complete_success'])

    def test_confirmed_progress_and_overnight_resume_after_replacing_failed_song(self):
        api = Fake(['old'])
        updates = []
        engine = Engine(api, self.ws, on_progress=updates.append)
        engine.plan()
        like = api.like

        def submit(sid):
            if sid == 'b':
                raise RequestFailure('500', 'upstream', status=500)
            like(sid)

        with patch.object(api, 'like', side_effect=submit):
            report = engine.run(True)
        self.assertEqual(report['blocked_song'], 'b')
        self.assertEqual([u.verified_song for u in updates if u.verified_song], ['c'])
        self.assertEqual(updates[-1].confirmed, 1)
        self.assertEqual(self.ws.data['target_state']['migrated'], ['c'])

        restored = Workspace(self.temp.name)
        restored.data['evidence']['time'] -= 90000
        restored.data['matches']['b']['chosen'] = asdict(song('replacement'))
        restored.data.pop('plan', None)
        resume = Engine(api, restored)
        with self.assertRaises(SafetyError):
            resume.plan('sync')
        self.assertEqual(resume.plan()['operations'], ['replacement', 'a'])
        self.assertTrue(resume.run(True)['complete_success'])
        self.assertEqual(api.order, ['a', 'replacement', 'c', 'old'])
        self.assertEqual(set(restored.data['target_state']['migrated']), {'a', 'replacement', 'c'})
        self.assertNotIn('resume_order', restored.data)

    def test_verified_progress_does_not_count_an_unknown_write(self):
        api = Fake()
        api.fail = 'c'
        updates = []
        engine = Engine(api, self.ws, on_progress=updates.append)
        engine.plan()
        report = engine.run(True)
        self.assertEqual(report['pending_write']['id'], 'c')
        self.assertFalse(any(u.confirmed for u in updates))
        self.assertNotIn('c', self.ws.data['target_state']['migrated'])

    def test_sync_pauses_before_reordering_an_incomplete_list(self):
        api = Fake(['c', 'a'])
        engine = Engine(api, self.ws)
        engine.plan('sync')
        with (patch.object(api, 'like', side_effect=RequestFailure('写入返回 500', 'upstream', code=500)) as like,
              patch.object(api, 'reorder') as reorder):
            report = engine.run(True)
        like.assert_called_once_with('b')
        reorder.assert_not_called()
        self.assertEqual(report['status'], 'paused')
        self.assertEqual(report['blocked_song'], 'b')
        self.assertEqual(api.order, ['c', 'a'])

    def test_collision_blocks(self):
        self.ws.data['matches']['b']['chosen'] = asdict(song('a'))
        with self.assertRaises(SafetyError):
            Engine(Fake(), self.ws).plan()

    def test_probe_empty_baseline_both_directions(self):
        for direction in ('prepend', 'append'):
            api = Fake(direction=direction)
            evidence = Engine(api, self.ws).probe(True, True)
            self.assertEqual(evidence['direction'], direction)
            self.assertTrue(evidence['reorder'])
            self.assertEqual(set(api.order), set('ab'))

    def test_probe_preserves_old(self):
        api = Fake(['old1', 'old2'])
        Engine(api, self.ws).probe(True, True)
        self.assertEqual(api.order[-2:], ['old1', 'old2'])

    def test_probe_requires_approval(self):
        api = Fake()
        with self.assertRaises(SafetyError):
            Engine(api, self.ws).probe()
        self.assertFalse(api.writes)

    def test_cancel_no_writes(self):
        api = Fake()
        e = Engine(api, self.ws, stopped=lambda: True)
        e.plan()
        self.assertEqual(e.run(True)['status'], 'paused')
        self.assertFalse(api.writes)

    def test_verify_reversed_and_missing(self):
        check = verify(list('abcd'), list('cba'))
        self.assertTrue(check['reversed'])
        self.assertEqual(check['missing'], ['d'])
        self.assertFalse(check['relative_order_ok'])

    def test_backup_unicode_csv(self):
        backup(Path(self.temp.name), 'qq', [song(1, title='=危险,日语テスト')])
        self.assertIn("'=危险", (Path(self.temp.name) / 'qq.csv').read_text('utf-8-sig'))
        self.assertEqual(json.loads((Path(self.temp.name) / 'qq.json').read_text('utf-8'))['songs'][0]['title'], '=危险,日语テスト')

    def test_parser_ms_unicode(self):
        s = ne_song({'id': 123, 'name': '日本語', 'ar': [{'name': '甲'}, {'name': '乙'}], 'al': {'name': '专辑'}, 'dt': 123456})
        self.assertEqual(s.seconds, 123.456)
        self.assertEqual(s.artists, ('甲', '乙'))

    def test_windows_vault(self):
        v = Vault(Path(self.temp.name) / 'auth')
        v.save({'cookie': 'private-secret', 'unicode': '中文'})
        self.assertNotIn(b'private-secret', v.path.read_bytes())
        self.assertEqual(v.read()['unicode'], '中文')
        v.clear()
        self.assertEqual(v.read(), {})


if __name__ == '__main__':
    unittest.main()
