"""Causal, fixed-rule statistical screening for the reverse cascade.

Models propose hypotheses and bounded parameters, never executable source. Signals
observe closed bars and trade at the following open. A chronological holdout is
scored once with frozen parameters, fees and slippage. Statistical screening is
conditional on approximately independent, stationary block signs; it is not a
probability that a strategy will make money. All accepted strategies remain paper.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import hashlib
import json
import math
import statistics
import time

FEE_BPS = 6.0
SLIPPAGE_BPS = 3.0
MIN_CANDLES = 300
MIN_TRADES = 8
MIN_BLOCKS = 12
ALPHA = .05
KINDS = {"momentum", "mean_reversion", "breakout", "event_surprise"}


def _number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{label} must be {'positive and ' if positive else ''}finite")
    return result


def timestamp_ms(value):
    """Accept epoch milliseconds/seconds, datetime or ISO time without guessing 0."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Timestamp requires a timezone")
        return round(value.timestamp() * 1000)
    if isinstance(value, str):
        try:
            return timestamp_ms(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except (ValueError, OverflowError) as exc:
            raise ValueError("Invalid timestamp") from exc
    number = _number(value, "timestamp", positive=True)
    return round(number * 1000 if number < 100_000_000_000 else number)


def canonical_spec(proposal):
    if not isinstance(proposal, dict):
        raise ValueError("Strategy proposal must be an object")
    symbol = proposal.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 32:
        raise ValueError("A market symbol is required")
    kind = proposal.get("kind", proposal.get("family"))
    if kind not in KINDS:
        raise ValueError("Supported families are momentum, mean_reversion, breakout, event_surprise")
    if proposal.get("direction", "long") != "long":
        raise ValueError("Only paper spot long strategies are supported")
    parameters = proposal.get("parameters", {})
    if not isinstance(parameters, dict) or set(parameters) - {"lookback", "threshold"}:
        raise ValueError("Only lookback and threshold parameters are supported")
    lookback = _number(parameters.get("lookback", 20), "lookback")
    if not lookback.is_integer() or not 3 <= lookback <= 100:
        raise ValueError("Lookback must be an integer from 3 to 100")
    threshold = _number(parameters.get("threshold", 1.0 if kind == "mean_reversion" else .002 if kind == "momentum" else 0), "threshold")
    if not (0.1 <= threshold <= 4 if kind == "mean_reversion" else 0 <= threshold <= 10 if kind == 'event_surprise' else 0 <= threshold <= .2):
        raise ValueError("Threshold is outside the bounded strategy family")
    result = {"symbol": symbol.strip().upper(), "kind": kind,
              "parameters": {"lookback": int(lookback), "threshold": threshold}}
    if 'security_id' in proposal:
        security = proposal['security_id']
        if not isinstance(security, str) or not security.strip() or len(security) > 160:
            raise ValueError('Invalid stable security identifier')
        result['security_id'] = security
    return result


def spec_hash(spec):
    encoded = json.dumps(canonical_spec(spec), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _market(symbol, markets):
    found = [market for market in markets if isinstance(market, dict) and str(market.get("symbol", "")).upper() == symbol]
    if len(found) != 1:
        raise ValueError("Exactly one market history is required per symbol")
    return found[0]


def closed_candles(market, *, now_ms=None):
    """Validate provenance timing and OHLC shape; reject rather than sort bad data."""
    bars = market.get("candles")
    if not isinstance(bars, list):
        raise ValueError("Market is missing historical candles")
    now_ms = round(time.time() * 1000) if now_ms is None else now_ms
    result = []
    previous_end = None
    interval = None
    equity_daily = (str(market.get("asset_class", "")).lower() == "equity"
                    and market.get("interval_ms") == 86_400_000)
    if str(market.get('asset_class', '')).lower() == 'equity' and not equity_daily:
        raise ValueError('Equity research requires explicit daily candle intervals')
    if equity_daily and market.get('history_adjustment') == 'unverified':
        raise ValueError('Equity historical corporate-action adjustment is unverified')
    if equity_daily:
        try:
            exchange_zone = ZoneInfo(market.get("exchange_timezone", "America/New_York"))
        except (ZoneInfoNotFoundError, TypeError) as exc:
            raise ValueError("Invalid exchange timezone") from exc
    for row in bars:
        if not isinstance(row, dict):
            raise ValueError("Candle must be an object")
        if row.get("closed") is False:
            continue
        start = timestamp_ms(row.get("start_ms", row.get("timestamp_ms", row.get("timestamp"))))
        end = timestamp_ms(row.get("end_ms", row.get("close_time", start)))
        if end <= start:
            raise ValueError("Candle requires an explicit end after its start")
        if end > now_ms:
            # A present, incomplete exchange bar is not historical evidence.
            continue
        width = end - start
        if equity_daily:
            if width != 86_400_000:
                raise ValueError("Equity daily candles must have 24-hour widths")
            local_start = datetime.fromtimestamp(start / 1000, timezone.utc).astimezone(exchange_zone)
            if (local_start.hour, local_start.minute, local_start.second, local_start.microsecond) != (0, 0, 0, 0):
                raise ValueError("Equity daily candles must start at exchange-local midnight")
            if result and local_start.date() <= datetime.fromtimestamp(result[-1]["start_ms"] / 1000, timezone.utc).astimezone(exchange_zone).date():
                raise ValueError("Equity candles must have unique ordered local dates")
            if result:
                prior_local = datetime.fromtimestamp(result[-1]["start_ms"] / 1000, timezone.utc).astimezone(exchange_zone)
                expected = prior_local.replace(tzinfo=None) + timedelta(days=1)
                local_gap = (local_start.replace(tzinfo=None) - expected).days
                if local_gap < 0 or local_gap > 7:
                    raise ValueError("Equity session gap exceeds seven days or is unordered")
                overlap = previous_end - start
                offset_change = (local_start.utcoffset() - prior_local.utcoffset()).total_seconds() * 1000
                if overlap > 0 and not (local_gap == 0 and overlap == offset_change and overlap <= 3_600_000):
                    raise ValueError('Equity session overlap is not explained by daylight saving time')
        elif interval is None:
            interval = width
        if not equity_daily and (width != interval or (previous_end is not None and start != previous_end)):
            raise ValueError("Candles must be chronological, unique, regular and gap free")
        values = {key: _number(row.get(key), key, positive=True) for key in ("open", "high", "low", "close")}
        volume = _number(row.get("volume", 0), "volume")
        if volume < 0 or values["high"] < max(values["open"], values["close"]) or values["low"] > min(values["open"], values["close"]) or values["low"] > values["high"]:
            raise ValueError("Invalid candle OHLC or volume")
        result.append({"start_ms": start, "end_ms": end, **values, "volume": volume,
                       "closed": True, "asset_class": "equity" if equity_daily else market.get("asset_class")})
        previous_end = end
    return result


def signal_target(spec, history, current_target=0):
    """Target from completed bars only; callers supply no current execution bar."""
    spec = canonical_spec(spec)
    lookback, threshold = spec["parameters"]["lookback"], spec["parameters"]["threshold"]
    if len(history) < lookback + 1:
        return 0
    last = history[-1]["close"]
    if spec['kind'] == 'event_surprise':
        cutoff = history[-1]['end_ms']
        first = history[-lookback]['start_ms']
        latest = {}
        for feature in history[-1].get('event_features', []):
            if (feature.get('symbol') != spec['symbol'] or not first <= feature.get('session_ms', 0) <= cutoff
                or feature.get('available_at_ms', cutoff+1) > cutoff or not feature.get('evidence_ids')):
                continue
            key = feature.get('event_identity', feature.get('id'))
            if key not in latest or feature['available_at_ms'] > latest[key]['available_at_ms']:
                latest[key] = feature
        return int(any(_number(feature.get('value'), 'event surprise') > threshold for feature in latest.values()))
    if spec["kind"] == "momentum":
        return int(last / history[-lookback - 1]["close"] - 1 > threshold)
    if spec["kind"] == "breakout":
        prior = history[-lookback - 1:-1]
        if current_target:
            return int(last >= statistics.fmean(row["close"] for row in prior))
        return int(last > max(row["high"] for row in prior) * (1 + threshold))
    window = [row["close"] for row in history[-lookback:]]
    deviation = statistics.pstdev(window)
    if deviation <= 1e-12:
        return 0
    z_score = (last - statistics.fmean(window)) / deviation
    return int(z_score < 0 if current_target else z_score < -threshold)


def _run_segment(spec, bars, start, end, *, fee_bps=FEE_BPS, slippage_bps=SLIPPAGE_BPS):
    cash, quantity, peak, max_drawdown, costs = 1.0, 0.0, 1.0, 0.0, 0.0
    returns, curve, trade_returns = [], [], []
    fee, slippage = fee_bps / 10_000, slippage_bps / 10_000
    entry_cost = 0.0
    previous_equity = 1.0
    entries = 0
    for index in range(start, end):
        bar = bars[index]
        target = signal_target(spec, bars[:index], int(quantity > 0))
        if target and not quantity:
            fill = bar["open"] * (1 + slippage)
            notional = cash / (1 + fee)
            costs += notional * fee + (notional / fill) * (fill - bar["open"])
            quantity, entry_cost, cash = notional / fill, cash, 0.0
            entries += 1
        elif not target and quantity:
            fill = bar["open"] * (1 - slippage)
            proceeds = quantity * fill
            costs += proceeds * fee + quantity * (bar["open"] - fill)
            cash, quantity = proceeds * (1 - fee), 0.0
            trade_returns.append(cash / entry_cost - 1)
        equity = cash + quantity * bar["close"]
        if index == end - 1 and quantity:
            # Liquidate at the boundary, charging the full round trip.
            proceeds = quantity * bar["close"] * (1 - slippage)
            costs += proceeds * fee + quantity * bar["close"] * slippage
            cash, quantity = proceeds * (1 - fee), 0.0
            equity = cash
            trade_returns.append(cash / entry_cost - 1)
        if not math.isfinite(equity) or equity <= 0:
            raise ValueError("Historical prices produced invalid backtest equity")
        period_return = equity / previous_equity - 1
        if not math.isfinite(period_return):
            raise ValueError("Historical prices produced an invalid return")
        returns.append(period_return)
        curve.append(equity)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1 - equity / peak)
        previous_equity = equity
    deviation = statistics.stdev(returns) if len(returns) > 1 else 0
    annual_bars = (252 if bars[0].get("asset_class") == "equity" else
                   365.25 * 86_400_000 / (bars[0]["end_ms"] - bars[0]["start_ms"]))
    sharpe = statistics.fmean(returns) / deviation * math.sqrt(annual_bars) if deviation else 0.0
    if not math.isfinite(sharpe) or not math.isfinite(costs):
        raise ValueError("Historical prices produced nonfinite metrics")
    return {"net_return": previous_equity - 1, "sharpe": sharpe, "max_drawdown": max_drawdown,
            "trades": len(trade_returns), "entries": entries, "bars": end - start,
            "win_rate": sum(value > 0 for value in trade_returns) / len(trade_returns) if trade_returns else 0.0,
            "costs": costs, "returns": returns, "equity_curve": curve}


def _sign_test(returns, block_size):
    blocks = []
    for index in range(0, len(returns) - block_size + 1, block_size):
        total = math.prod(1 + value for value in returns[index:index + block_size]) - 1
        blocks.append(total)
    nonzero = [value for value in blocks if abs(value) > 1e-12]
    successes, count = sum(value > 0 for value in nonzero), len(nonzero)
    # lgamma avoids integer-to-float overflow on long histories.
    p_value = sum(math.exp(math.lgamma(count + 1) - math.lgamma(k + 1) - math.lgamma(count - k + 1) - count * math.log(2)) for k in range(successes, count + 1)) if count else 1.0
    correlation = 0.0
    if len(blocks) >= 3:
        left, right = blocks[:-1], blocks[1:]
        left_mean, right_mean = statistics.fmean(left), statistics.fmean(right)
        denominator = math.sqrt(sum((value - left_mean) ** 2 for value in left) * sum((value - right_mean) ** 2 for value in right))
        if denominator:
            correlation = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right)) / denominator
    return min(1.0, p_value), count, successes, correlation


