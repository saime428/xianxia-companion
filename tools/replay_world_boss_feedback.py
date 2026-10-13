"""Compare feedback controllers against saved, sanitized observations offline.

This is a fixed-observation replay, not a prediction of damage or final rank.
Both controllers see the same recorded server/local holds and request excess.
"""
import argparse
import ast
import json
from pathlib import Path
import random
import sys
import tempfile
from types import MethodType
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
with patch('dotenv.load_dotenv', return_value=False):
    from test_world_boss_combat import Clock, boss, monitor_for


def baseline_feedback(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    monitor = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == 'WorldBossMonitor')
    method = next(node for node in monitor.body
                  if isinstance(node, ast.FunctionDef) and node.name == '_observe_hold_feedback')
    namespace = dict(vars(boss))
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['_observe_hold_feedback']


def replay(hits, root, old_feedback=None):
    monitor, _ = monitor_for(root, Clock())
    monitor._timing.enabled = True
    monitor._timing.rng = random.Random(42)
    if old_feedback is not None:
        monitor._observe_hold_feedback = MethodType(old_feedback, monitor)
    rows = []
    for hit in hits:
        plan = monitor._timing.plan(hit['perfect_ms'], monitor._planned_hold_ms(), monitor._hold_skew_ms)
        feedback = None
        if hit.get('accepted', True):
            _, feedback = monitor._observe_hold_feedback(
                hit['server_hold_ms'], hit['hold_ms'], perfect_ms=hit['perfect_ms'],
                late_delta_ms=hit['server_delta_ms'] if hit['arrival_direction'] == 'late' else None,
                request_excess_ms=hit['request_excess_ms'])
        rows.append({'sequence': hit['sequence'], 'hold_reserve_ms': plan.hold_reserve_ms,
                     'planned_hold_ms': plan.hold_ms, 'feedback': feedback})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.evidence.read_text(encoding='utf-8'))
    old_feedback = baseline_feedback(args.baseline)
    comparisons = []
    verified_observations = 0
    accepted_hits = 0
    with tempfile.TemporaryDirectory(prefix='boss-feedback-replay-') as directory:
        for profile, profile_data in data['profiles'].items():
            for index, event in enumerate(profile_data.get('events', [profile_data])):
                hits = event['hits']
                old = replay(hits, Path(directory) / profile / str(index) / 'old', old_feedback)
                new = replay(hits, Path(directory) / profile / str(index) / 'new')
                date = event.get('date', data.get('event_date'))
                changed = [a['sequence'] for a, b in zip(old, new)
                           if (a['hold_reserve_ms'], a['planned_hold_ms'])
                           != (b['hold_reserve_ms'], b['planned_hold_ms'])]
                comparisons.append({'profile': profile, 'date': date,
                                    'changed_sequences': changed, 'before': old, 'after': new})
                accepted_hits += sum(hit.get('accepted', True) for hit in hits)
                if event.get('verify_baseline', True):
                    for row, recorded in zip(old, hits):
                        assert row['feedback'] == recorded['hold_feedback'], (profile, date, row, recorded)
                        assert row['hold_reserve_ms'] == recorded['hold_reserve_ms'], (profile, date, row, recorded)
                        verified_observations += 1
    big = next(row for row in comparisons if row['profile'] == '2' and row['date'] == '2026-10-12')
    assert all(row['hold_reserve_ms'] >= 399 for row in big['before'][8:12])
    assert all(row['hold_reserve_ms'] == 100 for row in big['after'][8:12])
    assert all(row['planned_hold_ms'] >= 1050 for row in big['after'][8:12])
    for row in comparisons:
        if (row['profile'], row['date']) != ('2', '2026-10-12'):
            assert not row['changed_sequences'], (row['profile'], row['date'], row['changed_sequences'])
    result = {'battles': len(comparisons), 'accepted_hits': accepted_hits,
              'baseline_verified_observations': verified_observations, 'comparisons': comparisons,
              'limitation': 'Fixed-observation replay; predicted holds are illustrative. '
                            'No game requests, damage estimates, or rank predictions.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Feedback replay passed: {len(comparisons)} battles, {accepted_hits} accepted hits; '
          f'baseline matches {verified_observations} current-version observations; '
          'only 2026-10-12 profile 2 plans change, recovered-window reserve ~400ms -> 100ms.')


if __name__ == '__main__':
    main()
