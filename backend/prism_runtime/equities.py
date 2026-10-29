"""Public Yahoo chart data for broad, unofficial equity research."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import math
from urllib.parse import quote as url_quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class EquityMarketData:
    def __init__(self, fetch_json, evidence_factory, settings):
        self.fetch_json = fetch_json
        self.evidence_factory = evidence_factory
        self.settings = settings or {}

    def collect(self, symbol, budget, agent):
        # --- Validate input ticker early ---
        if isinstance(symbol, bool) or not isinstance(symbol, str):
            raise ValueError('Ticker symbol must be a non-empty string')
        from .sector import normalize_symbol
        symbol = normalize_symbol(symbol)
        if symbol is None:
            raise ValueError('Invalid equity ticker symbol')

        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{url_quote(symbol, safe='')}?range=5y&interval=1d"
        data = self.fetch_json(url, budget)
        try:
            result = data['chart']['result'][0]
            meta = result['meta']
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError('Yahoo chart response has no result') from exc

        # --- Validate returned symbol matches ---
        returned_symbol = str(meta.get('symbol', '')).upper()
        if returned_symbol != symbol:
            raise ValueError(f'Yahoo symbol mismatch: requested {symbol}, got {returned_symbol or "unknown"}')

        # --- Reject non-equity, non-USD, and boolean/invalid metadata ---
        quote_type = meta.get('instrumentType', meta.get('quoteType'))
        if isinstance(quote_type, bool) or not isinstance(quote_type, str):
            raise ValueError('Yahoo instrument metadata is malformed')
        if quote_type.upper() != 'EQUITY':
            raise ValueError('Yahoo instrument is not an equity')

        currency = meta.get('currency')
        if isinstance(currency, bool) or not isinstance(currency, str):
            raise ValueError('Yahoo instrument metadata is malformed')
        if currency.upper() != 'USD':
            raise ValueError(f'Yahoo equity currency is not USD: {currency or "unknown"}')

        # --- Validate quote timestamp: must be a valid epoch, not future ---
        raw_market_time = meta.get('regularMarketTime')
        if raw_market_time is None:
            raise ValueError('Yahoo quote has no valid timestamp')
        actual_seconds = self._integer(raw_market_time)
        actual_ms = actual_seconds * 1000
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if actual_ms <= 0 or actual_ms > now_ms:
            raise ValueError('Yahoo quote timestamp is in the future')

        # --- Validate price: finite positive, not None/bool/zero/negative ---
        raw_price = meta.get('regularMarketPrice')
        if isinstance(raw_price, bool):
            raise ValueError('Yahoo regularMarketPrice is not a valid number')
        price = self._positive(raw_price)
        if price is None:
            raise ValueError('Yahoo regularMarketPrice must be a finite positive number')

        # --- Resolve exchange timezone ---
        timezone_name = str(meta.get('exchangeTimezoneName') or 'UTC')
        try:
            zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Yahoo exchange timezone is invalid')

        # --- Extract chart indicators ---
        try:
            timestamps = result.get('timestamp') or []
            indicator = result['indicators']['quote'][0]
            adj = result.get('indicators', {}).get('adjclose', [{}])[0].get('adjclose') or []
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError('Yahoo chart response has incomplete indicators') from exc

        today_local = datetime.now(zone).date()

        # --- Build adjusted candles ---
        candles = []
        # Use candle_limit from settings (config ingestion key), not max_candles
        candle_limit = self.settings.get('candle_limit', self.settings.get('max_candles', 1260))
        try:
            candle_limit = min(1260, max(1, int(candle_limit)))
        except (TypeError, ValueError):
            candle_limit = 1260

        for i, raw_ts in enumerate(timestamps):
            try:
                stamp = int(raw_ts)
                local_dt = datetime.fromtimestamp(stamp, timezone.utc).astimezone(zone)
                local_date = local_dt.date()

                # Exclude the current local date's bar if the session has not closed.
                # This uses actual session close metadata rather than a 24h nominal end.
                # Use only prior daily sessions. Nominal 24-hour candle bounds
                # stay consistent with statistical and paper signal timing.
                if local_date >= today_local:
                    continue

                # Reject rows with timestamps in the future
                if stamp * 1000 > now_ms:
                    continue

                start = datetime.combine(local_date, time.min, zone).astimezone(timezone.utc)
                start_ms = int(start.timestamp() * 1000)
                end_ms = int((start + timedelta(days=1)).timestamp() * 1000)

                quote = {key: (values[i] if i < len(values) else None) for key, values in indicator.items()}
                raw_close = self._positive(quote.get('close'))
                adjusted_close = self._positive(adj[i] if i < len(adj) else None)
                if adj and (raw_close is None or adjusted_close is None):
                    continue
                factor = adjusted_close / raw_close if raw_close and adjusted_close else 1.0

                vals = {key: self._positive(quote.get(key)) for key in ('open', 'high', 'low', 'close')}
                volume = self._number(quote.get('volume'))
                if any(value is None for value in vals.values()) or volume is None or volume < 0:
                    continue

                o, h, l, c = (vals[k] * factor for k in ('open', 'high', 'low', 'close'))
                if h < max(o, c) or l > min(o, c) or h < l:
                    continue

                candles.append({'timestamp': start.isoformat().replace('+00:00', 'Z'), 'start_ms': start_ms,
                                'end_ms': end_ms, 'closed': True, 'open': o, 'high': h,
                                'low': l, 'close': c, 'volume': volume,
                                'asset_class': 'equity'})
            except (TypeError, ValueError, OverflowError, OSError):
                continue
        candles = candles[-candle_limit:]

        # --- Compute daily change from correct previous session ---
        # previousClose is the *previous trading session* close, suitable for daily change.
        # chartPreviousClose is the close at the start of the 5-year chart range and
        # must NOT be used as yesterday's close.
        prev_close = self._positive(meta.get('previousClose'))
        if prev_close is not None:
            change = price / prev_close - 1
            change_caveat = ('Calculated from previous session close (Yahoo previousClose); '
                             'delayed or adjusted data may apply.')
        else:
            reported_change = self._number(meta.get('regularMarketChangePercent'))
            change = reported_change / 100 if reported_change is not None else None
            change_caveat = ('Reported regular-session percentage change; delayed data may apply.'
                             if reported_change is not None else
                             'Previous session change unavailable; the five-year range start is not a daily baseline.')

        # --- Build quote timestamp from actual quote time, not fetch wallclock ---
        quote_asof = datetime.fromtimestamp(actual_ms / 1000, timezone.utc).isoformat().replace('+00:00', 'Z')

        market = {'symbol': symbol, 'asset_class': 'equity', 'currency': 'USD',
                  'exchange': meta.get('exchangeName') or meta.get('fullExchangeName') or '',
                  'exchange_timezone': timezone_name, 'price': price, 'as_of_ms': actual_ms,
                  'timestamp': quote_asof, 'updated_at': quote_asof, 'change_24h': change,
                  'change_24h_caveat': change_caveat,
                  'funding_rate': None, 'interval_ms': 86_400_000, 'candles': candles}
        market['history_adjustment'] = 'adjusted_close' if adj else 'unverified'

        caveat = ('Unofficial Yahoo Finance chart data. Daily OHLC values are adjusted by adjusted-close/close, '
                  'so historical prices can reflect dividends and splits; volume and live quote remain raw. '
                  'Only prior daily sessions are included; the current local day is excluded conservatively. Coverage, corporate actions and timestamps may be revised.')

        # --- Evidence IDs: quote ID includes content-sensitive hash ---
        quote_content_dict = {k: v for k, v in market.items() if k != 'candles'}
        quote_content = json.dumps(quote_content_dict, sort_keys=True)
        quote_ev = self.evidence_factory(url, 'market', f'{symbol} public equity quote',
                                         quote_content, agent, published=quote_asof)
        # Ensure quote evidence observed_at preserves actual quote timestamp
        quote_ev['observed_at'] = quote_asof

        # --- Chart evidence: include hash of candle data and readable numeric context ---
        chart_hash = hashlib.sha256(json.dumps(candles, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

        # Build readable numeric context for the chart
        chart_numeric_parts = [f'{len(candles)} daily candles; interval 86400000 ms']
        if candles:
            first_ts = candles[0].get('timestamp', '')
            last_ts = candles[-1].get('timestamp', '')
            first_close = candles[0].get('close')
            last_close = candles[-1].get('close')
            chart_numeric_parts.append(f'range {first_ts} to {last_ts}')
            if first_close is not None and last_close is not None:
                chart_numeric_parts.append(f'close {first_close:.4f} to {last_close:.4f}')
        chart_numeric_parts.append(f'SHA256: {chart_hash}')
        chart_numeric_parts.append(caveat)
        chart_content = '; '.join(chart_numeric_parts)

        chart_ev = self.evidence_factory(url, 'chart', f'{symbol} adjusted daily chart',
                                         chart_content, agent, published='')
        chart_ev['observed_at'] = quote_asof

        quote_ev['numeric'] = {'price': price, 'change_24h': change}
        chart_ev['numeric'] = {'closed_candles': len(candles), 'interval_ms': 86_400_000,
                               'chart_hash': chart_hash}

        # Reject out-of-order or duplicate observations instead of silently
        # sorting a malformed history into apparent validity.
        if any(left['start_ms'] >= right['start_ms'] for left, right in zip(candles, candles[1:])):
            raise ValueError('Equity history has duplicate or unordered sessions')

        return {'markets': [market], 'evidence': [quote_ev, chart_ev]}

    @staticmethod
    def _number(value):
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _positive(cls, value):
        number = cls._number(value)
        return number if number is not None and number > 0 else None

    @staticmethod
    def _integer(value):
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError('Yahoo quote has no valid timestamp')
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('Yahoo quote has no valid timestamp') from exc
