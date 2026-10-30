#!/usr/bin/env python3
"""Measure local paper-transport costs with synthetic closed-order histories.

No installed service, model provider, credentials, venue or public transport is
constructed. Every scenario owns a temporary SQLite database.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from prism_runtime.paper_exchange import PaperExchange
from prism_runtime.store import Store


def measure(order_count, quote_count):
    with tempfile.TemporaryDirectory(prefix='prism-paper-profile-') as scratch:
        now = [1_793_361_600.]
        store = Store(Path(scratch) / 'fixture.sqlite3', clock=lambda: now[0])
        paper = PaperExchange(store, clock=lambda: now[0])
        state = paper._load()
        for index in range(order_count):
            link = f'closed-fixture-{index:08d}'
            state['orders'][link] = {'orderLinkId': link, 'orderId': link,
                'category': 'linear', 'symbol': 'SOLUSDT', 'side': 'Buy',
                'orderType': 'Limit', 'qty': '1', 'price': '100',
                'orderStatus': 'Cancelled', 'cumExecQty': '0', 'cumExecValue': '0',
                'avgPrice': '0', 'leavesQty': '1', 'createdTime': str(round(now[0] * 1000))}
        paper._save(state)
        del state
        tracemalloc.start()
        started = time.perf_counter()
        for _ in range(quote_count):
            now[0] += 1
            messages = paper.advance([{'category': 'linear', 'symbol': 'SOLUSDT', 'quote': {
                'bid': 99.9, 'ask': 100.1, 'bid_size': 10., 'ask_size': 10.,
                'timestamp_ms': round(now[0] * 1000)}}])
            if messages:
                raise AssertionError('Closed fixture orders must never fill')
        advance_seconds = time.perf_counter() - started
        started = time.perf_counter()
        for _ in range(quote_count):
            reply = paper.request('GET', 'https://offline.invalid/v5/market/tickers?category=linear&symbol=SOLUSDT')
            if reply['result']['list'][0]['symbol'] != 'SOLUSDT':
                raise AssertionError('Expected only the locally stored quote')
        ticker_seconds = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        store.close()
        return {'closed_orders': order_count, 'quotes': quote_count,
            'advance_seconds': round(advance_seconds, 6),
            'ticker_seconds': round(ticker_seconds, 6),
            'peak_traced_bytes': peak}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--orders', type=int, nargs='+', default=[0, 1000, 10000])
    parser.add_argument('--quotes', type=int, default=5)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 1 <= args.quotes <= 1000 or any(not 0 <= count <= 100000 for count in args.orders):
        parser.error('Use 1..1000 quotes and 0..100000 closed orders per scenario')
    report = {'schema_version': 1, 'offline_synthetic_fixture': True,
        'timing_scope': 'local matching and cached ticker lookup with tracemalloc enabled; excludes fixture setup',
        'scenarios': [measure(count, args.quotes) for count in args.orders]}
    content = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.write_text(content)
    print(content, end='')


if __name__ == '__main__':
    main()
