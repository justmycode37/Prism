#!/usr/bin/env python3
"""Offline profile for repeated SOL cashflow-history gates on SQLite ledgers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'
import sys
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from prism_runtime.bybit import BybitAdapter
from prism_runtime.live_execution import ExecutionController
from prism_runtime.research_memory import ResearchMemory
from prism_runtime.store import Store


NOW_MS = 1_800_000_000_000
ACCOUNT_ID = 'offline-cashflow-profile'


class NoNetworkTransport:
    def request(self, *_args, **_kwargs):
        raise AssertionError('The cashflow profile must not make a transport request')


def _controller(store, adapter, clock):
    market = type('OfflineMarket', (), {'snapshot': lambda *_args, **_kwargs: {}})()
    return ExecutionController(store, adapter, market,
        ResearchMemory(store, clock=clock), None,
        eligibility=lambda _sid: {'eligible': False}, clock=clock)


def _insert_rows(store, count):
    day_start = (NOW_MS // 86_400_000) * 86_400_000
    rows = []
    for index in range(count):
        ident = f'cashflow-profile-{index:08d}'
        when = day_start + 1 + index
        item = {'id': ident, 'transactionTime': str(when), 'type': 'TRADE',
                'category': 'linear', 'symbol': 'SOLUSDT', 'currency': 'USDT',
                'cashFlow': '0', 'funding': '0', 'fee': '0', 'change': '0'}
        rows.append(('paper', ident, 'linear', 'SOLUSDT', 'USDT', when,
                     '0', '0', '0', '0', json.dumps(item, separators=(',', ':'))))
    with store.connection() as db:
        db.executemany('INSERT INTO bybit_transactions(mode,transaction_id,category,symbol,currency,'
            'transaction_time_ms,funding,fee,cash_flow,change_amount,value) VALUES (?,?,?,?,?,?,?,?,?,?,?)', rows)


def _raw_account():
    return {'account_id': ACCOUNT_ID, 'account_type': 'UNIFIED',
        'raw_wallet': {'accountType': 'UNIFIED'}, 'mode': 'paper', 'equity': 1000.,
        'available_balance': 900., 'updated_at_ms': NOW_MS, 'reconciliation_required': False,
        'positions': [], 'currencies': [], 'leverage': 1., 'position_mode': 'one_way',
        'protection_supported': True}


def _timed_reads(controller, raw, count):
    durations = []
    outcomes = []
    for _ in range(count):
        started = time.perf_counter()
        result = controller._account_metrics(raw, [], [], False)[0]
        durations.append(time.perf_counter() - started)
        outcomes.append((result['daily_pnl_verified'], result['external_cashflow_detected']))
    if any(outcome != (True, False) for outcome in outcomes):
        raise AssertionError('Synthetic all-zero trade history was not classified consistently')
    return durations


def measure(count, warm_reads):
    with tempfile.TemporaryDirectory(prefix='prism-cashflow-profile-') as temp_dir:
        store = Store(Path(temp_dir) / 'profile.sqlite3', clock=lambda: NOW_MS / 1000)
        try:
            store.update_config({'bybit': {'mode': 'paper', 'enabled': True,
                'mainnet_enabled': False, 'account_id': ACCOUNT_ID}})
            adapter = BybitAdapter(store, store.config(), transport=NoNetworkTransport(),
                credential_loader=lambda _mode: (_ for _ in ()).throw(AssertionError('No credentials')),
                clock=lambda: NOW_MS / 1000)
            _insert_rows(store, count)
            raw = _raw_account()

            original_reader = adapter.iter_transactions
            scan_counts = {'legacy': 0, 'revisioned': 0}

            def counted_reader(label):
                def read(**kwargs):
                    scan_counts[label] += 1
                    return original_reader(**kwargs)
                return read

            # Legacy injected adapter has a full, uncached iterator but no
            # revision API, matching the compatibility path.
            class LegacyAdapter:
                mode = 'paper'
                def iter_transactions(self, **kwargs):
                    scan_counts['legacy'] += 1
                    return original_reader(**kwargs)

            legacy_controller = _controller(store, adapter, lambda: NOW_MS / 1000)
            legacy_controller.adapter = LegacyAdapter()
            legacy_times = _timed_reads(legacy_controller, raw, warm_reads)

            adapter.iter_transactions = counted_reader('revisioned')
            revision_controller = _controller(store, adapter, lambda: NOW_MS / 1000)
            cold = _timed_reads(revision_controller, raw, 1)[0]
            warm = _timed_reads(revision_controller, raw, warm_reads)
            if scan_counts != {'legacy': warm_reads, 'revisioned': 1}:
                raise AssertionError(f'Unexpected full-scan counts: {scan_counts}')
            return {
                'ledger_rows': count,
                'warm_reads': warm_reads,
                'legacy_uncached': {
                    'full_scans': scan_counts['legacy'],
                    'total_seconds': sum(legacy_times),
                    'mean_seconds': sum(legacy_times) / len(legacy_times),
                },
                'revisioned_cache': {
                    'full_scans': scan_counts['revisioned'],
                    'cold_seconds': cold,
                    'warm_total_seconds': sum(warm),
                    'warm_mean_seconds': sum(warm) / len(warm),
                },
            }
        finally:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', nargs='+', type=int, default=[0, 10_000, 50_000])
    parser.add_argument('--warm-reads', type=int, default=10)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.warm_reads < 1 or any(count < 0 for count in args.rows):
        parser.error('rows must be nonnegative and warm-reads must be positive')
    result = {
        'schema_version': 1,
        'fixture': 'Temporary SQLite journal; synthetic same-day TRADE rows with zero cash flow.',
        'qualification': 'Not evaluated; this measures only transaction history classification.',
        'external_io': False,
        'method': 'Compare repeated uncached complete iterator scans with one cold revision-keyed scan plus unchanged-revision reads.',
        'results': [measure(count, args.warm_reads) for count in args.rows],
    }
    encoded = json.dumps(result, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__':
    main()
