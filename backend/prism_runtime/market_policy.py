"""Explicit v2 market requirements. A legacy daily rule never changes frequency implicitly."""
from __future__ import annotations
import copy
import math


INTERVAL_MS = {str(n): n * 60000 for n in (1, 3, 5, 15, 30, 60, 120, 240, 360, 720)} | {'D': 86400000, 'W': 604800000}
ALIASES = {'1m': '1', '3m': '3', '5m': '5', '15m': '15', '30m': '30', '1h': '60', '2h': '120', '4h': '240', '1d': 'D', 'daily': 'D', '1w': 'W'}
ATTENTION_EVENTS = {'candle_closed', 'intrabar', 'signal', 'fill', 'order', 'position', 'risk', 'scheduled_event', 'decision_deadline', 'data_health'}


def timeframe(value):
    result = ALIASES.get(str(value), str(value))
    if result not in INTERVAL_MS:
        raise ValueError('Unsupported Bybit candle interval')
    return result


def measured_quote_source(quote, category, captured_ms):
    """Recognize measured WS inputs without relabelling the spot composite."""
    if not isinstance(quote, dict):
        return False
    if quote.get('source') == 'websocket':
        return True
    if quote.get('source') != 'orderbook+ticker' or category != 'spot':
        return False
    stamps = [quote.get('book_timestamp_ms'), quote.get('ticker_timestamp_ms')]
    return (type(captured_ms) is int
            and all(type(stamp) is int and 0 < stamp <= captured_ms for stamp in stamps)
            and quote.get('timestamp_ms') == min(stamps))


def validate_market_policy(value):
    required = {'version', 'venue', 'category', 'symbol', 'primary_timeframe', 'context_timeframes',
                'history_bars', 'indicators', 'signal_mode', 'max_data_age_seconds',
                'max_decision_latency_seconds', 'attention_events'}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {'intrabar'}:
        raise ValueError('Complete explicit market policy v2 is required')
    result = copy.deepcopy(value)
    if value['version'] != 2 or type(value['version']) is not int or value['venue'] != 'bybit' or value['category'] not in {'spot', 'linear'}:
        raise ValueError('Only explicit Bybit spot or linear v2 policies are supported')
    if not isinstance(value['symbol'], str) or not value['symbol'].isalnum() or value['symbol'] != value['symbol'].upper() or not 3 <= len(value['symbol']) <= 32:
        raise ValueError('An exact uppercase venue symbol is required; equity aliases are forbidden')
    result['primary_timeframe'] = timeframe(value['primary_timeframe'])
    if not isinstance(value['context_timeframes'], list) or len(value['context_timeframes']) > 5:
        raise ValueError('At most five context timeframes are allowed')
    result['context_timeframes'] = list(dict.fromkeys(timeframe(x) for x in value['context_timeframes']))
    if type(value['history_bars']) is not int or not 3 <= value['history_bars'] <= 5000:
        raise ValueError('History must be bounded to 3–5000 bars per series')
    if not isinstance(value['indicators'], list) or len(value['indicators']) > 12:
        raise ValueError('Indicators must be a bounded list')
    for indicator in value['indicators']:
        if not isinstance(indicator, dict) or set(indicator) != {'kind', 'period'} or indicator['kind'] not in {'sma', 'ema', 'rsi', 'atr', 'volatility'} or type(indicator['period']) is not int or not 2 <= indicator['period'] <= min(500, value['history_bars'] - 1):
            raise ValueError('Unsupported indicator or insufficient declared warm-up')
    for name in ('max_data_age_seconds', 'max_decision_latency_seconds'):
        number = value[name]
        if isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number) or not 1 <= number <= 86400:
            raise ValueError('Finite positive freshness and latency limits are required')
    if not isinstance(value['attention_events'], list) or not set(value['attention_events']) <= ATTENTION_EVENTS:
        raise ValueError('Unknown execution attention event')
    if value['signal_mode'] not in {'closed', 'intrabar'}:
        raise ValueError('Signals must declare closed or explicitly defined intrabar observations')
    if value['signal_mode'] == 'intrabar':
        intrabar = value.get('intrabar')
        if not isinstance(intrabar, dict) or set(intrabar) != {'kind', 'threshold_fraction', 'minimum_interval_seconds'} or intrabar['kind'] != 'return_since_open':
            raise ValueError('Intrabar requires a return-since-open trigger')
        threshold, interval = intrabar['threshold_fraction'], intrabar['minimum_interval_seconds']
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0 < threshold <= .2 or type(interval) is not int or not 5 <= interval <= 86400:
            raise ValueError('Invalid intrabar trigger threshold or coalescing interval')
    elif value.get('intrabar') is not None:
        raise ValueError('Closed-candle strategies cannot silently add intrabar observations')
    return result
