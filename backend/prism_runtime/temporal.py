"""Point-in-time filters for research evidence and market data."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math


def _ms(value):
    """Numeric timestamps are epoch milliseconds; source adapters convert seconds."""
    if isinstance(value, bool) or value is None:
        raise ValueError("Invalid timestamp")
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Invalid timestamp") from exc
    elif isinstance(value, (int, float)) and math.isfinite(value):
        return round(value)
    else:
        raise ValueError("Invalid timestamp")
    if dt.tzinfo is None:
        raise ValueError("Timestamp requires timezone")
    return round(dt.timestamp() * 1000)


def evidence_as_of(items, cutoff_ms, *, historical=False):
    cutoff = _ms(cutoff_ms)
    result = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            first = item.get("first_observed_at", item.get("observed_at"))
            fields = [first, item.get("available_at", first)]
            if item.get('published_at') is not None:
                fields.append(item['published_at'])
            if item.get("revised_at") is not None:
                fields.append(item["revised_at"])
            if historical:
                fields.extend([item["published_at"], item["first_available_at"]])
            times = [_ms(v) for v in fields]
            if max(times) > cutoff:
                continue
            if historical and not item.get("first_available_at"):
                continue
        except (ValueError, KeyError, TypeError, OverflowError):
            continue
        row = deepcopy(item)
        row["available_at_ms"] = max(times)
        result.append(row)
    return result


def market_as_of(market, cutoff_ms):
    cutoff = _ms(cutoff_ms)
    if not isinstance(market, dict):
        return {}
    keys = ("symbol", "asset_class", "currency", "exchange", "exchange_timezone", "interval_ms", "history_adjustment",
            'benchmark_kind', 'raw_history_basis', 'security_id', 'instrument_type')
    out = {k: deepcopy(market[k]) for k in keys if k in market}
    for name in ("candles", "raw_candles"):
        allowed = ('timestamp','start_ms','end_ms','open_ms','close_ms','provider_timestamp_ms',
                   'available_at_ms','open_available_at_ms','close_available_at_ms',
                   'open','high','low','close','volume','closed','asset_class')
        out[name] = [{key: deepcopy(r[key]) for key in allowed if key in r} for r in market.get(name, [])
                     if isinstance(r, dict) and _row_end(r) is not None and _row_end(r) <= cutoff
                     and (not r.get('start_ms') or _ms(r['start_ms']) < _row_end(r))
                     and (not r.get('available_at_ms') or _ms(r['available_at_ms']) <= cutoff)]
    out["corporate_actions"] = [deepcopy(r) for r in market.get("corporate_actions", []) if _available(r) is not None and _available(r) <= cutoff]
    out["event_features"] = [deepcopy(r) for r in market.get("event_features", []) if _available(r) is not None and _available(r) <= cutoff]
    quote_time = next((market[k] for k in ('as_of_ms', "quote_timestamp", "price_as_of", "as_of") if k in market), None)
    try:
        if quote_time is not None and _ms(quote_time) <= cutoff:
            for k in ("price", "as_of", 'as_of_ms', 'timestamp', 'updated_at'):
                if k in market: out[k] = deepcopy(market[k])
    except ValueError:
        pass
    try:
        if market.get("session") is not None and _ms(market.get("session_known_at")) <= cutoff:
            out['session'] = {key: deepcopy(market['session'].get(key)) for key in ('open_ms','close_ms','open_price')}
            if out['session'].get('open_ms', cutoff+1) > cutoff:
                out['session']['open_price'] = None
    except ValueError:
        pass
    out['history_revision'] = hashlib.sha256(json.dumps(out['candles'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return out


def _available(row):
    try: return _ms(row.get("available_at", row.get("available_at_ms")))
    except (ValueError, TypeError): return None


def _row_end(row):
    try: return _ms(row.get("end_ms", row.get("end_time", row.get("timestamp"))))
    except (ValueError, TypeError): return None


def event_features(evidence, cutoff_ms):
    cutoff = _ms(cutoff_ms)
    features = []
    for row in evidence_as_of(evidence, cutoff):
        event = row.get("event")
        if not isinstance(event, dict): continue
        try:
            if any(not isinstance(event.get(key), str) or not event[key].strip() for key in ('metric', 'period', 'unit')):
                continue
            symbol = event.get('symbol') or row.get('symbol')
            if not symbol and len(row.get('symbols', [])) == 1:
                symbol = row['symbols'][0]
            if not isinstance(symbol, str) or not symbol:
                continue
            if event.get('expected_unit', event['unit']) != event['unit'] or event.get('expected_period', event['period']) != event['period']:
                continue
            if isinstance(event.get('actual'), bool) or isinstance(event.get('expected'), bool):
                continue
            actual, expected = float(event["actual"]), float(event["expected"])
            available = max(row['available_at_ms'], _ms(row['published_at']))
            expectation = _ms(event["expectation_available_at"])
            event_at = _ms(event["event_at"])
            publication = _ms(row['published_at'])
            if not all(map(math.isfinite, (actual, expected))) or expected == 0 or not expectation < event_at <= publication <= available <= cutoff: continue
            value = max(-10.0, min(10.0, (actual - expected) / abs(expected)))
            if not row.get('id'):
                continue
            logical = hashlib.sha256(json.dumps([symbol,event['metric'],event['period'],event_at]).encode()).hexdigest()[:24]
            features.append({"id": row.get("id"), "event_identity": logical, "symbol": symbol, "available_at_ms": available,
                             "session_ms": event_at, "value": value, "evidence_ids": [row.get("id")] if row.get("id") else []})
        except (KeyError, ValueError, TypeError, OverflowError): continue
    return features


def coverage_report(evidence, markets):
    items = list(evidence or [])
    markets = list(markets or [])
    gaps = []
    if not items or any(not e.get("published_at") for e in items if isinstance(e, dict)): gaps.append("missing original publications")
    if not items or any(not e.get("first_available_at") for e in items if isinstance(e, dict)): gaps.append("missing first availability")
    if not any(isinstance(e, dict) and e.get("primary_source") for e in items): gaps.append("missing primary financial sources")
    if not markets or any(not m.get('session') or not (m.get('candles') or m.get('history_end_ms')) or (not m.get('raw_candles') and not m.get('_series_id')) or 'corporate_actions' not in m for m in markets if isinstance(m, dict)): gaps.append("missing session/raw history/actions")
    if any(m.get('raw_history_basis') != 'unadjusted' or (not m.get('execution_metadata_complete') and any(not row.get('open_ms') or not row.get('close_ms') for row in m.get('raw_candles', []))) for m in markets):
        gaps.append('historical execution prices or observed session calendars are unverified')
    if not event_features(items, 9_999_999_999_999): gaps.append("missing actual/expectation features")
    if not any(isinstance(m, dict) and m.get("historical_universe") for m in markets): gaps.append("missing historical universe")
    return {"gaps": gaps, "survivorship_safe": False, "counts": {"evidence": len(items), "markets": len(markets), "event_features": len(event_features(items, 9_999_999_999_999))}}
