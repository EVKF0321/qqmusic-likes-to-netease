"""Durable search progress. Temporary per-track failures never become 'no match'."""
from .core import Song, rank, SafetyError, RequestFailure, atomic_json
from .titles import search_title
import copy
import time


def reconfirm_cached(store):
    """Apply user's new threshold to cached pending candidates, without network calls."""
    policy = 'score_gt_0.8_v1'
    if not store.data.get('source') or store.data.get('matching_policy') == policy:
        return 0
    matches = store.data.get('matches', {})
    before = copy.deepcopy(matches)
    changed = 0
    for raw in store.data['source']:
        match = matches.get(raw['id'], {})
        if match.get('method') in ('manual', 'skipped') or match.get('chosen') or match.get('error') or match.get('metadata_issues'):
            continue
        try:
            result = rank(Song.load(raw), [Song.load(c['song']) for c in match.get('candidates', [])])
        except (KeyError, TypeError, ValueError):
            continue
        if result['chosen']:
            matches[raw['id']] = {**match, **result}
            changed += 1
    if changed:
        atomic_json(store.root / 'backups' / f'matching-policy-{time.time_ns()}.json',
                    {'matches': before, 'reason': '应用用户要求的自动确认阈值 > 0.8'})
        store.data.pop('plan', None)
    store.data['matching_policy'] = policy
    store.save()
    return changed


def match_pending(api, store, log=lambda _: None, stopped=lambda: False):
    matches = store.data.setdefault('matches', {})
    failures = consecutive = completed = 0
    for i, raw in enumerate(store.data['source']):
        if stopped():
            break
        s = Song.load(raw)
        if matches.get(s.id, {}).get('method') in ('manual', 'skipped'):
            continue
        previous = matches.get(s.id, {})
        broaden = bool(previous and not previous.get('chosen') and search_title(s.title) != s.title.strip()
                       and previous.get('search_title') != search_title(s.title))
        if s.id in matches and not matches[s.id].get('error') and not broaden:
            continue
        try:
            candidates = api.search(s)
            combined = [Song.load(c['song']) for c in previous.get('candidates', [])] + list(candidates) if broaden else candidates
            match = rank(s, combined)
            match['search_title'] = search_title(s.title)
            issues = previous.get('metadata_issues', []) + list(getattr(candidates, 'metadata_issues', []) or []) if broaden else getattr(candidates, 'metadata_issues', None)
            if issues:
                match['metadata_issues'] = issues
                # Excluded candidates might change the ambiguity margin: require a human.
                match['chosen'] = None
                match['method'] = 'pending'
                match['auto_disabled_reason'] = '部分候选元数据缺失，请人工核对正常候选'
            matches[s.id] = match
            consecutive = 0
            completed += 1
        except RequestFailure as exc:
            # Retain previous/manual decisions and any candidates already obtained.
            previous = matches.get(s.id, {})
            matches[s.id] = {**previous, 'chosen': previous.get('chosen'),
                            'candidates': previous.get('candidates', []),
                            'method': previous.get('method', 'pending'), 'error': str(exc)}
            store.save()
            if exc.kind == 'cancelled':
                break
            failures += 1
            consecutive += 1
            log(f'第 {i + 1} 首搜索失败，已记入待重试：{exc}')
            if not exc.transient:
                raise
            if consecutive >= 3:
                raise SafetyError('连续 3 首搜索失败，已暂停以避免持续请求。进度已保存，请稍后续查；诊断见 data/diagnostics.jsonl')
            continue
        store.save()
        log(f'搜索匹配 {i + 1}/{len(store.data["source"])} · 本轮完成 {completed} · 待重试 {failures}')
    log(f'本轮搜索结束：完成 {completed} 首，临时失败 {failures} 首（保留为待重试，不算未匹配）')
    return {'completed': completed, 'failed': failures}
