"""Operator-owned settings for the four-stage reverse cascade."""
from __future__ import annotations

import copy
import math
import os
from pathlib import Path
from urllib.parse import urlsplit
from .operations_config import OPERATION_DEFAULTS, validate_operations_section
from .trading_modes import MODE_DEFAULTS, validate_mode_update

WORKSPACE = Path(__file__).resolve().parents[2]
ARCHITECTURE_VERSION = 1
LAYER_ORDER = ('collector', 'analyst', 'strategist', 'executor')
DEFAULT_CONFIG = {
    **OPERATION_DEFAULTS,
    'trading_mode': MODE_DEFAULTS,
    'model': 'gpt-6-luna',  # Existing UI configuration field: collector model.
    'max_concurrency': 8,
    'cycle_interval_seconds': 3600,  # Legacy UI field; never drives continuous research.
    'market_refresh_seconds': 60,
    'daily_limit_usd': None,
    'max_calls_per_day': None,
    'max_output_tokens': 8192,
    'max_input_bytes': 1000000,
    'max_evidence_per_cycle': 4000,
    'max_handoffs_per_agent': 64,
    'research': {
        'version': 1, 'mode': 'prospective_shadow',
        'active_collectors': 6, 'active_analysts': 2,
        'discovery_interval_seconds': 86400,
        'max_shadow_candidates': 64,
        'variants_per_hypothesis': 16, 'max_candidates_per_cycle': 256,
        'max_holding_sessions': 20,
        'min_forward_sessions': 60, 'min_forward_trades': 8,
        'forward_block_sessions': 5, 'min_forward_blocks': 12,
        'review_interval_sessions': 20,
        'max_forward_drawdown': .10,
        'min_excess_return': 0.0,
        'data_cost_usd_per_day': 0.0, 'infrastructure_cost_usd_per_day': 0.0,
        'use_model_selector': False, 'use_model_executor': False,
        'benchmark_symbols': ['SPY', 'SMH'],
    },
    'layers': {
        'collector': {'count': 24, 'model': 'gpt-6-luna', 'reasoning_effort': 'low'},
        'analyst': {'count': 6, 'model': 'gpt-6.1-sol', 'reasoning_effort': 'medium'},
        'strategist': {'count': 2, 'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'},
        'executor': {'count': 1, 'model': 'gpt-6.1-sol', 'reasoning_effort': 'max'},
    },
    # Operator-supplied conservative reservation rates, not a published price claim.
    'model_prices': {
        'gpt-6-luna': {'input': 1.0, 'output': 5.0},
        'gpt-6.1-sol': {'input': 10.0, 'output': 50.0},
        'gpt-6-astra': {'input': 30.0, 'output': 150.0},
    },
    'symbols': ['MRVL', 'NBIS', 'APLD', 'CRWV', 'IREN', 'MU', 'NVDA', 'AMD', 'AVGO', 'ANET', 'VRT', 'TSM', 'ARM', 'SMCI', 'ALAB', 'DELL', 'HPE', 'CLS'],
    'sector': {'id': 'ai_infrastructure', 'max_symbols': 80, 'discovery_enabled': True,
               'seed_symbols': ['MRVL', 'NBIS', 'APLD', 'CRWV', 'IREN', 'MU', 'NVDA', 'AMD', 'AVGO', 'ANET', 'VRT', 'TSM', 'ARM', 'SMCI', 'ALAB', 'DELL', 'HPE', 'CLS'],
               'keywords': ['AI infrastructure', 'AI data centers', 'semiconductor supply chain', 'accelerated computing', 'data center power', 'AI networking']},
    'ingestion': {
        'max_pages': 48, 'max_response_bytes': 1000000,
        'request_timeout_seconds': 8, 'crawl_depth': 2, 'max_seconds': 90,
        'max_requests': 160, 'items_per_feed': 12, 'articles_per_feed': 3,
        'pages_per_site': 6, 'crawl_articles': True,
        'candle_limit': 1260, 'candle_interval': 'D', 'max_content_chars': 24000,
        'max_evidence': 128, 'shared_evidence_limit': 256, 'workers': 4,
    },
    'risk': {
        'max_gross_fraction': 0.30, 'max_symbol_fraction': 0.10,
        'allocation_fraction': 0.10, 'fee_bps': 6, 'slippage_bps': 3,
        'spread_bps': 10, 'impact_bps': 10, 'max_participation_fraction': .01,
        'max_correlated_fraction': .10, 'correlation_ceiling': .80,
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
                      'max_output_tokens': (1024, 32768), 'max_input_bytes': (4000, 2000000),
                      'max_evidence_per_cycle': (24, 20000), 'max_handoffs_per_agent': (4, 256)}
    for key, value in changes.items():
        if key == 'trading_mode':
            result[key] = validate_mode_update(value)
        elif key in OPERATION_DEFAULTS:
            result[key] = validate_operations_section(key, value)
        elif key in {'daily_limit_usd', 'max_calls_per_day'} and value is None:
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
        elif key == 'symbols':
            if not isinstance(value, list) or not 1 <= len(value) <= 256 or any(not isinstance(x, str) or not x or len(x) > 20 for x in value) or len(set(value)) != len(value):
                raise ValueError('Symbols must be a deduplicated list of tickers.')
            result[key] = list(value)
        elif key == 'research':
            if not isinstance(value, dict) or not value or set(value) - set(DEFAULT_CONFIG['research']):
                raise ValueError('Invalid research protocol settings.')
            integers = {'active_collectors': (1, 24), 'active_analysts': (1, 6),
                        'variants_per_hypothesis': (1, 128), 'max_candidates_per_cycle': (1, 2000),
                        'discovery_interval_seconds': (3600, 604800), 'max_shadow_candidates': (1, 256),
                        'max_holding_sessions': (1, 100), 'min_forward_sessions': (60, 2520),
                        'min_forward_trades': (8, 1000), 'forward_block_sessions': (5, 100),
                        'min_forward_blocks': (12, 100), 'review_interval_sessions': (20, 252)}
            for name, number in value.items():
                if name in {'use_model_selector', 'use_model_executor'}:
                    if not isinstance(number, bool):
                        raise ValueError('Research model switches must be boolean.')
                elif name == 'benchmark_symbols':
                    if not isinstance(number, list) or len(number) != 2 or any(not isinstance(symbol, str) or not symbol.isalnum() or len(symbol) > 10 for symbol in number) or len(set(number)) != 2:
                        raise ValueError('Supply distinct market and sector benchmark symbols.')
                elif name in integers:
                    if isinstance(number, bool) or not isinstance(number, int) or not integers[name][0] <= number <= integers[name][1]:
                        raise ValueError('Invalid research protocol limit.')
                elif name in {'data_cost_usd_per_day', 'infrastructure_cost_usd_per_day', 'min_excess_return'}:
                    if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0:
                        raise ValueError('Research costs and effect thresholds must be finite and nonnegative.')
                elif name == 'max_forward_drawdown':
                    if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not 0 < number <= .10:
                        raise ValueError('Forward drawdown cannot exceed 10%.')
                elif number != DEFAULT_CONFIG['research'][name]:
                    raise ValueError('Unsupported research protocol version or mode.')
            result[key] = copy.deepcopy(value)
        elif key == 'sector':
            if not isinstance(value, dict) or not value or not set(value) <= {'id', 'max_symbols', 'discovery_enabled', 'seed_symbols', 'keywords'}:
                raise ValueError('Invalid sector settings.')
            if 'id' in value and value['id'] != 'ai_infrastructure': raise ValueError('Invalid sector id.')
            if 'max_symbols' in value and (isinstance(value['max_symbols'], bool) or not isinstance(value['max_symbols'], int) or not 12 <= value['max_symbols'] <= 256): raise ValueError('Invalid sector symbol limit.')
            if 'discovery_enabled' in value and not isinstance(value['discovery_enabled'], bool): raise ValueError('Invalid discovery flag.')
            if 'seed_symbols' in value and (not isinstance(value['seed_symbols'], list) or not 1 <= len(value['seed_symbols']) <= 256 or any(not isinstance(x, str) or not x or len(x) > 20 for x in value['seed_symbols']) or len(set(value['seed_symbols'])) != len(value['seed_symbols'])): raise ValueError('Invalid sector seed symbols.')
            if 'keywords' in value and (not isinstance(value['keywords'], list) or not 1 <= len(value['keywords']) <= 40 or any(not isinstance(x, str) or not x.strip() or len(x) > 100 for x in value['keywords'])): raise ValueError('Invalid sector keywords.')
            result[key] = copy.deepcopy(value)
        elif key == 'sources':
            if not isinstance(value, list) or len(value) > 128:
                raise ValueError('Configure at most 128 public sources.')
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
            ranges = {'workers': (1, 16), 'max_pages': (1, 96), 'max_response_bytes': (1024, 2000000),
                      'request_timeout_seconds': (1, 20), 'crawl_depth': (0, 2), 'max_seconds': (5, 90),
                      'max_requests': (4, 512), 'items_per_feed': (1, 32), 'articles_per_feed': (0, 8),
                      'pages_per_site': (0, 16), 'candle_limit': (300, 1260), 'max_evidence': (1, 256),
                      'shared_evidence_limit': (1, 1024), 'max_content_chars': (256, 48000), 'host_delay_seconds': (.25, 10)}
            if not isinstance(value, dict) or not value:
                raise ValueError('Invalid ingestion settings.')
            for name, number in value.items():
                if name == 'crawl_articles':
                    if not isinstance(number, bool):
                        raise ValueError('crawl_articles must be boolean.')
                elif name == 'candle_interval':
                    if number != 'D':
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
