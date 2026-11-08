from __future__ import annotations
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import time
import random
import math
import re
from urllib.parse import urlsplit, parse_qs

from .core import LIKE_LIST_INTERVAL, Song, SafetyError, RequestFailure, collect_pages, ids
from .titles import search_title


def netease_song_id(value):
    value = value.strip()
    if value.isascii() and value.isdigit() and int(value) > 0:
        return value
    url = urlsplit(value)
    if url.scheme not in ('http', 'https') or url.hostname not in ('music.163.com', 'y.music.163.com'):
        raise SafetyError('请输入网易云歌曲的完整链接或纯数字歌曲 ID；短链接请先在浏览器打开后复制完整链接')
    route = urlsplit(url.fragment) if url.fragment.startswith('/') else url
    if route.path.rstrip('/') not in ('/song', '/m/song'):
        match = re.fullmatch(r'/(?:m/)?song/(\d+)/?', route.path)
        sid = match.group(1) if match else ''
    else:
        sid = parse_qs(route.query).get('id', [''])[0]
    if not sid.isascii() or not sid.isdigit() or int(sid) <= 0:
        raise SafetyError('链接中没有有效的歌曲 ID，请使用歌曲链接，而非歌单或专辑链接')
    return sid


def qq_song(s):
    return Song(str(s.id), s.title or s.name, tuple(a.name for a in s.singer), s.album.name,
                s.interval, url=f'https://y.qq.com/n/ryqq/songDetail/{s.mid}', version=s.subtitle)


class MetadataError(SafetyError):
    def __init__(self, field):
        self.field = field
        super().__init__(f'网易云歌曲元数据不完整或格式异常：{field}。未将此记录当成可确认歌曲')


class SearchResults(list):
    def __init__(self):
        super().__init__()
        self.metadata_issues = []
        self.has_more = False
        self.next_offset = 0


def ne_song(s, position=0):
    if not isinstance(s, dict):
        raise MetadataError('song')
    sid = str(s.get('id') or '')
    if not sid.isascii() or not sid.isdigit() or int(sid) <= 0:
        raise MetadataError('id')
    title = s.get('name')
    if not isinstance(title, str) or not title.strip():
        raise MetadataError('name')
    ar = s.get('ar', s.get('artists'))
    if not isinstance(ar, list) or not ar:
        raise MetadataError('artists')
    if any(not isinstance(a, dict) or not isinstance(a.get('name'), str) or not a['name'].strip() for a in ar):
        raise MetadataError('artists.name')
    al = s.get('al', s.get('album', {})) or {}
    if not isinstance(al, dict):
        raise MetadataError('album')
    album = al.get('name') or ''
    if not isinstance(album, str):
        raise MetadataError('album.name')
    duration = s.get('dt', s.get('duration', 0)) or 0
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
        raise MetadataError('duration')
    aliases = s.get('alia') or []
    version = aliases if isinstance(aliases, str) else ' '.join(x for x in aliases if isinstance(x, str)) if isinstance(aliases, list) else ''
    return Song(sid, title.strip(), tuple(a['name'].strip() for a in ar), album,
                duration / 1000, position, f'https://music.163.com/#/song?id={sid}', version)


