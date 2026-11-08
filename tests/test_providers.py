import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from musicbridge.core import RequestFailure, SafetyError, ids
from musicbridge.providers import Netease, QQ, qq_song


def raw_qq(sid):
    return NS(id=sid, title='歌曲', name='歌曲', singer=[NS(name='歌手')], album=NS(name='专辑'), interval=200, mid='mid'+str(sid), subtitle='')


class ProviderTests(unittest.TestCase):
    def ne(self, liked=('1', '2'), count=2, special=5):
        n = Netease(Path('.'))
        n.account, n.playlist = '123', '999'
        def call(method, **kw):
            if method == 'playlist_detail':
                return {'playlist': {'specialType': special, 'userId': 123, 'trackCount': count,
                                      'trackIds': [{'id': 1}, {'id': 2}]}}
            if method == 'likelist':
                return {'ids': list(liked)}
            if method == 'song_detail':
                return {'songs': [{'id': int(sid), 'name': '歌曲', 'ar': [{'name': '歌手'}], 'al': {'name': '专辑'}, 'dt': 200000} for sid in kw['ids'].split(',')]}
        n.call = call
        return n

    def test_ne_read_full_order_not_likelist_order(self):
        with patch('musicbridge.providers.time.sleep'):
            self.assertEqual(ids(self.ne(liked=('2','1')).read_likes()), ['1', '2'])

    def test_ne_count_mismatch(self):
        with self.assertRaises(SafetyError):
            self.ne(count=3).ordered_ids()

    def test_ne_real_likes_mismatch(self):
        with self.assertRaises(SafetyError):
            self.ne(liked=('1', '3')).ordered_ids()

    def test_ne_reject_ordinary_playlist(self):
        with self.assertRaises(SafetyError):
            self.ne(special=0).ordered_ids()

    def test_ne_reject_missing_metadata(self):
        n = self.ne()
        original = n.call
        n.call = lambda method, **kw: {'songs': []} if method == 'song_detail' else original(method, **kw)
        with self.assertRaises(SafetyError):
            n.read_likes()

    def test_identify_system_by_type_and_owner(self):
        n = Netease('.', cookie='mock-saved-cookie')
        n.call = lambda method, **kw: {'data': {'account': {'id': 123}}} if method == 'login_status' else {'playlist': [
            {'id': 111, 'name': '我喜欢的音乐', 'specialType': 0, 'creator': {'userId': 123}},
            {'id': 222, 'name': 'My music', 'specialType': 5, 'creator': {'userId': 123}},
        ]}
        self.assertEqual(n.connect(), '123')
        self.assertEqual(n.playlist, '222')

    def test_saved_netease_cookie_check_only_queries_login_status(self):
        n = Netease('.', cookie='mock-saved-cookie')
        with patch.object(n, 'call', return_value={'data': {'account': {'id': 123}}}) as call:
            self.assertEqual(n.check_account(quick=True), '123')
            self.assertEqual([args.args[0] for args in call.call_args_list], ['login_status'])
        with patch.object(n, 'call', return_value={'data': {'account': None}}):
            with self.assertRaises(RequestFailure) as err:
                n.check_account(quick=True)
            self.assertEqual(err.exception.kind, 'auth')
        with patch.object(n, 'call', return_value={'data': {}}):
            with self.assertRaises(RequestFailure) as err:
                n.check_account(quick=True)
            self.assertNotEqual(err.exception.kind, 'auth')

    def test_saved_qq_credential_requires_server_confirmation(self):
        q = QQ({'musicid': 123, 'encrypt_uin': 'private-uin', 'musickey': 'fake'})
        client = AsyncMock()
        client.__aenter__.return_value = NS(login=NS(check_expired=AsyncMock(side_effect=[False, True])))
        with patch('qqmusic_api.Client', return_value=client):
            self.assertEqual(asyncio.run(q.check_account()), '123')
            with self.assertRaises(RequestFailure) as err:
                asyncio.run(q.check_account())
        self.assertEqual(err.exception.kind, 'auth')

    def test_qq_parser(self):
        s = qq_song(raw_qq(7))
        self.assertEqual(s.id, '7')
        self.assertEqual(s.seconds, 200)
        self.assertIn('mid7', s.url)

    def fake_client(self, changed=False, fail=False):
        class Client:
            def __init__(self, **kw):
                self.user, self.calls = self, 0
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def get_fav_song(self, euin, page, num):
                self.calls += 1
                order = [2, 1] if changed and self.calls == 2 else [1, 2]
                return NS(code=0, subcode=0, total=3 if fail else 2,
                          songs=[raw_qq(x) for x in order] if page == 1 else [])
        return Client

    def test_qq_complete_second_pass(self):
        q = QQ({'musicid':123, 'encrypt_uin': 'private-uin', 'musickey':'fake'})
        with patch('qqmusic_api.Client', self.fake_client()):
            self.assertEqual(ids(asyncio.run(q.read())), ['1','2'])

    def test_qq_reject_unstable_order(self):
        q = QQ({'musicid':123, 'encrypt_uin': 'private-uin', 'musickey':'fake'})
        with patch('qqmusic_api.Client', self.fake_client(changed=True)):
            with self.assertRaises(SafetyError):
                asyncio.run(q.read())

    def test_qq_reject_truncated(self):
        q = QQ({'musicid':123, 'encrypt_uin': 'private-uin', 'musickey':'fake'})
        with patch('qqmusic_api.Client', self.fake_client(fail=True)):
            with self.assertRaises(SafetyError):
                asyncio.run(q.read())

    def test_qq_cancel(self):
        q = QQ({'musicid':123, 'encrypt_uin': 'private-uin', 'musickey':'fake'})
        with patch('qqmusic_api.Client', self.fake_client()):
            with self.assertRaises(SafetyError):
                asyncio.run(q.read(stopped=lambda: True))
