"""Predeclared prospective reviews and operating economics, independent of narratives."""
from __future__ import annotations
import math
import statistics

from .analytics import _sign_test


def forward_report(observations, config=None, *, performance_basis=None):
    settings = (config or {}).get('research', config or {})
    if performance_basis is None:
        performance_basis = settings.get('performance_basis', 'operating')
    if not isinstance(performance_basis, str) or performance_basis not in {'operating', 'trading'}:
        raise ValueError('Unsupported performance basis')
    minimum = max(60, settings.get('min_forward_sessions', 60))
    minimum_trades = max(8, settings.get('min_forward_trades', 8))
    block = max(5, settings.get('forward_block_sessions', 5))
    min_blocks = max(12, settings.get('min_forward_blocks', 12))
    interval = max(20, settings.get('review_interval_sessions', 20))
    alpha = min(.05, settings.get('alpha_budget', .05))
    result = {'status': 'inconclusive', 'sessions': len(observations), 'reasons': [],
              'net_return': None, 'excess_return': None, 'operating_pnl_usd': None,
              'trading_pnl_usd': None, 'operating_cost_usd': None,
              'performance_basis': performance_basis,
              'assumptions': 'Disjoint block signs must be approximately independent and stationary. No profit probability is reported.'}
    if not observations:
        result['reasons'] = ['Awaiting registered future sessions']
        return result
    if any(row.get('phase', 'forward') != 'forward' for row in observations):
        result['reasons'] = ['Development evaluations cannot be prospective observations']
        return result
    if any(not row.get('valuation_complete', False) for row in observations):
        result['reasons'] = ['Incomplete valuations cannot support promotion']
        return result
    if performance_basis == 'operating' and any(row.get('source_cost_complete') is False for row in observations):
        result['reasons'] = ['Imported source costs are incomplete; promotion requires known operating costs']
        return result
    dates = [row.get('session_ms', 0) for row in observations]
    if any(not isinstance(d, int) or isinstance(d, bool) or d <= 0 for d in dates) or any(b <= a for a,b in zip(dates,dates[1:])):
        raise ValueError('Forward sessions must be unique and chronological')
    initial = observations[0].get('initial_equity', 100000.)
    if not isinstance(initial, (int,float)) or isinstance(initial,bool) or not math.isfinite(initial) or initial <= 0:
        raise ValueError('Positive forward capital required')
    previous, operating_previous, peak, drawdown, costs = initial, initial, initial, 0., 0.
    net_returns = []
    insolvent = False
    baseline_returns = {name: [] for name in ('benchmark_return','market_return','sector_return')}
    missing = set()
    previous_trade_count = 0
    for row in observations:
        equity = row.get('equity')
        if not isinstance(equity,(float,int)) or isinstance(equity,bool) or not math.isfinite(equity) or equity <= 0:
            raise ValueError('Finite positive observed equity required')
        daily_cost = 0.
        for name in ('research_cost_usd','data_cost_usd','infrastructure_cost_usd'):
            value = row.get(name, 0.)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError('Each operating cost must be finite and nonnegative')
            daily_cost += value
        if not math.isfinite(daily_cost) or not math.isfinite(costs + daily_cost):
            raise ValueError('Operating costs exceed finite accounting bounds')
        trades = row.get('completed_trades', 0)
        if isinstance(trades, bool) or not isinstance(trades, int) or trades < previous_trade_count:
            raise ValueError('Completed round-trip counts must be nonnegative monotonic integers')
        previous_trade_count = trades
        if row.get('initial_equity', initial) != initial:
            raise ValueError('Forward capital basis cannot change within an experiment')
        costs += daily_cost
        operating = equity-costs
        if operating <= 0 and not insolvent:
            insolvent = True
            if performance_basis == 'operating':
                result.update(status='retired', reasons=['Operating capital exhausted'],
                              trading_pnl_usd=equity-initial,
                              operating_pnl_usd=operating-initial, operating_cost_usd=costs)
                return result
        metric_equity = equity if performance_basis == 'trading' else operating
        metric_previous = previous if performance_basis == 'trading' else operating_previous
        net_returns.append(metric_equity/metric_previous-1)
        if performance_basis == 'trading':
            previous = equity
        else:
            operating_previous = operating
        peak = max(peak, metric_equity)
        drawdown = max(drawdown,1-metric_equity/peak)
        for name in baseline_returns:
            value = row.get(name)
            if not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(value) or value <= -1:
                missing.add(name)
                baseline_returns[name].append(0.)
            else:
                baseline_returns[name].append(value)
    benchmark_wealth = {name: math.prod(1+r for r in values) for name,values in baseline_returns.items()}
    if any(not math.isfinite(value) for value in benchmark_wealth.values()):
        raise ValueError('Benchmark compounding exceeds finite accounting bounds')
    final_trading_equity = observations[-1]['equity']
    final_operating_equity = final_trading_equity-costs
    final_metric_equity = final_trading_equity if performance_basis == 'trading' else final_operating_equity
    total = final_metric_equity/initial-1
    result.update(net_return=total, operating_pnl_usd=final_operating_equity-initial, trading_pnl_usd=final_trading_equity-initial,
        operating_cost_usd=costs, max_drawdown=drawdown, volatility=statistics.stdev(net_returns) if len(net_returns)>1 else 0.,
        completed_trades=observations[-1].get('completed_trades',0), missing_baselines=sorted(missing),
        excess_return=None if 'benchmark_return' in missing else total-(benchmark_wealth['benchmark_return']-1),
        baseline_returns={k: None if k in missing else v-1 for k,v in benchmark_wealth.items()})
    if drawdown > min(.10,settings.get('max_forward_drawdown',.10)) or observations[-1].get('risk_halted'):
        result.update(status='retired', reasons=['Prospective drawdown limit exceeded'])
        return result
    reasons = []
    n = len(observations)
    if n < minimum:
        reasons.append(f'Need at least {minimum} observed future sessions')
    if observations[-1].get('completed_trades',0) < minimum_trades:
        reasons.append(f'Need at least {minimum_trades} completed prospective trades')
    if n % interval:
        reasons.append('Awaiting the next predeclared review checkpoint')
    if missing:
        reasons.append('Missing prospective stock, market or sector baseline observations')
    # Every review receives a summable portion of its experiment allocation.
    look = max(1,n//interval)
    threshold = alpha/(look*(look+1)*3)
    statistics_by_benchmark = {}
    for name, values in baseline_returns.items():
        excess = [(1+a)/(1+b)-1 for a,b in zip(net_returns,values)]
        p, count, positives, correlation = _sign_test(excess,block)
        statistics_by_benchmark[name] = {'p_value':p,'nonzero_blocks':count,'positive_blocks':positives,'lag1_correlation':correlation}
        if count < min_blocks or abs(correlation) > .5 or p > threshold:
            reasons.append('Insufficient independent search-adjusted excess-return evidence versus '+name)
    if total <= 0 or any(total-(value-1) <= settings.get('min_excess_return',0.) for value in benchmark_wealth.values()):
        reasons.append(('Trading' if performance_basis == 'trading' else 'Operating') + ' return must be positive and exceed every declared benchmark')
    result.update(reasons=list(dict.fromkeys(reasons)), statistics=statistics_by_benchmark,
                  review_number=look, alpha_budget=alpha, per_comparison_threshold=threshold,
                  status='inconclusive' if reasons else 'promoted')
    return result
