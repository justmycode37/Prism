#!/usr/bin/env python3
"""Offline development runner for explicit SOL momentum fixture comparisons."""
import argparse
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from prism_runtime.sol_research import compare_momentum


def run(settings):
    if not isinstance(settings, dict) or set(settings) - {"close_prices", "horizons", "momentum_thresholds_bps", "fee_bps", "slippage_bps", "cost_scenarios"}:
        raise ValueError("settings must be an object with supported fields only")
    if "close_prices" not in settings:
        raise ValueError("required input close_prices is absent")
    horizons = settings.get("horizons", [2, 4, 8])
    hurdles = settings.get("momentum_thresholds_bps", [0, 10, 25])
    if not isinstance(horizons, list) or not horizons or any(type(x) is not int or x < 1 for x in horizons):
        raise ValueError("horizons must be a nonempty list of positive integers")
    if not isinstance(hurdles, list) or not hurdles or any(type(x) not in (int, float) or not math.isfinite(x) or x < 0 for x in hurdles):
        raise ValueError("momentum_thresholds_bps must be a nonempty list of finite nonnegative numbers")
    args = dict(horizons=tuple(horizons), momentum_thresholds_bps=tuple(hurdles), fee_bps=settings.get("fee_bps", 5), slippage_bps=settings.get("slippage_bps", 5))
    if "cost_scenarios" not in settings:
        result = compare_momentum(settings["close_prices"], **args)
    else:
        scenarios = settings["cost_scenarios"]
        if not isinstance(scenarios, list) or not scenarios:
            raise ValueError("cost_scenarios must be a nonempty list")
        result = {"assumptions": None, "scenarios": []}
        for scenario in scenarios:
            if not isinstance(scenario, dict) or set(scenario) != {"name", "fee_bps", "slippage_bps"} or not isinstance(scenario["name"], str) or not scenario["name"]:
                raise ValueError("each cost scenario needs name, fee_bps, and slippage_bps")
            out = compare_momentum(settings["close_prices"], **(args | {"fee_bps": scenario["fee_bps"], "slippage_bps": scenario["slippage_bps"]}))
            result["assumptions"] = out["assumptions"]
            result["scenarios"].append({"name": scenario["name"], "fee_bps": scenario["fee_bps"], "slippage_bps": scenario["slippage_bps"], "comparison": out})
    result["development_only"] = True
    result["notice"] = "Synthetic/offline development comparison; not evidence of profitability. No winner is selected retrospectively."
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path, help="JSON settings fixture")
    ns = parser.parse_args()
    try:
        print(json.dumps(run(json.loads(ns.fixture.read_text())), indent=2, sort_keys=True, allow_nan=False))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
