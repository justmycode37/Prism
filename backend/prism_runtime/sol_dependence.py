"""Conservative SOL shadow-report diagnostic, not a proof of independence.

This exploratory screening rule is frozen in the local behavior digest. It
raises the evidence requirement beyond the original twelve five-day blocks,
but correlations at other lags, nonstationarity and model selection can still
invalidate the underlying sign-test p-values.
"""
from __future__ import annotations

import math
import statistics

_MIN_BLOCKS = 20
_MAX_UPPER_HEURISTIC = .5
_Z = 1.959963984540054
_BASELINES = ('benchmark_return', 'market_return', 'sector_return')


def assess_sol_dependence(observations, *, block_sessions=5):
    """Screen serial dependence in complete paired forward returns.

    The Fisher-transform margin is an exploratory finite-sample heuristic,
    not a calibrated confidence interval for overlapping lagged pairs. Passing
    this test does not establish independence, stationarity, or profitability.
    """
    if type(block_sessions) is not int or block_sessions < 5:
        raise ValueError('SOL dependence blocks must span at least five sessions')
    result = {'status': 'insufficient', 'blocks_required': _MIN_BLOCKS,
              'block_sessions': block_sessions, 'max_upper_heuristic': _MAX_UPPER_HEURISTIC,
              'benchmarks': {}, 'independence_verified': False,
              'limitations': ['Exploratory Fisher margin is not a valid confidence interval for dependent overlapping pairs',
                  'Low estimated lag-one correlation cannot exclude longer-range dependence or regime changes',
                  'Synthetic calibration is not a market profitability test']}
    if len(observations) < _MIN_BLOCKS * block_sessions:
        result['reason'] = f'At least 20 complete {block_sessions}-session blocks are required for SOL dependence screening'
        return result
    try:
        initial = observations[0]['initial_equity']
        if type(initial) not in (int, float) or not math.isfinite(initial) or initial <= 0:
            raise ValueError('Invalid initial capital')
        previous, paired = initial, {name: [] for name in _BASELINES}
        for row in observations:
            equity = row['equity']
            if type(equity) not in (int, float) or not math.isfinite(equity) or equity <= 0:
                raise ValueError('Invalid equity')
            for name in _BASELINES:
                baseline = row[name]
                if type(baseline) not in (int, float) or not math.isfinite(baseline) or baseline <= -1:
                    raise ValueError('Invalid baseline')
                paired[name].append(math.log(equity / previous) - math.log1p(baseline))
            previous = equity
        passed = True
        for name, values in paired.items():
            blocks = [sum(values[i:i+block_sessions]) for i in range(0, len(values)-block_sessions+1, block_sessions)]
            left, right = blocks[:-1], blocks[1:]
            mean_left, mean_right = statistics.fmean(left), statistics.fmean(right)
            a = [x - mean_left for x in left]
            b = [x - mean_right for x in right]
            denominator = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
            if not math.isfinite(denominator) or denominator <= 1e-28:
                correlation = upper = None
                passed = False
            else:
                correlation = max(-1., min(1., sum(x*y for x, y in zip(a, b)) / denominator))
                upper = math.tanh(math.atanh(min(abs(correlation), 1-1e-12)) + _Z / math.sqrt(len(left)-3))
                if upper > _MAX_UPPER_HEURISTIC:
                    passed = False
            result['benchmarks'][name] = {'block_count': len(blocks), 'lag1_correlation': correlation,
                                          'absolute_lag1_upper_heuristic': upper}
        result['status'] = 'passed_screen' if passed else 'insufficient'
        result['reason'] = ('No large lag-one correlation was detected by the exploratory screen'
                            if passed else 'SOL dependence screen is inconclusive or indicates dependence')
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        result['reason'] = 'SOL dependence screen requires valid paired observations and variation'
    return result
