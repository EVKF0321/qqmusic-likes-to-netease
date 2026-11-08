from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable


class SafetyError(RuntimeError):
    pass


class RequestFailure(SafetyError):
    def __init__(self, message, kind='network', transient=False, *, status=None, code=None):
        super().__init__(message)
        self.kind, self.transient = kind, transient
        self.status, self.code = status, code
        self.rate_limited = kind == 'restricted' and (code in (405, 429) or status == 429)


AUTO_CONFIRM_THRESHOLD = .80
LIKE_LIST_INTERVAL = 12
MIGRATION_WRITE_INTERVAL = 20
RESTRICTION_COOLDOWNS = (120, 240, 480, 960)


@dataclass(frozen=True)
class Song:
    id: str
    title: str
    artists: tuple[str, ...]
    album: str = ''
    seconds: float = 0
    position: int = 0
    url: str = ''
    version: str = ''

    @classmethod
    def load(cls, d):
        return cls(**{**d, 'artists': tuple(d['artists'])})


def ids(songs):
    return [s.id for s in songs]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def backup(folder, name, songs, **meta):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    atomic_json(folder / f'{name}.json', {'time': time.time(), 'songs': [asdict(s) for s in songs], **meta})
    with (folder / f'{name}.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['原始序号', '歌曲ID', '歌名', '歌手', '专辑', '时长秒', '版本', '链接'])
        for s in songs:
            row = [s.position, s.id, s.title, ' / '.join(s.artists), s.album, s.seconds, s.version, s.url]
            # Protect spreadsheet applications from formula injection.
            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in row])


def collect_pages(fetch: Callable, size=100):
    result, seen, total = [], set(), None
    for page in range(1, 100001):
        batch, count = fetch(page, size)
        if not isinstance(count, int) or count < 0:
            raise SafetyError('源平台没有可靠总数，无法确认完整性')
        if total is None:
            total = count
        if total != count:
            raise SafetyError('读取期间歌曲总数变化，请停止修改列表并重读')
        for song in batch:
            if not song.id or song.id in seen:
                raise SafetyError('分页出现重复/空 ID，拒绝静默遗漏')
            seen.add(song.id)
            result.append(Song(**{**asdict(song), 'position': len(result) + 1}))
        if len(result) == total:
            return result
        if not batch or len(result) > total:
            raise SafetyError(f'读取不完整：期望 {total} 首，取得 {len(result)} 首')
    raise SafetyError('分页安全上限已达到')


