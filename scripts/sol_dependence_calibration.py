#!/usr/bin/env python3
"""Offline holdout stress for the exploratory SOL serial-dependence screen."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from prism_runtime.experiments import forward_report
from prism_runtime.sol_dependence import assess_sol_dependence
from scripts.sol_null_calibration import wilson


def run(paths=500, seed=20261103):
    if type(paths) is not int or not 1 <= paths <= 100000 or type(seed) is not int:
        raise ValueError('A bounded path count and integer seed are required')
    checkpoints = tuple(range(60, 181, 20))
    scenarios = []
    for index, rho in enumerate((0., .8, .95)):
        rng = random.Random(seed + index)
        raw_any = screened_any = 0
        by_look = {n: {'raw_promoted': 0, 'screened_promoted': 0} for n in checkpoints}
        for _ in range(paths):
            previous = rng.gauss(0., .0005)
            equity, rows, raw_path, screened_path = 100., [], False, False
            for day in range(checkpoints[-1]):
                previous = rho * previous + math.sqrt(1-rho*rho) * rng.gauss(0., .0005)
                equity *= 1 + previous
                rows.append({'session_ms': 1_700_000_000_000 + day * 86_400_000,
                    'initial_equity': 100., 'equity': equity, 'valuation_complete': True,
                    'source_cost_complete': True, 'completed_trades': (day+1)//5,
                    'benchmark_return': 0., 'market_return': 0., 'sector_return': 0.})
                n = day + 1
                if n not in by_look:
                    continue
                promoted = forward_report(rows, {'alpha_budget': .025})['status'] == 'promoted'
                screened = promoted and assess_sol_dependence(rows)['status'] == 'passed_screen'
                by_look[n]['raw_promoted'] += promoted
                by_look[n]['screened_promoted'] += screened
                raw_path |= promoted
                screened_path |= screened
            raw_any += raw_path
            screened_any += screened_path
        scenarios.append({'rho': rho, 'paths': paths, 'raw_any_promotion': raw_any,
            'screened_any_promotion': screened_any,
            'screened_any_fraction': screened_any / paths,
            'screened_any_wilson_95': wilson(screened_any, paths),
            'by_look': {str(n): by_look[n] for n in checkpoints}})
    return {'purpose': 'Exploratory synthetic stress of SOL shadow screening, not market performance',
        'seed': seed, 'alpha_budget': .025, 'checkpoints': list(checkpoints),
        'scenarios': scenarios,
        'limitations': ['Independent simulated paths; within-path IID or stationary Gaussian AR(1) daily returns',
            'Zero-mean arithmetic returns can have nonzero expected compounded wealth',
            'Three identical zero-return benchmarks and synthetic trade counts are not realistic',
            'This does not run the full LiveResearch gate or establish independence for SOL',
            'The screen was developed using other simulations; this seed is a separate synthetic holdout',
            'A finite collection of scenarios cannot establish universal type-I error control']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--paths', type=int, default=500)
    parser.add_argument('--seed', type=int, default=20261103)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.paths, args.seed), indent=2, allow_nan=False))
    except ValueError as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
