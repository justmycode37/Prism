"""Offline comparison of strategy and benchmark simple returns.

All timestamps are integer UTC seconds (Unix epoch). The expected schedule is
independent of both series and defines the complete evaluation horizon.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable


@dataclass(frozen=True, order=True)
class Interval:
    start_utc: int
    end_utc: int


@dataclass(frozen=True)
class IntervalReturn:
    start_utc: int
    end_utc: int
    value: float


@dataclass(frozen=True)
class Series:
    returns: tuple[IntervalReturn, ...]


def compare(strategy: Series, benchmark: Series, *, expected_schedule: Iterable[Interval],
            annualization_periods_per_year: float | None = None) -> dict:
    """Compare returns on exact scheduled intervals; annualization is opt-in.

    Active metrics are arithmetic paired-return differences (mean, sample
    volatility, and information ratio); they are never compounded as wealth.
    """
    schedule = tuple(expected_schedule)
    if not schedule:
        raise ValueError("expected schedule must not be empty")
    for i, item in enumerate(schedule):
        if not isinstance(item, Interval) or type(item.start_utc) is not int or type(item.end_utc) is not int:
            raise ValueError("interval boundaries must be integer UTC instants")
        if item.start_utc >= item.end_utc:
            raise ValueError("interval end must be after start")
        if i and item.start_utc != schedule[i-1].end_utc:
            raise ValueError("expected schedule must be ordered, contiguous, and non-overlapping")

    if annualization_periods_per_year is not None and (
        isinstance(annualization_periods_per_year, bool) or not isinstance(annualization_periods_per_year, (int, float)) or
        not math.isfinite(annualization_periods_per_year) or annualization_periods_per_year <= 0
    ):
        raise ValueError("annualization assumption must be finite and positive")
    if annualization_periods_per_year is not None and len({item.end_utc - item.start_utc for item in schedule}) != 1:
        raise ValueError("period-count annualization requires equally sized intervals")

    def index(series: Series) -> dict[Interval, float]:
        if not isinstance(series, Series):
            raise ValueError("series must be a Series")
        result: dict[Interval, float] = {}
        for row in series.returns:
            if type(row.start_utc) is not int or type(row.end_utc) is not int:
                raise ValueError("return boundaries must be integer UTC instants")
            key = Interval(row.start_utc, row.end_utc)
            if key.start_utc >= key.end_utc:
                raise ValueError("interval end must be after start")
            if key in result:
                raise ValueError(f"duplicate interval: {key}")
            if isinstance(row.value, bool) or not isinstance(row.value, (int, float)) or not math.isfinite(row.value) or row.value <= -1:
                raise ValueError(f"invalid simple return for {key}")
            result[key] = float(row.value)
        if set(result) != set(schedule):
            raise ValueError("series must exactly match the expected interval schedule")
        return result

    s, b = index(strategy), index(benchmark)
    def wealth_metrics(values: list[float]) -> dict:
        wealth = peak = 1.0
        drawdown = 0.0
        for value in values:
            wealth *= 1 + value
            if not math.isfinite(wealth) or wealth <= 0:
                raise ValueError("compounded wealth exceeds finite positive bounds")
            peak = max(peak, wealth)
            drawdown = min(drawdown, wealth / peak - 1)
        out = {"total_return": wealth - 1, "ending_wealth": wealth, "max_drawdown": drawdown}
        if annualization_periods_per_year is not None:
            years = len(values) / annualization_periods_per_year
            out["annualized_return"] = wealth ** (1 / years) - 1
        return out

    sr = [s[i] for i in schedule]
    br = [b[i] for i in schedule]
    active = [x-y for x, y in zip(sr, br)]
    mean = sum(active) / len(active)
    variance = sum((x-mean)**2 for x in active) / (len(active)-1) if len(active) > 1 else None
    active_metrics = {"mean_interval_difference": mean,
                      "sample_volatility": math.sqrt(variance) if variance is not None else None,
                      "information_ratio": mean / math.sqrt(variance) if variance else None}
    if annualization_periods_per_year is not None:
        active_metrics["annualized_information_ratio"] = (mean / math.sqrt(variance) *
                                                            math.sqrt(annualization_periods_per_year) if variance else None)
    strategy_metrics, benchmark_metrics = wealth_metrics(sr), wealth_metrics(br)
    relative_wealth = strategy_metrics['ending_wealth'] / benchmark_metrics['ending_wealth']
    if not math.isfinite(relative_wealth) or relative_wealth <= 0:
        raise ValueError("relative wealth exceeds finite positive bounds")
    return {"intervals": [{"start_utc": i.start_utc, "end_utc": i.end_utc} for i in schedule],
            "observations": len(schedule), "strategy": strategy_metrics,
            "benchmark": benchmark_metrics, "active": active_metrics,
            "relative_wealth": relative_wealth,
            "annualization_periods_per_year": annualization_periods_per_year,
            "limitations": ["annualization is caller-specified and assumes the stated regular period count",
                            "benchmark total-return construction and valuation methodology are caller-owned",
                            "turnover and exposure-based metrics are unavailable"]}
