import copy
from dataclasses import asdict
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from musicbridge.core import Song, Workspace, SafetyError, score
from musicbridge.providers import Netease, SearchResults, netease_song_id
from musicbridge.review import ReviewQueue, ReviewWindow
from musicbridge.titles import search_title
from musicbridge.searching import match_pending
from test_review import seed


def raw(sid=123):
    return {'id':sid,'name':'作品','ar':[{'name':'歌手'}],'al':{'name':'专辑'},'dt':200000}


class ManualSearchTests(unittest.TestCase):
    def test_bracket_suffixes(self):
        for original, expected in [('作品 (Live)','作品'),('作品（电影主题曲）','作品'),('作品【重制】 [2025]','作品'),
                                   ('Song (Live (2025)) (Bonus)','Song'),('作品（现场（北京））','作品'),
                                   ('(I Can) Sing','(I Can) Sing'),('作品(内)尾','作品(内)尾'),('(作品)','(作品)'),('作品（未闭合','作品（未闭合')]:
            with self.subTest(original=original):
                self.assertEqual(search_title(original),expected)

    def test_automatic_queries_broaden_without_changing_source(self):
        n=Netease('.')
        n.call=Mock(return_value={'result':{'songs':[raw()]}})
        source=Song('q','作品（Live）',('歌手',))
        n.search(source)
        queries=[c.kwargs['keywords'] for c in n.call.call_args_list]
        self.assertEqual(queries,['作品 歌手','作品'])
        self.assertEqual(source.title,'作品（Live）')
        self.assertLess(score(source,Song('n','作品',('歌手',)))[0],.8)

    def test_manual_keywords_and_pagination(self):
        n=Netease('.')
        n.call=Mock(return_value={'result':{'songs':[raw(i+1) for i in range(30)],'songCount':65}})
        batch=n.search_keywords('自定 (Live) 歌手',30)
        self.assertTrue(batch.has_more)
        self.assertEqual(batch.next_offset,60)
        self.assertEqual(n.call.call_args.kwargs['keywords'],'自定 (Live) 歌手')
        self.assertEqual(n.call.call_args.kwargs['offset'],30)

    def test_supported_references(self):
        for value in ('123', 'https://music.163.com/#/song?id=123&userid=9', 'https://music.163.com/song?id=123',
                      'https://y.music.163.com/m/song?id=123', 'https://music.163.com/song/123/'):
            self.assertEqual(netease_song_id(value),'123')

    def test_reject_wrong_link_or_host(self):
        for value in ('0','abc','https://music.163.com/playlist?id=123','https://music.163.com.evil.test/song?id=123',
                      'file:///song?id=123','https://163cn.tv/short','https://music.163.com/song?id='):
            with self.subTest(value=value), self.assertRaises(SafetyError):
                netease_song_id(value)

    def test_lookup_validates_returned_id(self):
        n=Netease('.')
        n.call=Mock(return_value={'songs':[raw(123)]})
        self.assertEqual(n.song_by_reference('123').id,'123')
        n.call=Mock(return_value={'songs':[raw(999)]})
        with self.assertRaises(SafetyError):
            n.song_by_reference('123')

    def test_supplement_deduplicates_without_auto_confirming(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Workspace(folder)
            seed(store)
            queue=ReviewQueue(store)
            candidate=Song('new','作品',('歌手',))
            self.assertEqual(queue.merge_candidates('1',[candidate,candidate]),1)
            self.assertIsNone(store.data['matches']['1']['chosen'])
            self.assertEqual(len(store.data['matches']['1']['candidates']),2)
            self.assertNotIn('plan',store.data)
            self.assertEqual(queue.remaining,['1','4'])
            self.assertEqual(Workspace(folder).data['matches']['1']['manual_candidate_ids'],['new'])

    def test_merge_cannot_cross_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Workspace(folder)
            seed(store)
            q=ReviewQueue(store)
            with self.assertRaises(SafetyError):
                q.merge_candidates('4',[])

    def test_cached_suffix_pending_is_broadened_once(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Workspace(folder)
            source=asdict(Song('q','作品（电影主题曲）',('歌手',),'专辑',200))
            old=asdict(Song('old','旧候选',('歌手',),'专辑',200))
            store.data={'source':[source],'matches':{'q':{'chosen':None,'method':'pending','error':'',
                                                        'candidates':[{'song':old,'score':.4,'reasons':[]}]}}}
            api=Mock()
            api.search.return_value=[Song('new','新候选',('歌手',),'专辑',200)]
            self.assertEqual(match_pending(api,store)['completed'],1)
            self.assertEqual({c['song']['id'] for c in store.data['matches']['q']['candidates']},{'old','new'})
            self.assertEqual(store.data['matches']['q']['search_title'],'作品')
            api.search.reset_mock()
            self.assertEqual(match_pending(api,store)['completed'],0)
            api.search.assert_not_called()

    def test_explicit_single_song_can_edit_confirmed_or_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Workspace(folder)
            seed(store)
            for sid in ('2','3'):
                q=ReviewQueue(store,start_sid=sid)
                self.assertEqual(q.current,sid)

    def test_real_window_async_search_reference_and_next(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Workspace(folder)
            seed(store)
            root=tk.Tk()
            root.update()
            n=Mock()
            results=SearchResults()
            results.append(Song('777','手动找到的歌曲',('歌手',),'新专辑',200))
            results.has_more=True
            results.next_offset=30
            n.search_keywords.return_value=results
            n.song_by_reference.return_value=Song('888','链接歌曲',('歌手',),'链接专辑',201)
            window=ReviewWindow(root,store,api=n)
            def drain():
                deadline=time.monotonic()+3
                while window.network_busy and time.monotonic()<deadline:
                    root.update()
                    time.sleep(.01)
                self.assertFalse(window.network_busy)
            try:
                root.update()
                window.keywords.set('自己输入关键词')
                window.search_custom()
                drain()
                self.assertIsNone(store.data['matches']['1']['chosen'])
                self.assertTrue(window.more_available)
                selected=store.data['matches']['1']['candidates'][int(window.candidates.selection()[0])]['song']['id']
                self.assertEqual(selected,'777')
                window.search_custom(more=True)
                drain()
                n.search_keywords.assert_called_with('自己输入关键词',30)
                window.reference.set('888')
                window.load_reference()
                drain()
                selected=store.data['matches']['1']['candidates'][int(window.candidates.selection()[0])]['song']['id']
                self.assertEqual(selected,'888')
                window.confirm()
                self.assertEqual(store.data['matches']['1']['chosen']['id'],'888')
                self.assertEqual(window.queue.current,'4')
                self.assertFalse(window.more_available)
                self.assertNotIn('777', [c['song']['id'] for c in store.data['matches']['4']['candidates']])
            finally:
                window.close()
                root.destroy()

    def test_close_cancels_inflight_without_saving_results(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Workspace(folder)
            seed(store)
            before=copy.deepcopy(store.data)
            root=tk.Tk()
            root.update()
            api=Mock()
            release=threading.Event()
            def slow(*args):
                release.wait(2)
                return SearchResults()
            api.search_keywords.side_effect=slow
            window=ReviewWindow(root,store,api=api)
            try:
                window.search_custom()
                window.close()
                self.assertTrue(window.stop_event.is_set())
                release.set()
                deadline=time.monotonic()+3
                while window.winfo_exists() and time.monotonic()<deadline:
                    root.update()
                    time.sleep(.01)
                self.assertFalse(window.winfo_exists())
                self.assertEqual(before,store.data)
            finally:
                release.set()
                root.destroy()