def _summary(report):
    return {key: value for key, value in report.items() if key not in {"returns", "equity_curve"}}


def _fingerprint(bars):
    return hashlib.sha256(json.dumps(bars, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _daily_report(spec, market, markets, start, end, config):
    from .simulation import replay
    bars = closed_candles(market)
    replayed = replay(spec, [market], config, start_ms=bars[start]['start_ms'], end_ms=bars[end-1]['end_ms'],
        observation_delay_seconds=(config or {}).get('research', {}).get('observation_delay_seconds', 0))
    book, curve = replayed['portfolio'], replayed['curve']
    returns, equity = [], book['initial_equity']
    peak, drawdown = equity, 0.
    for point in curve:
        returns.append(point['equity'] / equity - 1)
        equity = point['equity']
        peak = max(peak, equity)
        drawdown = max(drawdown, 1-equity/peak)
    deviation = statistics.stdev(returns) if len(returns) > 1 else 0.
    exits = [row for row in book['trades'] if row['side'] == 'sell' and row.get('position_closed', True)]
    return {'net_return': equity/book['initial_equity']-1, 'returns': returns,
        'equity_curve': [row['equity']/book['initial_equity'] for row in curve],
        'sharpe': statistics.fmean(returns)/deviation*math.sqrt(252) if deviation else 0.,
        'max_drawdown': drawdown, 'trades': len(exits), 'entries': sum(row['side']=='buy' for row in book['trades']),
        'win_rate': sum(row['realized_pnl'] > 0 for row in exits)/len(exits) if exits else 0.,
        'costs': (book['fees_paid']+book.get('slippage_paid', 0))/book['initial_equity'], 'bars': len(curve)}


def _benchmarks(spec, market, markets, start, end, returns, config):
    from .simulation import execution_policy
    policy = execution_policy(config)
    bars = closed_candles(market)
    cost = 2*(policy['fee_bps']+policy['slippage_bps']+policy['spread_bps']/2)/10000
    stock = bars[end-1]['close']/bars[start]['open']-1-cost
    allocation = policy['allocation_fraction']
    result = {'stock_buy_hold_return': stock, 'exposure_matched_return': allocation*stock,
              'cash_return': 0., 'sector_return': None, 'market_return': None,
              'universe_scope': 'current_membership_only', 'missing_baselines': []}
    for kind in ('market', 'sector'):
        benchmark = next((m for m in markets if m.get('benchmark_kind') == kind), None)
        if benchmark:
            available = [b for b in closed_candles(benchmark) if bars[start]['start_ms'] <= b['start_ms'] and b['end_ms'] <= bars[end-1]['end_ms']]
            if len(available) >= end-start-2:
                result[kind+'_return'] = allocation*(available[-1]['close']/available[0]['open']-1-cost)
        if result[kind+'_return'] is None:
            result['missing_baselines'].append(kind)
    block_size = max(8, spec['parameters']['lookback'])
    blocks = [math.prod(1+r for r in returns[i:i+block_size])-1 for i in range(0,len(returns)-block_size+1,block_size)]
    if len(blocks) > 1:
        uncertainty = 1.96*statistics.stdev(blocks)/math.sqrt(len(blocks))
        result['block_mean_return'] = statistics.fmean(blocks)
        result['block_mean_interval'] = [statistics.fmean(blocks)-uncertainty, statistics.fmean(blocks)+uncertainty]
    if spec['kind'] == 'event_surprise' and market.get('asset_class') == 'equity':
        control = {'symbol': spec['symbol'], 'kind': 'momentum', 'parameters': {'lookback': spec['parameters']['lookback'], 'threshold': .002}}
        report = _daily_report(control, market, markets, start, end, config)
        result['event_ablation_technical_return'] = report['net_return']
    return result


def validate_edges(proposals: list[dict], markets: list[dict], *, trial_count: int = 1, config=None, alpha_budget=None) -> list[dict]:
    """Reject unmeasured claims; compare a frozen rule to cash after trading costs."""
    if not isinstance(proposals, list) or not isinstance(markets, list):
        raise ValueError("Proposals and markets must be lists")
    if isinstance(trial_count, bool) or not isinstance(trial_count, int) or trial_count < 1:
        raise ValueError("trial_count must include all attempted hypotheses")
    trials = max(trial_count, len(proposals), 1)
    from . import simulation
    policy = simulation.execution_policy(config)
    if alpha_budget is not None and (not isinstance(alpha_budget, (float, int)) or isinstance(alpha_budget, bool) or not 0 < alpha_budget <= ALPHA):
        raise ValueError('Invalid preallocated experiment alpha budget')
    result = []
    for proposal in proposals:
        proposal = proposal if isinstance(proposal, dict) else {}
        edge = {"id": "edge-" + hashlib.sha256(json.dumps(proposal, sort_keys=True, default=str).encode()).hexdigest()[:20],
                "symbol": proposal.get("symbol", ""), "kind": proposal.get("kind", proposal.get("family", "")),
                "parameters": deepcopy(proposal.get("parameters", {})), "evidence_ids": deepcopy(proposal.get("evidence_ids", [])),
                "hypothesis": str(proposal.get("hypothesis", ""))[:4000], "status": "rejected", "metrics": {}, "reason": "",
                'development_only': True, 'policy': policy, 'policy_hash': simulation.policy_hash(config)}
        try:
            spec = canonical_spec(proposal)
            if not isinstance(edge["evidence_ids"], list) or not edge["evidence_ids"] or not all(isinstance(value, str) and value for value in edge["evidence_ids"]) or not edge["hypothesis"].strip():
                raise ValueError("Hypothesis and source evidence lineage are required")
            market = _market(spec['symbol'], markets)
            bars = closed_candles(market)
            if len(bars) < MIN_CANDLES:
                raise ValueError(f"Insufficient closed history: need at least {MIN_CANDLES} candles")
            split = int(len(bars) * .65)
            warmup = spec["parameters"]["lookback"] + 1
            daily = market.get('asset_class') == 'equity'
            if daily:
                train = _daily_report(spec, market, markets, warmup, split, config)
                holdout = _daily_report(spec, market, markets, split, len(bars), config)
            else:
                train = _run_segment(spec, bars, warmup, split, fee_bps=policy['fee_bps'], slippage_bps=policy['slippage_bps'])
                holdout = _run_segment(spec, bars, split, len(bars), fee_bps=policy['fee_bps'], slippage_bps=policy['slippage_bps'])
            block_size = max(8, spec["parameters"]["lookback"])
            p_value, blocks, positive_blocks, correlation = _sign_test(holdout["returns"], block_size)
            adjusted = min(1.0, p_value * trials)
            metrics = {**_summary(holdout), "p_value": p_value, "adjusted_p_value": adjusted,
                       "trial_count": trials,
                       "block_size": block_size, "nonzero_blocks": blocks, "positive_blocks": positive_blocks,
                       "block_lag1_correlation": correlation, "train_net_return": train["net_return"], "train_trades": train["trades"]}
            validation = {"method": "development_chronological_block_screen", "benchmark": "stock_and_exposure_matched_buy_hold",
                          'development_only': True, 'policy_hash': simulation.policy_hash(config),
                          "spec_hash": spec_hash(spec), "data_fingerprint": _fingerprint(bars),
                          "train_start_ms": bars[warmup]["start_ms"], "train_end_ms": bars[split - 1]["end_ms"],
                          "holdout_start_ms": bars[split]["start_ms"], "holdout_end_ms": bars[-1]["end_ms"],
                          "sample_bars": len(bars), "alpha": ALPHA, "trial_count": trials,
                          "fee_bps": policy['fee_bps'], "slippage_bps": policy['slippage_bps'],
                          "assumptions": "Block signs are approximately independent and stationary; significance is a screening result, not a profit probability.",
                          "references": ["https://www.itl.nist.gov/div898/software/dataplot/refman1/auxillar/signtest.htm", "https://www.itl.nist.gov/div898/handbook/prc/section4/prc473.htm"]}
            reasons = []
            if train["trades"] < MIN_TRADES or holdout["trades"] < MIN_TRADES:
                reasons.append(f"At least {MIN_TRADES} completed trades required in both train and holdout")
            if blocks < MIN_BLOCKS:
                reasons.append(f"At least {MIN_BLOCKS} nonzero holdout blocks required")
            if abs(correlation) > .5:
                reasons.append("Serially dependent holdout blocks fail the independence screen")
            if train["net_return"] <= 0 or holdout["net_return"] <= 0:
                reasons.append("Net return must be positive after costs in both periods")
            if (p_value > alpha_budget if alpha_budget is not None else adjusted > ALPHA):
                reasons.append("Holdout evidence fails the multiple-testing corrected significance threshold")
            if holdout["max_drawdown"] > .2:
                reasons.append("Holdout drawdown exceeds the 20% research ceiling")
            benchmarks = _benchmarks(spec, market, markets, split, len(bars), holdout['returns'], config)
            if holdout['net_return'] <= benchmarks['exposure_matched_return']:
                reasons.append('Development result does not beat exposure-matched buy-and-hold')
            if len(holdout['returns']) // block_size < MIN_BLOCKS:
                reasons.append('Requested lookback is infeasible for available independent blocks; more history required')
            stress, folds, sensitivity = [], [], []
            if daily:
                for multiplier in (2, 3):
                    stressed = deepcopy(config or {})
                    stressed['risk'] = {**(config or {}).get('risk', {}), 'slippage_bps': policy['slippage_bps'] * multiplier,
                        'spread_bps': policy['spread_bps'] * multiplier, 'impact_bps': policy['impact_bps'] * multiplier}
                    report = _daily_report(spec, market, markets, split, len(bars), stressed)
                    stress.append({'cost_multiplier': multiplier, 'net_return': report['net_return'], 'max_drawdown': report['max_drawdown']})
                if any(row['net_return'] <= 0 for row in stress):
                    reasons.append('Development profitability fails adverse execution-cost scenarios')
                delayed = deepcopy(config or {})
                delayed['research'] = {**delayed.get('research', {}), 'observation_delay_seconds': 180}
                delayed_report = _daily_report(spec, market, markets, split, len(bars), delayed)
                stress.append({'scenario': 'missed_opening_observation', 'observation_delay_seconds': 180,
                               'net_return': delayed_report['net_return'], 'entries': delayed_report['entries']})
                for lookback in (spec['parameters']['lookback']-1, spec['parameters']['lookback']+1):
                    if 3 <= lookback <= 100:
                        neighbor = {**spec, 'parameters': {**spec['parameters'], 'lookback': lookback}}
                        report = _daily_report(neighbor, market, markets, split, len(bars), config)
                        sensitivity.append({'spec': neighbor, 'net_return': report['net_return'],
                                            'development_only': True, 'eligible_for_deployment': False})
                width = max(1, (len(bars) - warmup) // 3)
                for start in range(warmup, len(bars) - width + 1, width):
                    report = _daily_report(spec, market, markets, start, min(start + width, len(bars)), config)
                    folds.append({'start_ms': bars[start]['start_ms'], **_summary(report)})
            edge.update(spec, metrics=metrics, validation=validation, train_metrics=_summary(train), holdout_metrics=_summary(holdout),
                        benchmarks=benchmarks, stress_reports=stress, walk_forward=folds, parameter_sensitivity=sensitivity,
                        status="rejected" if reasons else "validated", reason="; ".join(reasons) if reasons else "Frozen rule passed development screening; prospective confirmation required")
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            edge["reason"] = str(exc)
        result.append(edge)
    return result


def assess_strategy(proposal: dict, edges: list[dict], markets: list[dict], config=None) -> dict:
    """The strategist may name and select evidence; it cannot retune tested rules."""
    now = datetime.now(timezone.utc).isoformat()
    proposal = proposal if isinstance(proposal, dict) else {}
    strategy = {"id": "strategy-" + hashlib.sha256(json.dumps(proposal, sort_keys=True, default=str).encode()).hexdigest()[:20],
                "name": str(proposal.get("name", "Cascade strategy"))[:120], "symbol": proposal.get("symbol", ""),
                "family": proposal.get("family", proposal.get("kind", "")), "status": "rejected", "net_return": 0.0,
                "sharpe": 0.0, "max_drawdown": 0.0, "trades": 0, "reason": "", "created_at": now,
                "spec": None, "edge_ids": [], "evidence_ids": [], "validation": {"passed": False}}
    try:
        identifiers = proposal.get("edge_ids")
        if not isinstance(identifiers, list) or not identifiers or not all(isinstance(value, str) for value in identifiers) or len(identifiers) != len(set(identifiers)):
            raise ValueError("Unique validated edge_ids are required")
        selected = [next((edge for edge in edges if edge.get("id") == identity), None) for identity in identifiers]
        if any(edge is None or edge.get("status") != "validated" for edge in selected):
            raise ValueError("Strategies require validated edges only")
        first = selected[0]
        frozen = canonical_spec(first)
        spec = canonical_spec({"symbol": proposal.get("symbol", frozen["symbol"]), "kind": proposal.get("kind", proposal.get("family", frozen["kind"])),
                               "parameters": proposal.get("parameters", frozen["parameters"]), "direction": proposal.get("direction", "long")})
        strategy["spec"] = spec
        if spec != frozen or any(canonical_spec(edge) != frozen for edge in selected):
            raise ValueError("Parameters and family must match one frozen validated rule; combinations require a new test")
        for edge in selected:
            if edge.get("validation", {}).get("spec_hash") != spec_hash(spec):
                raise ValueError("Edge specification failed its frozen parameter check")
            validation = edge.get("validation", {})
            if not validation.get("data_fingerprint") or validation.get("holdout_end_ms", 0) <= validation.get("holdout_start_ms", 0):
                raise ValueError("Edge lacks chronological holdout validation")
            metrics = edge.get("metrics", {})
            if metrics.get("adjusted_p_value", 1) > ALPHA or metrics.get("trades", 0) < MIN_TRADES or metrics.get("net_return", 0) <= 0:
                raise ValueError("Edge does not meet statistical eligibility")
        evidence = list(dict.fromkeys(value for edge in selected for value in edge.get("evidence_ids", [])))
        if not evidence:
            raise ValueError("Validated source evidence lineage is required")
        supplied = proposal.get("evidence_ids", evidence)
        if not isinstance(supplied, list) or any(value not in evidence for value in supplied):
            raise ValueError("Strategy cannot introduce unvalidated evidence lineage")
        bars = closed_candles(_market(spec["symbol"], markets))
        if not bars or bars[-1]["end_ms"] < first["validation"]["holdout_end_ms"]:
            raise ValueError("Market history no longer covers the validated holdout")
        for edge in selected:
            validation = edge["validation"]
            validated_bars = [bar for bar in bars if bar["end_ms"] <= validation["holdout_end_ms"]]
            if len(validated_bars) < validation.get("sample_bars", 0) or _fingerprint(validated_bars[-validation["sample_bars"]:]) != validation["data_fingerprint"]:
                raise ValueError("Historical data differs from the measured validation evidence")
        strategy.update(symbol=spec["symbol"], family=spec["kind"], spec=spec, status="paper", edge_ids=identifiers, evidence_ids=evidence,
                        reason="Validated frozen rule accepted for bounded paper execution", validation={"passed": True, "spec_hash": spec_hash(spec),
                            "holdout_end_ms": first["validation"]["holdout_end_ms"], "trial_count": first["validation"].get("trial_count", 1)},
                        **{key: first["metrics"][key] for key in ("net_return", "sharpe", "max_drawdown", "trades")})
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        strategy["reason"] = str(exc)
    return strategy
