"""Operator-owned settings for the four-stage reverse cascade."""
from __future__ import annotations

import copy
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

WORKSPACE = Path(__file__).resolve().parents[2]
ARCHITECTURE_VERSION = 1
LAYER_ORDER = ('collector', 'analyst', 'strategist', 'executor')
DEFAULT_CONFIG = {
    'model': 'gpt-6-luna',  # Existing UI configuration field: collector model.
    'max_concurrency': 8,
    'cycle_interval_seconds': 900,
    'market_refresh_seconds': 60,
    'daily_limit_usd': None,
    'max_calls_per_day': None,
    'max_output_tokens': 4096,
    'max_input_bytes': 48000,
    'max_evidence_per_cycle': 240,
    'max_handoffs_per_agent': 32,
    'layers': {
        'collector': {'count': 24, 'model': 'gpt-6-luna', 'reasoning_effort': 'low'},
        'analyst': {'count': 6, 'model': 'gpt-6.1-sol', 'reasoning_effort': 'medium'},
        'strategist': {'count': 2, 'model': 'gpt-6-astra', 'reasoning_effort': 'high'},
        'executor': {'count': 1, 'model': 'gpt-6-astra', 'reasoning_effort': 'max'},
    },
    # Operator-supplied conservative reservation rates, not a published price claim.
    'model_prices': {
        'gpt-6-luna': {'input': 1.0, 'output': 5.0},
        'gpt-6.1-sol': {'input': 10.0, 'output': 50.0},
        'gpt-6-astra': {'input': 30.0, 'output': 150.0},
    },
    'symbols': ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT', 'ADAUSDT'],
    'ingestion': {
        'max_pages': 16, 'max_response_bytes': 1000000,
        'request_timeout_seconds': 8, 'crawl_depth': 1, 'max_seconds': 30,
        'max_requests': 48, 'items_per_feed': 4, 'articles_per_feed': 1,
        'pages_per_site': 3, 'crawl_articles': True,
        'candle_limit': 1000, 'candle_interval': '60',
    },
    'risk': {
        'max_gross_fraction': 0.30, 'max_symbol_fraction': 0.10,
        'allocation_fraction': 0.10, 'fee_bps': 6, 'slippage_bps': 3,
        'max_quote_age_seconds': 120, 'stop_loss_fraction': 0.05,
        'max_drawdown_fraction': 0.10, 'position_ttl_seconds': 86400,
        'kill_switch': False,
    },
}


def state_directory() -> Path:
    return Path(os.environ.get('PRISM_STATE_DIR', str(WORKSPACE / '.prism-runtime'))).expanduser().resolve()


def token_file() -> Path:
    return Path(os.environ.get('PRISM_CONTROL_TOKEN_FILE', str(state_directory() / 'control.token'))).expanduser().resolve()


def default_config() -> dict:
    return copy.deepcopy(DEFAULT_CONFIG)


