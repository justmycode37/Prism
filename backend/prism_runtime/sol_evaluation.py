"""Offline uncertainty summaries for aligned SOL strategy and baseline returns."""
from __future__ import annotations

import math
import random


def paired_block_bootstrap(strategy_returns, baseline_returns, *, block_size=5,
                           replicates=4000, confidence=0.95, seed=0):
    """Estimate mean per-period excess return with a circular paired block bootstrap.

    Inputs must be synchronized net returns (including modeled execution costs).
    Pairing preserves shared market moves; circular blocks retain local serial
    dependence. This is descriptive uncertainty, not a guarantee of future returns.
    The bootstrap fraction is not a posterior probability or a p-value; the
    interval does not correct for searching multiple candidates. Callers must
    establish timestamp alignment, source provenance and independent evaluation.
    """
    strategy = list(strategy_returns)
    baseline = list(baseline_returns)
    if len(strategy) != len(baseline) or not strategy:
        raise ValueError('Return series must be nonempty and aligned')
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
           for x in strategy + baseline):
        raise ValueError('Returns must be finite numbers')
    if not isinstance(block_size, int) or isinstance(block_size, bool) or not 1 <= block_size <= len(strategy):
        raise ValueError('block_size must be a positive integer no greater than the sample length')
    if not isinstance(replicates, int) or isinstance(replicates, bool) or replicates < 100:
        raise ValueError('replicates must be an integer of at least 100')
    if type(seed) is not int:
        raise ValueError('seed must be an integer for reproducibility')
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 < confidence < 1:
        raise ValueError('confidence must be between zero and one')
    excess = [a - b for a, b in zip(strategy, baseline)]
    if any(not math.isfinite(value) for value in excess):
        raise ValueError('Excess returns must be finite')
    n = len(excess)
    # Divide before summing, so finite returns cannot overflow merely because
    # many observations share the same sign.
    observed = math.fsum(value / n for value in excess)
    rng = random.Random(seed)
    samples = []
    for _ in range(replicates):
        sample = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(excess[(start + offset) % n] for offset in range(block_size))
        samples.append(math.fsum(value / n for value in sample[:n]))
    samples.sort()
    tail = (1 - confidence) / 2
    low = samples[int(tail * replicates)]
    high = samples[min(replicates - 1, math.ceil((1 - tail) * replicates) - 1)]
    return {'observations': n, 'block_size': block_size, 'replicates': replicates,
            'confidence': float(confidence), 'mean_excess_return': observed,
            'interval_low': low, 'interval_high': high,
            'bootstrap_fraction_positive': sum(value > 0 for value in samples) / replicates,
            'seed': seed, 'method': 'paired_circular_block_bootstrap'}
