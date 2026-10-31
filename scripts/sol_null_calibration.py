#!/usr/bin/env python3
"""Stress the forward gate using synthetic zero-mean return processes only."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from prism_runtime.experiments import forward_report


def wilson(count, total):
    """Approximate 95% binomial interval across independent simulated paths."""
    z = 1.959963984540054
    p = count / total
    denominator = 1 + z*z/total
    centre = (p + z*z/(2*total)) / denominator
    half = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / denominator
    return [max(0., centre-half), min(1., centre+half)]


def run(paths=1000, seed=20261030, alpha=.05):
    if type(paths) is not int or not 1 <= paths <= 100000 or type(seed) is not int:
        raise ValueError('paths must be an integer from 1 to 100000 and seed must be an integer')
    if type(alpha) not in (int, float) or not math.isfinite(alpha) or not 0 < alpha <= .05:
        raise ValueError('alpha must be finite and greater than zero, at most 0.05')
    scenarios = []
    for index, rho in enumerate((0., .8, .95)):
        rng = random.Random(seed + index)
        promoted = retired = 0
        for _ in range(paths):
            # Stationary AR(1) with zero mean and marginal daily volatility .05%.
            previous = rng.gauss(0., .0005)
            equity, rows = 100., []
            for day in range(60):
                previous = rho * previous + math.sqrt(1-rho*rho) * rng.gauss(0., .0005)
                equity *= 1 + previous
                rows.append({'session_ms': 1_700_000_000_000 + day * 86_400_000,
                    'initial_equity': 100., 'equity': equity, 'valuation_complete': True,
                    'source_cost_complete': True, 'completed_trades': (day+1)//5,
                    'benchmark_return': 0., 'market_return': 0., 'sector_return': 0.})
            status = forward_report(rows, {'alpha_budget': alpha})['status']
            promoted += status == 'promoted'
            retired += status == 'retired'
        scenarios.append({'rho': rho, 'paths': paths, 'promoted': promoted,
                          'retired': retired, 'promotion_fraction': promoted / paths,
                          'promotion_fraction_wilson_95': wilson(promoted, paths)})
    return {'purpose': 'Synthetic null calibration of the forward report, not market performance',
            'seed': seed, 'alpha_budget': alpha, 'sessions': 60, 'initial_equity': 100.,
            'daily_marginal_volatility': .0005, 'completed_trades': 12,
            'benchmarks': 'Three identical zero-return series; each scenario is tested once',
            'assumptions': ['Zero-mean arithmetic returns; correlated compounding may change expected wealth',
                            'Synthetic trade counts and costs are not a model of execution',
                            'IID and AR(1) processes do not establish the distribution of actual SOL returns',
                            'Intervals quantify Monte Carlo sampling uncertainty, not trading profitability',
                            'Calls forward_report alone, without full live-research eligibility gates'],
            'scenarios': scenarios}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--paths', type=int, default=1000)
    parser.add_argument('--seed', type=int, default=20261030)
    parser.add_argument('--alpha', type=float, default=.05)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.paths, args.seed, args.alpha), indent=2, allow_nan=False))
    except ValueError as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
