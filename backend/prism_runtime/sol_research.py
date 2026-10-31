"""Offline, deterministic SOL momentum strategy comparisons on synthetic bars.

This is a research fixture only. It makes no claim about real market performance.
"""
from __future__ import annotations

import math


def compare_momentum(close_prices, *, horizons=(2, 4, 8), momentum_thresholds_bps=(0, 10, 25),
                     fee_bps=5, slippage_bps=5):
    """Compare long/flat close-to-close momentum with cash and buy/hold.

    Signal at close[t] uses only closes through t; desired position executes at
    close[t+1]. Returns are marked close-to-close thereafter. Turnover is absolute
    position change and incurs fee+slippage. Momentum thresholds are historical
    price changes, not forecasts of future return or transaction-cost estimates.
    """
    prices = list(close_prices)
    if len(prices) < 3 or any(isinstance(p, bool) or not isinstance(p, (int,float)) or
                              not math.isfinite(p) or p <= 0 for p in prices):
        raise ValueError("close_prices must contain at least three finite positive prices")
    for value in (fee_bps, slippage_bps, *momentum_thresholds_bps):
        if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or value < 0:
            raise ValueError("costs and hurdles must be finite nonnegative numbers")
    if not horizons or not momentum_thresholds_bps or any(type(h) is not int or h < 1 for h in horizons):
        raise ValueError("horizons must be positive integers")
    if any(h + 3 > len(prices) for h in horizons):
        raise ValueError("Each horizon requires enough prices for a signal, delayed execution and a scored return")
    bars = [prices[i+1] / prices[i] - 1 for i in range(len(prices)-1)]
    if any(not math.isfinite(r) or r <= -1 for r in bars):
        raise ValueError("price returns must be finite and above -100%")
    fee = (fee_bps + slippage_bps) / 10000
    if not math.isfinite(fee) or fee >= 1:
        raise ValueError("per-side cost must be less than 100%")
    def total_return(returns):
        wealth = 1.0
        for value in returns:
            wealth *= 1 + value
            if not math.isfinite(wealth) or wealth <= 0:
                raise ValueError("compounded wealth exceeds finite positive bounds")
        return wealth - 1
    output = {"assumptions": {"instrument":"synthetic SOL spot proxy", "signal":"trailing close momentum; long if positive and exceeds hurdle", "execution":"next bar close", "cost":"linear fee + slippage per unit turnover", "position":"long or cash; no leverage", "performance_claim":False}, "strategies": []}
    for h in horizons:
        for hurdle in momentum_thresholds_bps:
            positions = [0] * len(bars)
            # A signal at close i-1 executes at close i and earns i -> i+1.
            for i in range(1, len(bars)):
                decision_index = i - 1
                if decision_index >= h:
                    momentum = prices[decision_index] / prices[decision_index-h] - 1
                    positions[i] = int(momentum > hurdle / 10000)
            gross=[]; net=[]; turnover=[]; prev=0
            for i, pos in enumerate(positions):
                tr=abs(pos-prev); turnover.append(tr); gross.append(pos*bars[i]); net.append((1-tr*fee)*(1+pos*bars[i])-1); prev=pos
            output["strategies"].append({"horizon_bars":h,"momentum_threshold_bps":hurdle,"gross_returns":gross,"net_returns":net,"turnover":turnover,"total_return":total_return(net)})
    buy_hold = bars[:]
    buy_hold[0] = (1-fee)*(1+buy_hold[0])-1
    output["baselines"]={"cash_returns":[0.0]*len(bars),"buy_hold_returns":buy_hold,"buy_hold_total_return":total_return(buy_hold)}
    output["assumptions"]["valuation"] = "Open positions marked at final close; no terminal liquidation cost. Buy-and-hold pays initial entry costs. Funding, spread variation, market impact and operating costs are not modeled."
    return output