def norm(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


VERSION_PATTERNS = {
    'live': r'\blive\b|现场|演唱会',
    'remix': r'\bremix\b|混音',
    'cover': r'\bcover\b|翻唱',
    'instrumental': r'\binstrumental\b|伴奏|纯音乐|\bkaraoke\b',
    'acoustic': r'\bacoustic\b|不插电',
    'remaster': r'\bremaster(?:ed)?\b|重制',
    'demo': r'\bdemo\b|小样',
    'sped': r'\bsped\b|\bslowed\b|加速|降速',
}


def versions(s):
    # Album names often identify live recordings when the track title does not.
    t = ' '.join((s.title, s.album, s.version)).casefold()
    return {k for k, v in VERSION_PATTERNS.items() if re.search(v, t)}


def score(source: Song, candidate: Song):
    reasons = []
    title = SequenceMatcher(None, norm(source.title), norm(candidate.title)).ratio()
    a, b = {norm(x) for x in source.artists if norm(x)}, {norm(x) for x in candidate.artists if norm(x)}
    artists = len(a & b) / len(a | b) if a and b else 0
    album = SequenceMatcher(None, norm(source.album), norm(candidate.album)).ratio() if source.album and candidate.album else 0
    delta = abs(source.seconds - candidate.seconds) if source.seconds and candidate.seconds else None
    duration = max(0, 1 - delta / 12) if delta is not None else 0
    value = 0.43 * title + 0.32 * artists + 0.10 * album + 0.15 * duration
    if versions(source) != versions(candidate):
        reasons.append('版本标签冲突')
        value = min(value, .45)
    if not artists:
        reasons.append('歌手未对应')
        value = min(value, .45)
    if title < .92:
        value = min(value, .84)
    if delta is not None and delta > max(10, source.seconds * .05):
        reasons.append('时长差异过大')
        value = min(value, .60)
    if delta is None:
        reasons.append('缺少时长')
        value = min(value, .82)
    if artists < 1:
        value = min(value, .84)
    return round(value, 4), reasons


def rank(source, candidates):
    unique = {c.id: c for c in candidates}
    rows = [{'song': asdict(c), 'score': score(source, c)[0], 'reasons': score(source, c)[1]} for c in unique.values()]
    rows.sort(key=lambda x: (-x['score'], x['song']['id']))
    high = bool(rows and rows[0]['score'] > AUTO_CONFIRM_THRESHOLD)
    return {'candidates': rows, 'chosen': rows[0]['song'] if high else None,
            'method': 'auto' if high else 'pending', 'error': ''}


def verify(expected, actual):
    expected = list(dict.fromkeys(expected))
    positions = {v: i for i, v in enumerate(actual)}
    present = [x for x in expected if x in positions]
    projected = [x for x in actual if x in set(expected)]
    missing = [x for x in expected if x not in positions]
    differences = [{'position': i + 1, 'expected': a, 'actual': b} for i, (a, b) in enumerate(zip(present, projected)) if a != b]
    return {'missing': missing, 'duplicates': len(actual) != len(set(actual)),
            'relative_order_ok': present == projected, 'order_differences': differences,
            'reversed': len(present) > 1 and present[::-1] == projected,
            'confirmed_liked': len(present)}


class Workspace:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'session.json'
        self.data = json.loads(self.path.read_text('utf-8')) if self.path.exists() else {}

    def save(self):
        atomic_json(self.path, self.data)

    def record_target(self, account, live_ids, migrated=()):
        """Keep the last read-back result scoped to its actual account."""
        previous = self.data.get('target_state', {})
        confirmed = set(previous.get('migrated', ())) if previous.get('account') == account else set()
        confirmed.update(migrated)
        self.data['target_state'] = {
            'account': account, 'ids': list(live_ids),
            'migrated': [sid for sid in live_ids if sid in confirmed],
        }

    def snapshots(self, source, target, account):
        stamp = time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.time_ns() % 1000000)
        folder = self.root / 'backups' / stamp
        backup(folder, 'qq', source, order=self.data.get('source_order', '尚待界面确认'))
        backup(folder, 'netease', target, account=account)
        return str(folder)


@dataclass(frozen=True)
class MigrationProgress:
    phase: str
    total: int
    confirmed: int
    current: str | None = None
    verified_song: str | None = None


