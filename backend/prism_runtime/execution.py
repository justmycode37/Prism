"""Deterministic, cash-collateralized paper execution for accepted cascade rules.

An executor model can activate, hold, or close a known strategy. It cannot submit
an order, choose a price, size a position, borrow, or trade a new instrument.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import math
import time

from .analytics import canonical_spec, closed_candles, signal_target, spec_hash, timestamp_ms

DEFAULT_LIMITS = {"max_gross_fraction": .30, "max_symbol_fraction": .10, "allocation_fraction": .10,
                  "fee_bps": 6.0, "slippage_bps": 3.0, "max_quote_age_seconds": 120.0,
                  "stop_loss_fraction": .05, "max_drawdown_fraction": .10, "position_ttl_seconds": 86400.0}


def _finite(value, label, *, positive=False):
    try:
        valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and (not positive or value > 0)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"Invalid {label}")
    return float(value)


def _iso(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat()


def initial_portfolio(capital=100000.0):
    capital = _finite(capital, "paper capital", positive=True)
    return {"mode": "paper", "currency": "USD", "equity": capital, "cash": capital,
            "initial_equity": capital, "peak_equity": capital, "realized_pnl": 0.0,
            "unrealized_pnl": 0.0, "fees_paid": 0.0, "gross_exposure": 0.0,
            "as_of": None, "as_of_ms": None, "pnl_history": [], "positions": [], "trades": [],
            "seen_signals": {}, "risk_halted": False, "valuation_complete": True, "last_execution_ms": None,
            "last_action": {"status": "ready", "reason": "Paper cash ledger initialized"}}


def _limits(config):
    if not isinstance(config, dict):
        raise ValueError("Paper execution config must be an object")
    supplied = {**config, **config.get("risk", {})}
    result = {}
    for key, default in DEFAULT_LIMITS.items():
        number = _finite(supplied.get(key, default), key, positive=True)
        if key.endswith("fraction") and number > 1:
            raise ValueError(f"{key} cannot exceed one")
        if key in {"fee_bps", "slippage_bps"} and number > 100:
            raise ValueError("Paper transaction costs cannot exceed 100 bps per side")
        result[key] = number
    # Hard ceilings remain deterministic even if config or an agent asks for more.
    result["max_gross_fraction"] = min(result["max_gross_fraction"], .30)
    result["max_symbol_fraction"] = min(result["max_symbol_fraction"], .10)
    result["allocation_fraction"] = min(result["allocation_fraction"], .10)
    result["max_quote_age_seconds"] = min(result["max_quote_age_seconds"], 120)
    result["kill_switch"] = config.get("kill_switch") is True or supplied.get("kill_switch") is True
    return result


def _quotes(markets, now_ms, limits):
    quotes, errors = {}, []
    seen = set()
    for market in markets:
        if not isinstance(market, dict):
            errors.append("Invalid market quote")
            continue
        symbol = str(market.get("symbol", "")).upper()
        if symbol in seen:
            quotes.pop(symbol, None)
            errors.append(f"{symbol}: duplicate market quotes")
            continue
        seen.add(symbol)
        try:
            raw_price = market.get("price", market.get("last_price", market.get("mark_price")))
            price = _finite(raw_price, "quote price", positive=True)
            quote_ms = timestamp_ms(market.get("as_of_ms", market.get("timestamp", market.get("updated_at"))))
            if quote_ms > now_ms or now_ms - quote_ms > limits["max_quote_age_seconds"] * 1000:
                raise ValueError("Quote is stale or future dated")
            quotes[symbol] = {"price": price, "timestamp_ms": quote_ms, "market": market}
        except (ValueError, TypeError, OverflowError) as exc:
            errors.append(f"{symbol}: {exc}")
    return quotes, errors


def _revalue(portfolio):
    gross = 0.0
    unrealized = 0.0
    for position in portfolio["positions"]:
        value = position["quantity"] * position["mark_price"]
        position["unrealized_pnl"] = value - position["cost_basis"]
        unrealized += position["unrealized_pnl"]
        gross += value
    portfolio["gross_exposure"] = gross
    portfolio["unrealized_pnl"] = unrealized
    portfolio["equity"] = portfolio["cash"] + gross
    portfolio["peak_equity"] = max(portfolio["peak_equity"], portfolio["equity"])
    if not math.isfinite(portfolio["equity"]) or portfolio["cash"] < -1e-8:
        raise ValueError("Paper ledger violated the cash accounting invariant")


def _close(portfolio, position, quote, limits, now_ms, reason):
    fill_price = quote["price"] * (1 - limits["slippage_bps"] / 10_000)
    proceeds = position["quantity"] * fill_price
    fee = proceeds * limits["fee_bps"] / 10_000
    pnl = proceeds - fee - position["cost_basis"]
    portfolio["cash"] += proceeds - fee
    portfolio["fees_paid"] += fee
    portfolio["realized_pnl"] += pnl
    portfolio["trades"].append({"id": f"{position['id']}:close", "strategy_id": position["strategy_id"],
        "symbol": position["symbol"], "side": "sell", "quantity": position["quantity"], "price": fill_price,
        "fee": fee, "timestamp": _iso(now_ms), "timestamp_ms": now_ms,
        "quote_timestamp_ms": quote["timestamp_ms"], "realized_pnl": pnl, "reason": reason, "mode": "paper"})
    portfolio["positions"].remove(position)
    portfolio["last_action"] = {"status": "filled", "reason": reason}
    _revalue(portfolio)


def _accepted(strategy):
    if not isinstance(strategy, dict) or strategy.get("status") != "paper":
        return False
    try:
        spec = canonical_spec(strategy.get("spec", {}))
        validation = strategy.get("validation", {})
        return (isinstance(strategy.get("id"), str) and bool(strategy["id"])
                and validation.get("passed") is True and validation.get("spec_hash") == spec_hash(spec)
                and bool(strategy.get("edge_ids")) and bool(strategy.get("evidence_ids")))
    except (ValueError, TypeError):
        return False


def execute(portfolio: dict, strategies: list[dict], decisions: list[dict], markets: list[dict], config: dict, *, now=None) -> dict:
    """Copy, mark, enforce risk, then execute eligible closed-bar targets once.

    Empty decisions still mark and apply risk/strategy exits. Opening always needs
    a fresh activate decision for a validated strategy and a fresh closed signal.
    """
    now_ms = round(time.time() * 1000) if now is None else timestamp_ms(now)
    if portfolio.get('policy_version') == 1 or any(row.get('asset_class') == 'equity' for row in markets if isinstance(row, dict)):
        from .simulation import advance
        return advance(portfolio, strategies, decisions, markets, config, now_ms)
    limits = _limits(config)
    if not isinstance(portfolio, dict) or not isinstance(strategies, list) or not isinstance(decisions, list) or not isinstance(markets, list):
        raise ValueError("Invalid paper execution inputs")
    result = deepcopy(portfolio)
    previous_execution = result.get("last_execution_ms") or result.get("as_of_ms")
    if previous_execution is not None and now_ms < previous_execution:
        raise ValueError("Paper execution cannot move backwards in time")
    for key in ("cash", "equity", "initial_equity", "peak_equity", "realized_pnl", "fees_paid"):
        _finite(result.get(key), key)
    if result["cash"] < 0 or result["initial_equity"] <= 0:
        raise ValueError("Invalid initial paper account")
    quotes, errors = _quotes(markets, now_ms, limits)
    result["last_action"] = {"status": "unchanged", "reason": "No eligible executor action"}
    strategy_by_id = {strategy["id"]: strategy for strategy in strategies if _accepted(strategy)}
    decisions_by_id = {}
    for decision in decisions:
        if not isinstance(decision, dict) or decision.get("strategy_id") not in strategy_by_id or decision.get("action") not in {"activate", "hold", "close"}:
            errors.append("Executor decision references an unaccepted strategy or action")
            continue
        identity = decision["strategy_id"]
        if identity in decisions_by_id:
            # Contradictory model instructions never generate orders.
            decisions_by_id[identity] = {"strategy_id": identity, "action": "hold", "reason": "Duplicate executor decisions"}
        else:
            decisions_by_id[identity] = decision

    stale_position = False
    for position in result["positions"]:
        for key in ("quantity", "mark_price", "entry_price", "cost_basis"):
            _finite(position.get(key), f"position {key}", positive=True)
        quote = quotes.get(position["symbol"])
        if quote and quote["timestamp_ms"] >= position.get("last_mark_ms", 0):
            position["mark_price"] = quote["price"]
            position["last_mark_ms"] = quote["timestamp_ms"]
        else:
            stale_position = True
            errors.append(f"{position['symbol']}: open position lacks a fresh monotonic mark")
    _revalue(result)
    drawdown = 1 - result["equity"] / result["peak_equity"] if result["peak_equity"] else 1
    if drawdown >= limits["max_drawdown_fraction"]:
        result["risk_halted"] = True
    forced_halt = result.get("risk_halted", False) or limits["kill_switch"]
    closed_ids = set()
    for position in list(result["positions"]):
        quote = quotes.get(position["symbol"])
        if not quote or quote["timestamp_ms"] < position.get("last_mark_ms", 0):
            continue
        strategy = strategy_by_id.get(position["strategy_id"])
        decision = decisions_by_id.get(position["strategy_id"], {})
        reason = None
        if forced_halt:
            reason = "Paper kill switch" if limits["kill_switch"] else "Portfolio drawdown limit"
        elif quote["price"] <= position["entry_price"] * (1 - limits["stop_loss_fraction"]):
            reason = "Deterministic paper stop loss"
        elif now_ms - position["opened_at_ms"] >= limits["position_ttl_seconds"] * 1000:
            reason = "Paper position time to live expired"
        elif decision.get("action") == "close":
            reason = "Executor selected strategy close"
        elif strategy is None:
            reason = "Strategy is no longer accepted"
        else:
            try:
                bars = closed_candles(quote["market"], now_ms=now_ms)
                if bars and bars[-1]["end_ms"] <= quote["timestamp_ms"] and not signal_target(strategy["spec"], bars, 1):
                    reason = "Frozen strategy emitted a flat target"
                    result["seen_signals"][position["strategy_id"]] = bars[-1]["end_ms"]
            except (ValueError, TypeError, KeyError):
                errors.append(f"{position['symbol']}: invalid candles; strategy exit deferred")
        if reason:
            closed_ids.add(position["strategy_id"])
            _close(result, position, quote, limits, now_ms, reason)

    for identity, decision in sorted(decisions_by_id.items()):
        if decision["action"] != "activate" or identity in closed_ids:
            continue
        strategy = strategy_by_id[identity]
        spec = canonical_spec(strategy["spec"])
        signal_identity = spec_hash(spec)
        symbol = spec["symbol"]
        quote = quotes.get(symbol)
        if not quote:
            errors.append(f"{symbol}: entry requires a fresh quote")
            continue
        if forced_halt or stale_position:
            errors.append("Paper entries halted by risk or missing marks")
            continue
        if any(position["symbol"] == symbol for position in result["positions"]):
            continue
        try:
            bars = closed_candles(quote["market"], now_ms=now_ms)
            if len(bars) < spec["parameters"]["lookback"] + 1:
                raise ValueError("Insufficient closed signal history")
            signal_ms = bars[-1]["end_ms"]
            interval_ms = bars[-1]["end_ms"] - bars[-1]["start_ms"]
            stale_signal = now_ms - signal_ms > interval_ms * 2
            if quote['market'].get('asset_class') == 'equity':
                zone = ZoneInfo(quote['market'].get('exchange_timezone', 'America/New_York'))
                observed_day = datetime.fromtimestamp(bars[-1]['start_ms'] / 1000, zone).date()
                current_day = datetime.fromtimestamp(now_ms / 1000, zone).date()
                days = (current_day - observed_day).days
                # Weekends do not age a daily stock signal by trading sessions.
                # Holidays count conservatively as weekdays; no bars are invented.
                sessions = sum((observed_day + timedelta(days=day)).weekday() < 5 for day in range(1, min(days, 14) + 1))
                stale_signal = days < 0 or days > 14 or sessions > 2
            if signal_ms > quote["timestamp_ms"] or stale_signal:
                raise ValueError("Signal is future dated or stale")
            if signal_ms <= result.setdefault("seen_signals", {}).get(signal_identity, 0):
                continue
            result["seen_signals"][signal_identity] = signal_ms
            if not signal_target(spec, bars):
                continue
            _revalue(result)
            fee_rate = limits["fee_bps"] / 10_000
            slip_rate = limits["slippage_bps"] / 10_000
            # Include entry fees and immediate slippage in post-fill equity caps.
            fraction = min(limits["allocation_fraction"], limits["max_symbol_fraction"])
            fill_price = quote["price"] * (1 + slip_rate)
            mark_ratio = quote["price"] / fill_price
            loss_ratio = 1 + fee_rate - mark_ratio
            gross_limit = limits["max_gross_fraction"]
            gross_capacity = max(0.0, (result["equity"] * gross_limit - result["gross_exposure"]) / (mark_ratio + gross_limit * loss_ratio))
            # Reserve costs for the remaining gross budget so earlier positions
            # also respect the symbol cap after subsequent entries pay their fees.
            minimum_equity = result["equity"] - gross_capacity * loss_ratio
            symbol_capacity = minimum_equity * fraction / mark_ratio
            notional = min(symbol_capacity, gross_capacity, result["cash"] / (1 + fee_rate))
            if notional < 1:
                errors.append("Paper cash or exposure limit reached")
                continue
            quantity = notional / fill_price
            fee = notional * fee_rate
            position_id = f"{identity}:{signal_ms}"
            result["cash"] -= notional + fee
            result["fees_paid"] += fee
            result["positions"].append({"id": position_id, "strategy_id": identity, "symbol": symbol, "side": "long",
                "quantity": quantity, "entry_price": fill_price, "mark_price": quote["price"], "cost_basis": notional + fee,
                "last_mark_ms": quote["timestamp_ms"], "opened_at": _iso(now_ms), "opened_at_ms": now_ms,
                "signal_ms": signal_ms, "unrealized_pnl": quantity * quote["price"] - notional - fee})
            result["trades"].append({"id": position_id + ":open", "strategy_id": identity, "symbol": symbol,
                "side": "buy", "quantity": quantity, "price": fill_price, "fee": fee, "timestamp": _iso(now_ms),
                "timestamp_ms": now_ms, "quote_timestamp_ms": quote["timestamp_ms"], "realized_pnl": 0.0,
                "reason": "Accepted frozen strategy and executor activation", "mode": "paper"})
            result["last_action"] = {"status": "filled", "reason": "Bounded paper entry at observed quote plus costs"}
            _revalue(result)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            errors.append(f"{symbol}: {exc}")
    _revalue(result)
    result["last_execution_ms"] = now_ms
    result["valuation_complete"] = not stale_position
    if not stale_position:
        result["as_of_ms"], result["as_of"] = now_ms, _iso(now_ms)
        point = {"timestamp": _iso(now_ms), "pnl": result["equity"] - result["initial_equity"]}
        if not result["pnl_history"] or result["pnl_history"][-1] != point:
            result["pnl_history"].append(point)
    result["limits"] = limits
    result["warnings"] = list(dict.fromkeys(errors))
    if errors and result["last_action"]["status"] == "unchanged":
        result["last_action"] = {"status": "blocked", "reason": "; ".join(result["warnings"])}
    return result
