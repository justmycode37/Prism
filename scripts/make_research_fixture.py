#!/usr/bin/env python3
"""Write deterministic SYNTHETIC daily histories for offline engineering checks.

These prices, sessions and events are invented fixtures, not investment evidence.
No network, credentials or service are used.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import random
from zoneinfo import ZoneInfo


def make_fixture():
    zone = ZoneInfo('America/New_York')
    markets, evidence = [], []
    for offset, symbol in enumerate(('FIXTURE_A', 'FIXTURE_B', 'FIXTURE_C', 'SPY', 'SMH')):
        randomizer = random.Random(1700 + offset)
        day, price, bars, features = datetime(2024, 1, 2, tzinfo=zone), 100., [], []
        for index in range(480):
            while day.weekday() >= 5:
                day += timedelta(days=1)
            start = round(day.timestamp() * 1000)
            opening_time = round(day.replace(hour=9, minute=30).timestamp() * 1000)
            close_time = round(day.replace(hour=16).timestamp() * 1000)
            opening = price * (1 + randomizer.uniform(-.003, .003))
            change = .0002 + .004 * math.sin(index / 13 + offset) + randomizer.gauss(0., .009)
            price = opening * (1 + change)
            bars.append({'start_ms': start, 'end_ms': start + 86400000,
                         'open_ms': opening_time, 'close_ms': close_time,
                         'open': opening, 'close': price, 'high': max(opening, price) * 1.003,
                         'low': min(opening, price) * .997, 'volume': 1000000 + index * 1000,
                         'closed': True, 'asset_class': 'equity'})
            if index % 40 == 0 and offset < 3:
                identifier = f'synthetic-{symbol}-{index}'
                available = day.replace(hour=16, minute=1).isoformat()
                evidence.append({'id': identifier, 'source_url': f'https://example.org/synthetic/{identifier}',
                    'source_kind': 'fixture', 'source_family': 'synthetic-engine-fixture',
                    'title': 'SYNTHETIC engineering event', 'content': 'Invented actual 120 versus expected 100; no real company or market claim.',
                    'symbols': [symbol], 'observed_at': available, 'published_at': available,
                    'first_available_at': available, 'primary_source': False,
                    'event': {'symbol': symbol, 'actual': 120., 'expected': 100., 'unit': 'fixture units',
                              'metric': 'synthetic capacity', 'period': str(index),
                              'expectation_available_at': (day - timedelta(days=2)).isoformat(),
                              'event_at': day.replace(hour=16).isoformat()}})
                features.append({'id': identifier, 'event_identity': identifier, 'symbol': symbol,
                    'available_at_ms': close_time + 60000, 'session_ms': close_time,
                    'value': .2, 'evidence_ids': [identifier]})
            day += timedelta(days=1)
        market = {'symbol': symbol, 'security_id': 'synthetic-' + symbol,
                  'asset_class': 'equity', 'exchange_timezone': 'America/New_York', 'interval_ms': 86400000,
                  'history_adjustment': 'adjusted_close', 'raw_history_basis': 'unadjusted',
                  'candles': bars, 'raw_candles': deepcopy(bars), 'corporate_actions': [],
                  'event_features': features, 'synthetic_fixture': True}
        if offset >= 3:
            market['benchmark_kind'] = 'market' if symbol == 'SPY' else 'sector'
        markets.append(market)
    return {'id': 'synthetic-engine-v1', 'source': 'SYNTHETIC offline engineering fixture; invented prices and sessions',
            'as_of': '2026-01-01T00:00:00Z', 'synthetic_fixture': True, 'markets': markets, 'evidence': evidence,
            'universe': []}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    arguments = parser.parse_args()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(make_fixture(), separators=(',', ':'), allow_nan=False) + '\n')
    print(f'Synthetic engineering fixture written to {arguments.output}')
