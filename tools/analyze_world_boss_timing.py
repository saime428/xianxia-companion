"""离线复盘导出的 Boss 诊断；不连接游戏、不修改实战参数。

python tools/analyze_world_boss_timing.py evidence.json --out-dir tmp/boss-analysis
python tools/analyze_world_boss_timing.py --self-check
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics


def request_timeline(data):
    """Export v5 anchors without inventing wall times for older diagnostics.

    The bounded window log may only retain the last attempt's metadata alongside
    summed durations. Mark those summaries rather than presenting them as raw
    individual attempts. Server Date has second precision, not a combat clock.
    """
    rows = []
    for profile, state in data['profiles'].items():
        for event in state.get('world_boss_events', []):
            for result in event.get('identity_results', []):
                diag = result.get('diagnostics') or {}
                requests = [('begin', None, (diag.get('clock_sync') or {}).get('request') or {})]
                requests += [('start', item.get('sequence'), item.get('request') or {}) for item in (diag.get('entry') or {}).get('entry_requests', [])]
                requests += [('window', item.get('sequence'), item.get('request') or {}) for item in (diag.get('window_reveal') or {}).get('log', [])]
                for hit in diag.get('hits', []):
                    requests += [('charge-start', hit.get('sequence'), (hit.get('charge') or {}).get('request') or {}),
                                 ('hit', hit.get('sequence'), hit.get('request') or {})]
                requests += [('finish', None, (diag.get('finish') or {}).get('request') or {})]
                for endpoint, sequence, trace in requests:
                    if not trace:
                        continue
                    attempts = [a for a in trace.get('attempts', []) if isinstance(a, dict)]
                    for attempt in attempts or [trace]:
                        row = {'profile': profile, 'event': (result.get('server_result') or {}).get('event_id'),
                               'endpoint': endpoint, 'sequence': sequence,
                               'scope': 'attempt' if attempts else 'summary_last_attempt_metadata',
                               'attempt': attempt.get('attempt', trace.get('attempt_count'))}
                        keys = ('request_started_unix_ms', 'request_started_monotonic_ms',
                                'request_resumed_monotonic_ms', 'request_clock_step_ms',
                                'http_response_headers_monotonic_ms', 'http_server_date_unix_ms',
                                'http_cf_ray', 'http_server_timing_cf_edge_ms', 'http_server_timing_cf_origin_ms',
                                'executor_queue_ms', 'transport_ms', 'loop_resume_ms',
                                'http_pool_dispatch_ms', 'http_connect_ms', 'http_tls_ms', 'http_headers_wait_ms')
                        row.update({key: attempt.get(key) for key in keys})
                        timestamp = row['request_started_unix_ms']
                        row['started_utc'] = None
                        if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) and 0 <= timestamp <= 4102444800000:
                            row['started_utc'] = datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat(timespec='milliseconds')
                        rows.append(row)
    return sorted(rows, key=lambda r: (r['request_started_unix_ms'] if r['started_utc'] else float('inf'), r['profile']))


def merge(intervals):
    result = []
    for lo, hi in sorted(intervals):
        if lo > hi:
            continue
        if result and lo <= result[-1][1]:
            result[-1][1] = max(hi, result[-1][1])
        else:
            result.append([lo, hi])
    return result


def bias_intervals(hit, slack=2):
    """B = 本地起点晚于服务端起点的量；保留绝对 delta 的早/晚两个可能分支。"""
    delta = (hit.get('server_hit') or {}).get('deltaMs')
    sent, received, center = (hit.get(k) for k in ('sent_elapsed_ms', 'request_completed_elapsed_ms', 'center_ms'))
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (delta, sent, received, center)) or delta < 0 or received < sent:
        return []
    return merge([[center + sign * delta - received - slack, center + sign * delta - sent + slack] for sign in (-1, 1)])


def shared_bias(hits):
    feasible = [[-10000, 10000]]
    count = 0
    zero_compatible = 0
    for hit in hits:
        ranges = bias_intervals(hit)
        if not ranges:
            continue
        count += 1
        zero_compatible += any(lo <= 0 <= hi for lo, hi in ranges)
        feasible = merge([[max(a, c), min(b, d)] for a, b in feasible for c, d in ranges])
    return {'samples': count, 'zero_bias_compatible_hits': zero_compatible,
            'shared_bias_ms': [[round(a, 2), round(b, 2)] for a, b in feasible] if count else []}


def stats(values):
    values = sorted(v for v in values if isinstance(v, (int, float)))
    return {'count': len(values), 'median': round(statistics.median(values), 3),
            'max': round(max(values), 3), 'over_100': sum(v > 100 for v in values)} if values else {}


def analyze(data):
    summaries, rows = [], []
    for profile_id, state in data['profiles'].items():
        for event in state.get('world_boss_events', []):
            date = str(event.get('started_at') or event.get('updated_at') or '')[:10]
            for result in event.get('identity_results', []):
                diagnostics = result.get('diagnostics') or {}
                hits = diagnostics.get('hits') or []
                if not hits:
                    continue
                server = result.get('server_result') or {}
                clock = diagnostics.get('clock_sync') or {}
                begin = clock.get('request') or {}
                successful = next((a for a in reversed(begin.get('attempts', [])) if isinstance(a, dict) and a.get('ok')), {})
                reveal = diagnostics.get('window_reveal') or {}
                accepted = [h for h in hits if h.get('server_status') == 'accepted']
                damages = [(h.get('server_hit') or {}).get('damageYi', 0) for h in hits]
                total = sum(damages)
                if server.get('realtime_damage_yi') is not None:
                    assert total == server['realtime_damage_yi'], (profile_id, date, 'damage mismatch')
                summary = {
                    'profile': profile_id, 'date': date, 'event': server.get('event_id'),
                    'damage_yi': total, 'hits': len(accepted), 'windows': len(hits),
                    'perfects': sum(bool(h.get('accepted_perfect')) for h in hits),
                    'local_perfects': sum(bool(h.get('local_perfect')) for h in hits),
                    'attack_bonus': (diagnostics.get('player') or {}).get('attackBonus'),
                    'clock_rtt_ms': clock.get('round_trip_ms'), 'begin': successful,
                    'clock_source': begin.get('clock_rtt_source'),
                    'guard_checks': diagnostics.get('guard_checks'),
                    'wake': stats([h.get('wake_lateness_ms') for h in hits]),
                    'server_delta': stats([(h.get('server_hit') or {}).get('deltaMs') for h in hits]),
                    'server_hold': stats([h.get('server_hold_ms') for h in hits]),
                    'arrival_directions': (diagnostics.get('strategy') or {}).get('drift_direction_counts'),
                    'bias': shared_bias(accepted),
                    'hold_outside_band': sum(not 520 <= h.get('server_hold_ms', 1000) <= 1250 for h in accepted),
                    'timing_outside_perfect': sum((h.get('server_hit') or {}).get('deltaMs', 0) > h.get('perfect_ms', 210) for h in accepted),
                    'request_timing': {},
                }
                for stage in ('hit', 'charge'):
                    traces = [(h if stage == 'hit' else h.get('charge', {})).get('request') or {} for h in hits]
                    summary['request_timing'][stage] = {key: stats([trace.get(key) for trace in traces]) for key in ('total_duration_ms','guard_ms','executor_queue_ms','transport_ms','loop_resume_ms')}
                windows = [item for item in reveal.get('log', []) if isinstance(item, dict) and item.get('status') == 'revealed']
                summary['window_reveal'] = {'count': len(windows), 'minimum_lead_ms': reveal.get('min_lead_ms'),
                                            'request_ms': stats([(item.get('request') or {}).get('total_duration_ms') for item in windows])}
                summaries.append(summary)
                for h in hits:
                    request = h.get('request') or {}
                    charge = (h.get('charge') or {}).get('request') or {}
                    sh = h.get('server_hit') or {}
                    row = {'profile': profile_id, 'date': date, 'event': server.get('event_id')}
                    row.update({key:h.get(key) for key in ('sequence','center_ms','hit_ms','perfect_ms','account_offset_ms','schedule_lead_ms','target_ms','sent_elapsed_ms','request_completed_elapsed_ms','wake_lateness_ms','hold_ms','server_hold_ms','accepted_perfect','error')})
                    row.update(damage_yi=sh.get('damageYi',0), server_delta_ms=sh.get('deltaMs'), direction=(h.get('arrival_inference') or {}).get('direction'))
                    for prefix, trace in (('hit',request),('charge',charge)):
                        row.update({prefix+'_'+key:trace.get(key) for key in ('total_duration_ms','guard_ms','executor_queue_ms','transport_ms','loop_resume_ms')})
                    rows.append(row)
    return summaries, rows


def self_check():
    # 两击共同锁定 +300ms；原控制器拿本地时基解释 server delta，会误判为区间之外。
    def hit(sent, received, delta):
        return dict(sent_elapsed_ms=sent, request_completed_elapsed_ms=received, center_ms=1000, server_hit={'deltaMs':delta})
    sample = [hit(1050,1070,360), hit(950,970,260)]
    assert shared_bias(sample)['shared_bias_ms'] == [[288,312]]
    assert shared_bias(sample)['zero_bias_compatible_hits'] == 0
    assert shared_bias([hit(990,1010,0)])['shared_bias_ms'] == [[-12,12]]
    assert shared_bias([{'server_hit':{}}])['samples'] == 0
    assert shared_bias([{'server_hit':{}}])['shared_bias_ms'] == []
    assert bias_intervals(hit(1070,1050,30)) == []
    assert bias_intervals(hit(1050,1070,float('nan'))) == []
    assert merge([[2,1],[0,2],[1,3],[5,6]]) == [[0,3],[5,6]]
    data = {'profiles': {'2': {'world_boss_events': [{'identity_results': [{
        'server_result': {'event_id': 143}, 'diagnostics': {'hits': [{'sequence': 1, 'request': {
            'attempts': ['<truncated>'], 'attempt_count': 2, 'transport_ms': 500,
            'request_started_unix_ms': 1790947800123, 'http_cf_ray': '0123456789abcdef-HNL',
        }}]}}
    ]}]}}}
    timeline = request_timeline(data)
    assert len(timeline) == 1 and timeline[0]['scope'] == 'summary_last_attempt_metadata'
    assert timeline[0]['started_utc'] == '2026-10-02T13:30:00.123+00:00'
    assert timeline[0]['http_server_date_unix_ms'] is None
    print('world boss timing analysis: ok')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', nargs='?', type=Path)
    parser.add_argument('--out-dir', type=Path)
    parser.add_argument('--self-check', action='store_true')
    args = parser.parse_args()
    if args.self_check:
        self_check()
    else:
        if not args.evidence or not args.out_dir:
            parser.error('evidence and --out-dir are required')
        raw = args.evidence.read_bytes()
        data = json.loads(raw)
        summaries, rows = analyze(data)
        requests = request_timeline(data)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        (args.out_dir/'summary.json').write_text(json.dumps({'evidence_sha256':hashlib.sha256(raw).hexdigest(),'battles':summaries,
            'request_observations': {'count': len(requests), 'with_utc_anchor': sum(r['started_utc'] is not None for r in requests)}},ensure_ascii=False,indent=2),encoding='utf-8')
        with (args.out_dir/'hits.csv').open('w',encoding='utf-8',newline='') as file:
            writer=csv.DictWriter(file,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
        if requests:
            with (args.out_dir/'requests.csv').open('w',encoding='utf-8',newline='') as file:
                writer=csv.DictWriter(file,fieldnames=list(requests[0]));writer.writeheader();writer.writerows(requests)
        print(json.dumps({'battles':len(summaries),'hits':len(rows),'output':str(args.out_dir)},ensure_ascii=False))
