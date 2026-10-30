"""Deterministic, bounded candidate expansion for paper research.

This module generates only frozen parameter specifications. It performs no
model calls, data fetching, or execution and is safe to use from the runtime's
candidate-expansion hook as well as the offline research CLI.
"""
from __future__ import annotations

from itertools import product

from .analytics import KINDS, canonical_spec, spec_hash


SEARCH_PROTOCOL = "deterministic-grid-v1"
DEFAULT_MAX_CANDIDATES = 256
MAX_CANDIDATES = 5_000
DEFAULT_LOOKBACKS = (3, 5, 8, 10, 15, 20, 30, 40, 60, 80, 100)
FAMILY_ORDER = ("momentum", "mean_reversion", "breakout", "event_surprise")
THRESHOLDS = {
    "momentum": (0.0, 0.002, 0.005, 0.01, 0.02, 0.04, 0.08, 0.12, 0.2),
    "mean_reversion": (0.1, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0),
    "breakout": (0.0, 0.002, 0.005, 0.01, 0.02, 0.04, 0.08, 0.12, 0.2),
    "event_surprise": (0.0, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0),
}


def candidate_space_size(markets, *, families=None, lookbacks=None, exclude_symbols=None):
    """Return the unbounded Cartesian grid size for the selected markets."""
    symbols = _symbols(markets, exclude_symbols=exclude_symbols)
    family_names = _families(families)
    windows = _lookbacks(lookbacks)
    return len(symbols) * len(windows) * sum(len(THRESHOLDS[name]) for name in family_names)


def generate_candidates(markets, *, evidence_ids, max_candidates=DEFAULT_MAX_CANDIDATES,
                        families=None, lookbacks=None, exclude_symbols=None):
    """Generate reproducibly ordered, duplicate-free strategy proposals.

    The grid cycles through every family for each lookback/threshold rank before
    advancing. With one symbol this gives broad family and parameter coverage;
    with several symbols it spreads each early grid slice across the universe.
    ``max_candidates`` is a hard cap, not a target that gets silently exceeded.
    """
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or not 1 <= max_candidates <= MAX_CANDIDATES:
        raise ValueError(f"max_candidates must be an integer from 1 to {MAX_CANDIDATES}")
    if not isinstance(evidence_ids, (list, tuple)) or not evidence_ids or any(
            not isinstance(item, str) or not item.strip() for item in evidence_ids):
        raise ValueError("Candidate expansion requires available dataset evidence IDs")
    evidence = list(dict.fromkeys(item.strip() for item in evidence_ids))
    symbols = _symbols(markets, exclude_symbols=exclude_symbols)
    family_names = _families(families)
    windows = _lookbacks(lookbacks)

    result = []
    seen = set()
    max_threshold_rank = max(len(THRESHOLDS[name]) for name in family_names)
    for lookback, threshold_rank, family in product(windows, range(max_threshold_rank), family_names):
        thresholds = THRESHOLDS[family]
        if threshold_rank >= len(thresholds):
            continue
        threshold = thresholds[threshold_rank]
        for market in symbols:
            proposal = {
                "symbol": market["symbol"],
                "kind": family,
                "parameters": {"lookback": lookback, "threshold": threshold},
                "hypothesis": (f"Deterministic {family} rule with lookback {lookback} and "
                               f"threshold {threshold:g}; development screen only."),
                "evidence_ids": evidence,
            }
            if market.get("security_id"):
                proposal["security_id"] = market["security_id"]
            spec = canonical_spec(proposal)
            identity = spec_hash(spec)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(proposal)
            if len(result) >= max_candidates:
                return result
    return result


def _symbols(markets, *, exclude_symbols=None):
    if not isinstance(markets, (list, tuple)):
        raise ValueError("markets must be a list")
    excluded = {str(value).strip().upper() for value in (exclude_symbols or []) if isinstance(value, str)}
    unique = {}
    for market in markets:
        if not isinstance(market, dict):
            continue
        # Explicit market/sector baseline histories are not search candidates.
        if market.get("benchmark_kind") or str(market.get("symbol", "")).strip().upper() in excluded:
            continue
        symbol = market.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            continue
        normalized = symbol.strip().upper()
        unique.setdefault(normalized, {"symbol": normalized, **(
            {"security_id": market["security_id"]} if market.get("security_id") else {})})
    if not unique:
        raise ValueError("No eligible market symbols for candidate expansion")
    return [unique[key] for key in sorted(unique)]


def _families(families):
    if families is None:
        return FAMILY_ORDER
    if not isinstance(families, (list, tuple)) or not families:
        raise ValueError("families must be a nonempty list of supported strategy families")
    if any(not isinstance(value, str) or value not in KINDS for value in families):
        raise ValueError("Supported families are momentum, mean_reversion, breakout, event_surprise")
    values = tuple(dict.fromkeys(families))
    return tuple(value for value in FAMILY_ORDER if value in values)


def _lookbacks(lookbacks):
    if lookbacks is None:
        return DEFAULT_LOOKBACKS
    if not isinstance(lookbacks, (list, tuple)) or not lookbacks:
        raise ValueError("lookbacks must be a nonempty list")
    if any(isinstance(value, bool) or not isinstance(value, int) or not 3 <= value <= 100 for value in lookbacks):
        raise ValueError("lookbacks must be integers from 3 to 100")
    return tuple(sorted(set(lookbacks)))