class Engine:
    """No unlike operation exists. All writes require caller's explicit approval."""
    def __init__(self, api, store, log=lambda _: None, stopped=lambda: False, on_progress=lambda _: None):
        self.api, self.store, self.log, self.stopped = api, store, log, stopped
        self.on_progress = on_progress
        self.last_write = float('-inf')
        self.write_interval = MIGRATION_WRITE_INTERVAL

    def selected(self):
        return [Song.load(self.store.data['matches'][s['id']]['chosen'])
                for s in self.store.data['source'] if self.store.data.get('matches', {}).get(s['id'], {}).get('chosen')]

    def current(self):
        return self.api.ordered_ids() if hasattr(self.api, 'ordered_ids') else ids(self.api.read_likes())

    def wait(self, seconds):
        while seconds > 0 and not self.stopped():
            step = min(1, seconds)
            time.sleep(step)
            seconds -= step
        if self.stopped():
            raise RequestFailure('已暂停，进度已保存', 'cancelled')

    def wait_for_write_slot(self):
        self.wait(max(0, self.last_write + self.write_interval - time.monotonic()))

    def cooldown(self, seconds):
        self.write_interval = min(60, self.write_interval * 3 // 2)
        self.log(f'网易云暂时限制请求。等待 {seconds // 60} 分钟后自动核对，本轮写入间隔调至 {self.write_interval} 秒')
        self.wait(seconds)

    def read_with_cooldown(self, read):
        for delay in (*RESTRICTION_COOLDOWNS, None):
            try:
                return read()
            except RequestFailure as exc:
                if not exc.rate_limited or delay is None:
                    raise
                self.cooldown(delay)

    def write_and_verify(self, write, before, expected, *, next_write=False, verify_server_error=False):
        for delay in (*RESTRICTION_COOLDOWNS, None):
            self.wait(0)
            self.last_write = time.monotonic()
            try:
                write()
                break
            except RequestFailure as exc:
                check_server_error = verify_server_error and exc.kind == 'upstream' and 500 in (exc.status, exc.code)
                if check_server_error:
                    self.log('网易云写入返回 500，等待后核对该曲是否加入')
                    self.wait_for_write_slot()
                elif exc.rate_limited and delay is not None:
                    self.cooldown(delay)
                else:
                    raise
                actual = self.read_with_cooldown(self.current)
                if actual == expected:
                    return True
                if actual != before:
                    raise SafetyError('等待期间目标列表发生变化，请重新预览')
                if check_server_error:
                    return False
        if next_write:
            # Read after the pacing wait so the same live snapshot also guards the next write.
            self.wait_for_write_slot()
        self.settle(expected, read=self.read_with_cooldown)
        return True

    def settle(self, expected, read=None):
        read = read or (lambda operation: operation())
        for attempt in range(4):
            live = read(self.current)
            if live == expected:
                return live
            if attempt < 3:
                self.wait(1.5 * (attempt + 1))
        raise SafetyError('写后读取与期望不一致；可能有延迟或外部修改，已暂停。请重新预览')

    def probe(self, approved=False, reorder=False, approved_ids=None):
        if not approved:
            raise SafetyError('排序实测需要明确同意真实新增两首喜欢')
        source = [Song.load(s) for s in self.store.data['source']]
        before = self.api.read_likes()
        self.store.snapshots(source, before, self.api.account)
        baseline = ids(before)
        candidates = list(dict.fromkeys(s.id for s in self.selected() if s.id not in baseline))[:2]
        if len(candidates) != 2:
            raise SafetyError('需要两首已确认匹配且尚未喜欢的歌曲来验证排序；已有喜欢不会被取消')
        if approved_ids is not None and candidates != approved_ids:
            raise SafetyError('实时实测歌曲与授权预览不符，未执行写入')
        evidence = {'account': self.api.account, 'direction': None, 'reorder': False, 'time': time.time(), 'tested_ids': candidates}
        self.store.data.pop('evidence', None)
        self.store.save()
        observed = baseline
        direction = None
        for sid in candidates:
            if self.stopped():
                raise SafetyError('实测已暂停，已新增的歌曲保留')
            if self.current() != observed:
                raise SafetyError('排序测试前发现列表已改变')
            self.api.like(sid)
            for attempt in range(4):
                after = self.current()
                if sid in after:
                    break
                time.sleep(2)
            if not observed and after == [sid]:
                now = None
            elif after == [sid] + observed:
                now = 'prepend'
            elif after == observed + [sid]:
                now = 'append'
            else:
                raise SafetyError('未能验证新喜欢置顶/置底规则，已停止；已新增的歌曲保留')
            if direction is not None and now is not None and now != direction:
                raise SafetyError('两次测试观察到的排序不一致')
            # An empty baseline cannot distinguish direction; use the second observation.
            direction = now or direction
            observed = after
        evidence['direction'] = direction
        if reorder:
            if self.current() != observed or self.stopped():
                raise SafetyError('重排实测前列表变化或用户暂停，未执行重排')
            proposed = observed.copy()
            i, j = proposed.index(candidates[0]), proposed.index(candidates[1])
            proposed[i], proposed[j] = proposed[j], proposed[i]
            self.api.reorder(proposed)
            self.settle(proposed)
            evidence['reorder'] = True
            observed = proposed
        evidence['snapshot'] = observed
        self.store.data['evidence'] = evidence
        self.store.record_target(self.api.account, observed, candidates)
        self.store.save()
        return evidence

    def can_resume(self, mode, account=None):
        data = self.store.data
        account = self.api.account if account is None else account
        ev = data.get('evidence', {})
        report = data.get('report', {})
        checkpoint = data.get('resume_order') or (
            data.get('plan') if data.get('inflight') else None) or (
            report.get('plan') if report.get('status') in ('paused', 'unverified') else None)
        return bool(checkpoint
                    and checkpoint.get('account') == ev.get('account') == account
                    and account
                    and checkpoint.get('source_hash') == digest(data.get('source', []))
                    and checkpoint.get('mode') == mode
                    and checkpoint.get('direction') == ev.get('direction')
                    and ev.get('direction') in ('prepend', 'append'))

    def plan(self, mode='merge'):
        if not self.store.data.get('source_confirmed'):
            raise SafetyError('请先确认完整来源和界面显示顺序')
        ev = self.store.data.get('evidence', {})
        if (ev.get('account') != self.api.account or ev.get('direction') not in ('prepend', 'append')
                or (time.time() - ev.get('time', 0) > 86400 and not self.can_resume(mode))):
            raise SafetyError('新迁移请先在此账号做排序实测（有效期 24 小时）；续传请沿用原账号、来源和迁移模式')
        desired = ids(self.selected())
        if len(desired) != len(set(desired)):
            raise SafetyError('多首来源匹配到同一个网易云 ID，请人工消除重复后重试')
        target = self.read_with_cooldown(self.api.read_likes)
        current = ids(target)
        if mode == 'sync':
            if not ev.get('reorder'):
                raise SafetyError('此账号未通过真实喜欢列表重排实测，严格同步不可用')
            extras = set(current) - set(desired)
            if extras:
                raise SafetyError(f'严格同步会影响 {len(extras)} 首额外收藏。本版本拒绝批量取消喜欢；可改用安全合并')
        additions = [x for x in desired if x not in current]
        operations = additions[::-1] if ev['direction'] == 'prepend' else additions
        self.store.snapshots([Song.load(s) for s in self.store.data['source']], target, self.api.account)
        plan = {'account': self.api.account, 'mode': mode, 'before': current, 'desired': desired,
                'operations': operations, 'direction': ev['direction'], 'source_hash': digest(self.store.data['source']),
                'mapping_hash': digest(self.store.data['matches']), 'created': time.time()}
        plan['preview_songs'] = [
            {'source_id': s['id'], 'source_position': s['position'], 'source_title': s['title'],
             'target': self.store.data['matches'][s['id']]['chosen'],
             'action': '已喜欢，保持' if self.store.data['matches'][s['id']]['chosen']['id'] in current else '新增真实喜欢'}
            for s in self.store.data['source'] if self.store.data.get('matches', {}).get(s['id'], {}).get('chosen')]
        self.store.data['plan'] = plan
        self.store.record_target(self.api.account, current)
        self.store.save()
        return plan

    def run(self, approved=False):
        if not approved:
            raise SafetyError('尚未确认变更预览')
        p = self.store.data.get('plan')
        if not p or p['account'] != self.api.account:
            raise SafetyError('请生成此账号的迁移预览')
        if time.time() - p['created'] > 900:
            raise SafetyError('预览已超过 15 分钟，请重新预览')
        if digest(self.store.data['source']) != p['source_hash'] or digest(self.store.data['matches']) != p['mapping_hash']:
            raise SafetyError('来源或匹配发生变化，请重新预览')
        operations = p['operations']
        self.on_progress(MigrationProgress('checking', len(operations), 0))
        source = [Song.load(s) for s in self.store.data['source']]
        target = self.read_with_cooldown(self.api.read_likes)
        if ids(target) != p['before']:
            raise SafetyError('目标列表在预览后发生变化，请重新预览（续传也需重新核对）')
        backup_path = self.store.snapshots(source, target, self.api.account)
        # The fresh approved baseline already resolves any pending write from an earlier run.
        self.store.data.pop('inflight', None)
        self.store.data['resume_order'] = {key: p[key] for key in ('account', 'source_hash', 'mode', 'direction')}
        self.store.record_target(self.api.account, p['before'])
        self.store.save()
        expected = p['before'].copy()
        errors, added = [], []
        status = 'running'
        blocked_song = None
        check_final = True
        self.log(f'低频迁移已开始，新增喜欢间隔至少 {self.write_interval} 秒')
        try:
            for index, sid in enumerate(operations):
                if index == 0:
                    self.wait_for_write_slot()
                    if self.read_with_cooldown(self.current) != expected:
                        raise SafetyError('检测到列表外部修改，停止后续写入')
                self.wait(0)
                self.on_progress(MigrationProgress('writing', len(operations), len(added), current=sid))
                self.store.data['inflight'] = {'id': sid, 'before': expected, 'time': time.time()}
                self.store.save()
                proposed = [sid] + expected if p['direction'] == 'prepend' else expected + [sid]
                confirmed = self.write_and_verify(
                    lambda: self.api.like(sid), expected, proposed,
                    next_write=index + 1 < len(operations), verify_server_error=True,
                )
                entry = {'id': sid, 'confirmed': confirmed, 'time': time.time()}
                if confirmed:
                    expected = proposed
                    added.append(sid)
                    self.store.record_target(self.api.account, expected, [sid])
                else:
                    entry['error_code'] = 500
                    blocked_song = sid
                self.store.data.setdefault('journal', []).append(entry)
                self.store.data.pop('inflight', None)
                self.store.save()
                if blocked_song:
                    raise SafetyError('写入返回 500，读回确认该曲未加入。已暂停在该曲位置，请核对或更换候选后重新预览。')
                self.on_progress(MigrationProgress('confirmed', len(operations), len(added), verified_song=sid))
                self.log(f'已重新读取确认新增 {index + 1}/{len(operations)} 首')
            if p['mode'] == 'sync' and expected != p['desired']:
                self.wait_for_write_slot()
                if self.read_with_cooldown(self.current) != expected:
                    raise SafetyError('重排前发现外部修改')
                self.write_and_verify(lambda: self.api.reorder(p['desired']), expected, p['desired'])
            status = 'finished'
        except Exception as exc:
            status = 'paused'
            errors.append(str(exc) if isinstance(exc, SafetyError) else f'{type(exc).__name__}：写入响应不确定，请重新预览并核对实时状态')
            if isinstance(exc, RequestFailure) and exc.kind in ('restricted', 'auth', 'cancelled'):
                check_final = False
                if exc.kind != 'cancelled':
                    status = 'unverified'
        report = {'status': status, 'complete_success': False, 'final_verified': False,
                  'source_total': len(source), 'target_before': len(p['before']),
                  'matched': len(p['desired']), 'newly_confirmed': len(added),
                  'blocked_song': blocked_song,
                  'pending_write': self.store.data.get('inflight'), 'target_final': None,
                  'errors': errors, 'backup': backup_path, 'plan': p}
        if check_final:
            self.on_progress(MigrationProgress('verifying', len(operations), len(added)))
            self.verify_run(report, source)
        if report['status'] == 'finished':
            self.store.data.pop('resume_order', None)
        report['time'] = time.time()
        self.store.data['report'] = report
        self.store.save()
        atomic_json(self.store.root / 'reports' / f'report-{time.time_ns()}.json', report)
        return report

    def verify_run(self, report, source):
        p = report['plan']
        try:
            final_songs = self.read_with_cooldown(self.api.read_likes)
            final = ids(final_songs)
            check = verify(p['desired'], final)
            # Re-check target metadata against each source. Manual choices stay labelled manual.
            by_id = {s.id: s for s in final_songs}
            identities = []
            for s in source:
                m = self.store.data.get('matches', {}).get(s.id, {})
                if m.get('chosen'):
                    sid = m['chosen']['id']
                    v, why = score(s, by_id[sid]) if sid in by_id else (0, ['目标不存在'])
                    identities.append({'source_id': s.id, 'target_id': sid, 'method': m['method'], 'score': v, 'reasons': why,
                                       'metadata_verified': v > AUTO_CONFIRM_THRESHOLD})
            preserved = set(p['before']) <= set(final)
            complete = report['status'] == 'finished' and not check['missing'] and check['relative_order_ok'] and not check['duplicates'] and preserved and len(p['desired']) == len(source) and all(x['metadata_verified'] for x in identities)
            backup(self.store.root / 'reports', 'target-final', final_songs)
            self.store.record_target(self.api.account, final)
            report.update({'complete_success': complete, 'final_verified': True,
                      'already_liked': len(set(p['desired']) & set(p['before'])),
                      'unmatched': [asdict(s) for s in source if not self.store.data.get('matches', {}).get(s.id, {}).get('chosen')],
                      'failed_songs': [s for s in p['operations'] if s not in final],
                      'unresolved_matches': self.store.data.get('matches', {}), 'target_final': len(final), 'verification': check,
                      'new_songs_order': verify([x for x in p['desired'] if x not in p['before']], final),
                      'identity_checks': identities, 'original_likes_preserved': preserved,
                      'limitations': ['歌曲身份按元数据核对；不下载音频，不能证明录音内容完全一致', '安全合并保留旧位置；严格同步拒绝删除额外收藏']})
        except Exception as exc:
            report['status'] = 'paused' if self.stopped() else 'unverified'
            report['errors'].append(str(exc) if isinstance(exc, SafetyError) else type(exc).__name__)