class QQ:
    def __init__(self, credential=None, device_path=None):
        self.credential = credential
        self.device_path = device_path

    async def check_account(self):
        """Validate a saved QQ credential without opening a QR or reading a playlist."""
        from qqmusic_api import Client, Credential
        from qqmusic_api.core.exceptions import CredentialExpiredError, CredentialInvalidError, LoginAuthExpiredError
        if not self.credential or not self.credential.get('musicid') or not self.credential.get('encrypt_uin'):
            raise RequestFailure('QQ 音乐未登录或授权不完整', 'auth')
        try:
            credential = Credential.model_validate(self.credential)
            async with Client(credential=credential, device_path=self.device_path, rate=1, capacity=1) as client:
                if await asyncio.wait_for(client.login.check_expired(), 12):
                    raise RequestFailure('QQ 音乐授权已过期', 'auth')
        except (CredentialExpiredError, CredentialInvalidError, LoginAuthExpiredError):
            raise RequestFailure('QQ 音乐授权已过期，请重新扫码', 'auth') from None
        return str(credential.musicid)

    async def login(self, show, stopped=lambda: False):
        from qqmusic_api import Client
        from qqmusic_api.models.login import QRLoginType, QRCodeLoginEvents
        async with Client(device_path=self.device_path, rate=1, capacity=1) as c:
            qr = await asyncio.wait_for(c.login.get_qrcode(QRLoginType.QQ), 40)
            show(qr.data)
            for _ in range(90):
                if stopped():
                    raise SafetyError('已取消登录')
                await asyncio.sleep(2)
                result = await asyncio.wait_for(c.login.check_qrcode(qr), 35)
                if result.done:
                    self.credential = result.credential.model_dump()
                    return self.credential
                if result.event in (QRCodeLoginEvents.TIMEOUT, QRCodeLoginEvents.REFUSE):
                    raise SafetyError('二维码过期或已拒绝，请重新登录')
            raise SafetyError('扫码等待超时')

    async def read(self, log=lambda _: None, stopped=lambda: False):
        from qqmusic_api import Client, Credential
        from qqmusic_api.core.exceptions import NetworkError
        if not self.credential:
            raise SafetyError('请先登录 QQ 音乐')
        credential = Credential.model_validate(self.credential)
        if not credential.encrypt_uin:
            raise SafetyError('QQ 登录凭据缺少 encrypt_uin，请重新授权')
        async with Client(credential=credential, device_path=self.device_path, rate=1, capacity=1) as c:
            async def all_pages():
                result, seen, total = [], set(), None
                for page in range(1, 100001):
                    if stopped():
                        raise SafetyError('来源读取已取消，本次未完整读取的数据不会用于迁移')
                    response = None
                    for attempt in range(3):
                        try:
                            response = await asyncio.wait_for(c.user.get_fav_song(credential.encrypt_uin, page=page, num=100), 40)
                            break
                        except (asyncio.TimeoutError, ConnectionError, OSError, NetworkError):
                            if attempt == 2:
                                raise SafetyError(f'QQ 第 {page} 页网络失败，读取未完成') from None
                            await asyncio.sleep(2 ** attempt)
                    if response.code or response.subcode:
                        raise SafetyError(f'QQ 第 {page} 页返回业务错误，请检查登录/访问限制')
                    if total is None:
                        total = response.total
                    if response.total != total:
                        raise SafetyError('QQ 读取期间总数变化')
                    for raw in response.songs:
                        song = qq_song(raw)
                        if not song.id or song.id in seen:
                            raise SafetyError('QQ 分页出现重复 ID')
                        seen.add(song.id)
                        result.append(Song(**{**asdict(song), 'position': len(result) + 1}))
                    log(f'QQ 已读取 {len(result)}/{total}')
                    if len(result) == total:
                        return result
                    if not response.songs or len(result) > total:
                        raise SafetyError(f'QQ 读取不完整：{len(result)}/{total}')
                    await asyncio.sleep(.6)
                raise SafetyError('QQ 分页超过安全上限')
            songs = await all_pages()
            # Detect edits and unstable paging by comparing a complete second pass.
            log('QQ 开始第二遍完整读取，检查序列稳定性')
            second = await all_pages()
            if ids(songs) != ids(second):
                raise SafetyError('QQ 两次完整读取的顺序不一致，请停止编辑后重试')
            return songs