def validate_config_update(changes: dict) -> dict:
    if not isinstance(changes, dict) or not changes:
        raise ValueError('Provide configuration values.')
    result = {}
    integer_ranges = {'max_concurrency': (1, 32), 'cycle_interval_seconds': (60, 86400),
                      'market_refresh_seconds': (15, 900), 'max_calls_per_day': (1, 100000),
                      'max_output_tokens': (1024, 32768), 'max_input_bytes': (4000, 200000),
                      'max_evidence_per_cycle': (24, 2000), 'max_handoffs_per_agent': (4, 128)}
    for key, value in changes.items():
        if key in {'daily_limit_usd', 'max_calls_per_day'} and value is None:
            result[key] = None
        elif key == 'daily_limit_usd':
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise ValueError('The daily ceiling must be positive or null.')
            result[key] = float(value)
        elif key in integer_ranges:
            low, high = integer_ranges[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f'Invalid {key}.')
            result[key] = value
        elif key == 'model' and value == DEFAULT_CONFIG['model']:
            result[key] = value
        elif key == 'layers':
            if not isinstance(value, dict) or set(value) != set(LAYER_ORDER):
                raise ValueError('Configure all four cascade layers.')
            counts = []
            for layer in LAYER_ORDER:
                row = value[layer]
                if not isinstance(row, dict) or set(row) != {'count', 'model', 'reasoning_effort'}:
                    raise ValueError('Invalid layer settings.')
                if isinstance(row['count'], bool) or not isinstance(row['count'], int) or not 1 <= row['count'] <= 96:
                    raise ValueError('Invalid layer count.')
                if row['model'] != DEFAULT_CONFIG['layers'][layer]['model'] or row['reasoning_effort'] != DEFAULT_CONFIG['layers'][layer]['reasoning_effort']:
                    raise ValueError('Cascade model and reasoning progression is fixed by layer.')
                counts.append(row['count'])
            if counts[-1] != 1 or any(a <= b for a, b in zip(counts, counts[1:])):
                raise ValueError('The cascade must narrow at every layer to one executor.')
            result[key] = copy.deepcopy(value)
        elif key == 'sources':
            if not isinstance(value, list) or len(value) > 16:
                raise ValueError('Configure at most 16 public sources.')
            for source in value:
                if not isinstance(source, (str, dict)):
                    raise ValueError('A source must be a URL or source object.')
                if isinstance(source, dict):
                    if not set(source) <= {'url', 'kind', 'name', 'keywords'} or source.get('kind', 'rss') not in {'rss', 'atom', 'article'}:
                        raise ValueError('Invalid source definition.')
                    if not isinstance(source.get('name', ''), str) or len(source.get('name', '')) > 200:
                        raise ValueError('Invalid source name.')
                    words = source.get('keywords', [])
                    if not isinstance(words, list) or len(words) > 30 or any(not isinstance(word, str) or len(word) > 80 for word in words):
                        raise ValueError('Invalid source keywords.')
                url = source if isinstance(source, str) else source.get('url')
                if not isinstance(url, str) or len(url) > 2048:
                    raise ValueError('Invalid source URL.')
                try:
                    parts = urlsplit(url)
                    valid = parts.scheme == 'https' and parts.hostname and not parts.username and not parts.password and parts.port in {None, 443}
                except ValueError:
                    valid = False
                if not valid:
                    raise ValueError('Sources must use public HTTPS URLs.')
            result[key] = copy.deepcopy(value)
        elif key == 'ingestion':
            ranges = {'workers': (1, 8), 'max_pages': (1, 40), 'max_response_bytes': (1024, 2000000),
                      'request_timeout_seconds': (1, 20), 'crawl_depth': (0, 2), 'max_seconds': (5, 90),
                      'max_requests': (4, 96), 'items_per_feed': (1, 12), 'articles_per_feed': (0, 4),
                      'pages_per_site': (0, 8), 'candle_limit': (300, 1000), 'max_evidence': (1, 96),
                      'max_content_chars': (256, 12000), 'host_delay_seconds': (.25, 10)}
            if not isinstance(value, dict) or not value:
                raise ValueError('Invalid ingestion settings.')
            for name, number in value.items():
                if name == 'crawl_articles':
                    if not isinstance(number, bool):
                        raise ValueError('crawl_articles must be boolean.')
                elif name == 'candle_interval':
                    if number not in {'1', '3', '5', '15', '30', '60', '120', '240', '360', '720', 'D'}:
                        raise ValueError('Invalid candle interval.')
                elif name not in ranges or isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number) or not ranges[name][0] <= number <= ranges[name][1]:
                    raise ValueError('Invalid ingestion limit.')
                elif name not in {'host_delay_seconds', 'request_timeout_seconds', 'max_seconds'} and not isinstance(number, int):
                    raise ValueError('This ingestion limit requires an integer.')
            result[key] = dict(value)
        elif key == 'risk':
            if not isinstance(value, dict) or not value or not set(value) <= set(DEFAULT_CONFIG['risk']):
                raise ValueError('Invalid risk settings.')
            for name, number in value.items():
                if name == 'kill_switch':
                    if not isinstance(number, bool):
                        raise ValueError('kill_switch must be boolean.')
                elif isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number) or number <= 0:
                    raise ValueError('Risk limits must be finite and positive.')
                elif 'fraction' in name and number > 1:
                    raise ValueError('Risk fractions cannot exceed one.')
                elif name.endswith('_bps') and number > 100:
                    raise ValueError('Cost assumptions cannot exceed 100 basis points.')
            result[key] = dict(value)
        else:
            raise ValueError(f'Unsupported configuration field: {key[:60]}')
    return result