class Netease:
    def __init__(self, bridge_dir, cookie='', node='node', log=lambda _: None, stopped=lambda: False, diagnostic_path=None):
        self.bridge_dir = Path(bridge_dir)
        self.cookie, self.node, self.account, self.playlist = cookie, node, '', ''
        self.log, self.stopped, self.diagnostic_path = log, stopped, diagnostic_path
        self.next_search = 0
        self.next_like_list = 0

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            if self.stopped():
                raise RequestFailure('已暂停，进度已保留', 'cancelled')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(.25, remaining))

    def diagnose(self, method, attempt, kind, status=None, code=None):
        if not self.diagnostic_path:
            return
        # No keyword, song title, user ID, Cookie, raw stdout or upstream message.
        record = {'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'method': method,
                  'attempt': attempt + 1, 'kind': kind, 'http_status': status, 'upstream_code': code}
        path = Path(self.diagnostic_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.exists() and path.stat().st_size > 1024 * 1024:
                path.replace(path.with_suffix('.previous.jsonl'))
            with path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(record) + '\n')
        except OSError:
            pass

    def call(self, method, write=False, *, attempts=None, process_timeout=45, **params):
        params.update(cookie=self.cookie, timestamp=int(time.time() * 1000), realIP='')
        attempts = 1 if write else (attempts or 3)
        for attempt in range(attempts):
            kind, status, code = 'network', None, None
            try:
                if method == 'cloudsearch':
                    self.wait(max(0, self.next_search - time.monotonic()))
                    self.next_search = time.monotonic() + 2.5 + random.uniform(0, .5)
                if method == 'likelist':
                    self.wait(max(0, self.next_like_list - time.monotonic()))
                    self.next_like_list = time.monotonic() + LIKE_LIST_INTERVAL
                proc = subprocess.run([self.node, str(self.bridge_dir / 'call.cjs')],
                                      input=json.dumps({'method': method, 'params': params}), text=True,
                                      encoding='utf-8', capture_output=True, timeout=process_timeout,
                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                lines = [line[10:] for line in proc.stdout.splitlines() if line.startswith('MB_RESULT:')]
                data = json.loads(lines[-1]) if lines else {}
                if isinstance(data, dict) and data.get('bridge_error'):
                    detail = data.get('detail') or {}
                    kind = detail.get('kind', 'upstream')
                    if kind not in ('auth', 'restricted', 'timeout', 'network', 'dependency', 'upstream'):
                        kind = 'upstream'
                    status, code = detail.get('http_status'), detail.get('upstream_code')
                    if kind in ('auth', 'restricted', 'dependency'):
                        self.diagnose(method, attempt, kind, status, code)
                        reason = {'auth': '授权失效，请重新登录', 'restricted': '平台拒绝或限制请求，请稍后在官方客户端检查', 'dependency': '本地 Node 依赖缺失，请重新安装'}[kind]
                        raise RequestFailure(f'网易云 {method}：{reason}（HTTP={status}，code={code}）', kind, status=status, code=code)
                    raise OSError('bridge failure')
                if proc.returncode or not isinstance(data, dict) or not data:
                    kind = 'protocol'
                    raise ValueError('invalid bridge response')
                code = data.get('code', data.get('data', {}).get('code', 200))
                if code in (301, 302, 401, 403, 405, 429):
                    kind = 'auth' if code in (301, 302, 401) else 'restricted'
                    self.diagnose(method, attempt, kind, code=code)
                    raise RequestFailure(f'网易云拒绝访问（{code}），请重新授权或稍后重试', kind, code=code)
                if code not in (200, 800, 801, 802, 803):
                    if isinstance(code, int) and 500 <= code <= 599:
                        kind = 'upstream'
                        raise OSError('upstream error')
                    raise RequestFailure(f'网易云返回业务错误（{code}）', 'business')
                return data
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                if isinstance(exc, subprocess.TimeoutExpired):
                    kind = 'timeout'
                elif isinstance(exc, FileNotFoundError):
                    kind = 'dependency'
                self.diagnose(method, attempt, kind, status, code)
                if kind == 'dependency':
                    raise RequestFailure('未找到 Node.js，请检查本地运行依赖', kind) from None
                if attempt + 1 == attempts:
                    suffix = '写入状态不确定，必须重读核对' if write else '本次未完成搜索/读取，已保留进度'
                    raise RequestFailure(f'网易云 {method}：{kind}（HTTP={status}，code={code}）；{suffix}',
                                         kind, transient=not write, status=status, code=code) from None
                delay = (10, 25)[attempt] if method == 'cloudsearch' else 2 ** attempt
                self.log(f'网易云 {method} 暂时失败（{kind}），等待 {delay} 秒后重试 {attempt + 2}/{attempts}，可点暂停')
                self.wait(delay)

    def check_account(self, *, quick=False):
        if not self.cookie:
            raise RequestFailure('网易云尚未扫码登录', 'auth')
        response = self.call('login_status', attempts=1, process_timeout=14) if quick else self.call('login_status')
        data = response.get('data')
        if not isinstance(data, dict) or 'account' not in data:
            raise RequestFailure('网易云登录状态暂无法核验', 'upstream')
        account = data.get('account') or {}
        if not isinstance(account, dict):
            raise RequestFailure('网易云登录状态暂无法核验', 'upstream')
        uid = account.get('id')
        if not uid or account.get('anonimousUser') or account.get('anonymousUser'):
            raise RequestFailure('网易云授权已过期，请重新扫码', 'auth')
        return str(uid)

    def connect(self):
        self.account = self.playlist = ''
        self.account = self.check_account()
        uid = int(self.account)
        found, offset = [], 0
        for _ in range(1000):
            page = self.call('user_playlist', uid=uid, limit=100, offset=offset)
            playlists = page.get('playlist', [])
            found += [p for p in playlists if p.get('specialType') == 5 and str((p.get('creator') or {}).get('userId')) == self.account]
            if not page.get('more'):
                break
            if not playlists:
                raise SafetyError('用户歌单分页不完整')
            offset += len(playlists)
        if len(found) != 1:
            raise SafetyError('不能唯一确认系统喜欢列表（specialType=5 且属于当前账号），拒绝使用普通歌单')
        self.playlist = str(found[0]['id'])
        return self.account

    def ordered_ids(self):
        p = self.call('playlist_detail', id=self.playlist)['playlist']
        if p.get('specialType') != 5 or str(p.get('userId', (p.get('creator') or {}).get('userId'))) != self.account:
            raise SafetyError('目标不是此账号系统喜欢列表')
        ordered = [str(x['id']) for x in p.get('trackIds', [])]
        liked = [str(x) for x in self.call('likelist', uid=self.account).get('ids', [])]
        if len(ordered) != p.get('trackCount') or len(ordered) != len(set(ordered)) or set(ordered) != set(liked):
            raise SafetyError('网易云完整列表与真实红心 ID 集合不一致；禁止不完整迁移')
        return ordered

    def read_likes(self):
        if not self.account:
            self.connect()
        ordered = self.ordered_ids()
        songs = {}
        for start in range(0, len(ordered), 200):
            page = self.call('song_detail', ids=','.join(ordered[start:start + 200]))
            for s in page.get('songs', []):
                parsed = ne_song(s)
                songs[parsed.id] = parsed
            time.sleep(.2)
        if set(songs) != set(ordered):
            raise SafetyError('网易云部分歌曲元数据无法读取；保留当前列表，停止写入')
        if self.ordered_ids() != ordered:
            raise SafetyError('网易云读取期间列表改变，请重读')
        return [Song(**{**asdict(songs[sid]), 'position': i + 1}) for i, sid in enumerate(ordered)]

    def search_keywords(self, keywords, offset=0):
        keywords = keywords.strip()
        if not keywords or len(keywords) > 300:
            raise SafetyError('请输入 1–300 个字符的搜索关键词')
        if not isinstance(offset, int) or offset < 0:
            raise SafetyError('搜索页码无效')
        data = self.call('cloudsearch', keywords=keywords, type=1, limit=30, offset=offset)
        block = data.get('result', {})
        if not isinstance(block, dict):
            raise RequestFailure('搜索响应的 result 格式异常，保留为待重试', 'response', True)
        batch = block.get('songs', [])
        if not isinstance(batch, list):
            raise RequestFailure('搜索响应的 songs 格式异常，保留为待重试', 'response', True)
        result = SearchResults()
        for index, raw in enumerate(batch):
            try:
                result.append(ne_song(raw))
            except MetadataError as exc:
                result.metadata_issues.append({'offset': offset, 'candidate_index': index, 'field': exc.field})
                self.diagnose('cloudsearch', 0, 'metadata:' + exc.field)
        total = block.get('songCount')
        result.has_more = len(batch) == 30 and (not isinstance(total, (int, float)) or offset + 30 < total)
        result.next_offset = offset + 30
        return result

    def song_by_reference(self, value):
        sid = netease_song_id(value)
        songs = self.call('song_detail', ids=sid).get('songs', [])
        if not isinstance(songs, list):
            raise SafetyError('歌曲详情响应格式异常')
        for raw in songs:
            if isinstance(raw, dict) and str(raw.get('id')) == sid:
                return ne_song(raw)
        raise SafetyError('没有读取到此 ID 对应的网易云歌曲，请检查歌曲链接或可访问状态')

    def search(self, source):
        result = SearchResults()
        title = search_title(source.title)
        queries = list(dict.fromkeys((title + ' ' + ' '.join(source.artists), title)))
        for query_index, query in enumerate(queries):
            for offset in (0, 30):
                batch = self.search_keywords(query, offset)
                result.extend(batch)
                result.metadata_issues.extend({**issue, 'query_index': query_index} for issue in batch.metadata_issues)
                if not batch.has_more:
                    break
        if result.metadata_issues:
            self.log(f'搜索排除 {len(result.metadata_issues)} 条元数据异常候选，正常候选 {len(result)} 条')
            if not result:
                raise RequestFailure('搜索候选元数据均不完整，保留为待重试，不计作未匹配', 'metadata', True)
        return result

    def like(self, sid):
        self.call('like', write=True, id=sid, like='true')

    def reorder(self, ordered):
        self.call('song_order_update', write=True, pid=self.playlist, ids=json.dumps(ordered))
