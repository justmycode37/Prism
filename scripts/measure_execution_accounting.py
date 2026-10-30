#!/usr/bin/env python3
"""Profile Prism's complete execution-accounting scan with offline paper data.

Run from the repository root, for example:
  PYTHONPATH=backend:backend/tests python3 scripts/measure_execution_accounting.py \
      --orders 1000 10000 50000 --warm-reads 20 --output /tmp/prism-accounting.json

The benchmark creates a fresh temp SQLite store for each count, reuses the
SOL end-to-end paper fixture, and directly inserts deterministic local order
and execution rows. It never handles an event, calls an agent, or starts a
service. The fixture's strategy id is used only as ledger ownership metadata;
eligibility and trading authority are not evaluated.
"""
from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
import time
import tracemalloc

from test_sol_swarm_e2e import SolSwarmEndToEndTests


QTY = 0.0001
PRICE = 100.0
BATCH_SIZE = 1000


def _populate(case, count):
    adapter, store = case.adapter, case.store
    mode = adapter.mode
    strategy_id = case.strategy_id
    now_ms = round(case.clock() * 1000)
    intent = json.dumps({"category": "linear", "symbol": "SOLUSDT", "side": "Buy",
        "orderType": "Market", "qty": str(QTY), "price": str(PRICE),
        "reduceOnly": False, "strategy_id": strategy_id}, separators=(",", ":"))
    revision_before = adapter.accounting_revision()
    started = time.perf_counter()
    with store.connection() as db:
        db.executemany("""INSERT INTO bybit_orders(mode,order_link_id,order_id,category,symbol,
            status,intent,filled_qty,average_price,fee_total,realized_pnl,stop_loss,
            protection_verified,exchange_status,last_mutation,created_at_ms,updated_at_ms,fee_breakdown)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ((mode, f"bench-order-{index:07d}", f"bench-exchange-{index:07d}", "linear", "SOLUSDT",
              "filled", intent, str(QTY), str(PRICE), "0", "0", None, 0, "Filled", None,
              now_ms + index, now_ms + index, "{}") for index in range(count)))
        db.executemany("""INSERT INTO bybit_executions(mode,exec_id,order_link_id,order_id,
            category,symbol,qty,price,fee,fee_currency,realized_pnl,exec_time_ms,value)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ((mode, f"bench-fill-{index:07d}", f"bench-order-{index:07d}",
              f"bench-exchange-{index:07d}", "linear", "SOLUSDT", str(QTY), str(PRICE),
              "0", "USDT", "0", now_ms + index,
              json.dumps({"execId": f"bench-fill-{index:07d}",
                "orderLinkId": f"bench-order-{index:07d}",
                "orderId": f"bench-exchange-{index:07d}", "category": "linear",
                "symbol": "SOLUSDT", "side": "Buy", "execQty": str(QTY),
                "execPrice": str(PRICE), "execFee": "0", "feeCurrency": "USDT",
                "execTime": str(now_ms + index), "execType": "Trade"}, separators=(",", ":")))
             for index in range(count)))
    insert_seconds = time.perf_counter() - started
    revision_after = adapter.accounting_revision()
    if revision_after["orders"] - revision_before["orders"] != count:
        raise AssertionError("Order revision does not cover every inserted fixture row")
    if revision_after["executions"] - revision_before["executions"] != count:
        raise AssertionError("Execution revision does not cover every inserted fixture row")
    with store.connection() as db:
        order_count = db.execute("SELECT COUNT(*) FROM bybit_orders WHERE mode=?", (mode,)).fetchone()[0]
        execution_count = db.execute("SELECT COUNT(*) FROM bybit_executions WHERE mode=?", (mode,)).fetchone()[0]
    if order_count != count or execution_count != count:
        raise AssertionError("SQLite source ledger count differs from the requested benchmark size")
    return {"insert_seconds": round(insert_seconds, 6), "source_order_count": order_count,
            "source_execution_count": execution_count, "revision": revision_after}


def _assert_complete(case, snapshot, count):
    if snapshot["order_count"] != count or snapshot["active_order_count"] != 0:
        raise AssertionError("Accounting snapshot omitted or misclassified order history")
    fills = snapshot["fills"].get(("linear", "SOLUSDT"), {})
    if set(fills) != {case.strategy_id}:
        raise AssertionError("Full execution ownership did not resolve to the fixture strategy")
    lot = fills[case.strategy_id]
    expected_quantity = count * QTY
    expected_cost = count * QTY * PRICE
    if abs(lot["quantity"] - expected_quantity) > max(1e-9, expected_quantity * 1e-10):
        raise AssertionError("Derived inventory quantity does not include the entire ledger")
    if abs(lot["cost"] - expected_cost) > max(1e-7, expected_cost * 1e-10):
        raise AssertionError("Derived inventory cost does not include the entire ledger")
    # Display rows remain bounded; full accounting is independently proven by
    # the complete input count and quantity/cost sums above.
    if len(snapshot["orders"]) != min(count, 64):
        raise AssertionError("Terminal display history is not the expected bounded projection")
    retained_fills = snapshot["recent_fills"].get(case.strategy_id, [])
    if len(retained_fills) != min(count, 64):
        raise AssertionError("Recent-fill projection is not bounded at its expected size")
    if len(snapshot["executions"]) != min(count, 64):
        raise AssertionError("Bounded execution projection has an unexpected size")
    if snapshot["incomplete"]:
        raise AssertionError("Complete deterministic fixture was marked incomplete")
    return {"derived_owner_count": len(fills), "derived_total_quantity": lot["quantity"],
            "derived_total_cost": lot["cost"], "retained_orders": len(snapshot["orders"]),
            "retained_recent_fills": len(retained_fills),
            "retained_owner_count": len(snapshot["recent_fills"]),
            "retained_execution_projection": len(snapshot["executions"])}


def profile_count(count, warm_reads):
    case = SolSwarmEndToEndTests(
        methodName="test_closed_candle_fans_out_to_luna_then_sol_intent_is_revalidated_on_paper")
    case.setUp()
    try:
        source = _populate(case, count)
        adapter = case.adapter
        order_scan = adapter.iter_orders
        execution_scan = adapter.iter_executions
        scans = {"orders": 0, "executions": 0}
        batches = {"orders": set(), "executions": set()}

        def counted_orders(**kwargs):
            scans["orders"] += 1
            batches["orders"].add(kwargs.get("batch_size"))
            return order_scan(**kwargs)

        def counted_executions(**kwargs):
            scans["executions"] += 1
            batches["executions"].add(kwargs.get("batch_size"))
            return execution_scan(**kwargs)

        adapter.iter_orders = counted_orders
        adapter.iter_executions = counted_executions
        gc.collect()
        tracemalloc.start()
        started = time.perf_counter()
        snapshot = case.controller._accounting_snapshot()
        cold_seconds = time.perf_counter() - started
        _cold_current, cold_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        totals = _assert_complete(case, snapshot, count)
        if scans != {"orders": 1, "executions": 1}:
            raise AssertionError("Cold accounting did not scan each complete ledger exactly once")
        if batches != {"orders": {BATCH_SIZE}, "executions": {BATCH_SIZE}}:
            raise AssertionError("Accounting scan did not use the expected bounded iterator page size")

        gc.collect()
        tracemalloc.start()
        started = time.perf_counter()
        for _ in range(warm_reads):
            repeated = case.controller._accounting_snapshot()
            if repeated is not snapshot:
                raise AssertionError("Unchanged ledger revision did not reuse the derived snapshot")
        warm_seconds = time.perf_counter() - started
        _warm_current, warm_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        if scans != {"orders": 1, "executions": 1}:
            raise AssertionError("An unchanged-revision read repeated the full-ledger scan")
        if len(case.responses.requests) != 0:
            raise AssertionError("Benchmark unexpectedly made a model-provider request")
        if not all("/v5/market/" in row["url"] for row in case.public.calls):
            raise AssertionError("Fixture attempted a non-market HTTP route")
        return {"orders_and_executions": count, **source, **totals,
            "cold_scan_seconds": round(cold_seconds, 6), "cold_peak_bytes": cold_peak,
            "warm_reads": warm_reads, "warm_total_seconds": round(warm_seconds, 6),
            "warm_mean_ms": round(warm_seconds * 1000 / warm_reads, 6) if warm_reads else 0,
            "warm_peak_bytes": warm_peak, "ledger_iterator_scans": scans,
            "iterator_batch_size": BATCH_SIZE,
            "provider_requests": len(case.responses.requests),
            "scope": "offline paper ledger only; fixture strategy ownership injected; no eligibility decision"}
    finally:
        case.tearDown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orders", nargs="+", type=int, default=[1000, 10000, 50000],
                        help="complete order/execution ledger sizes to profile")
    parser.add_argument("--warm-reads", type=int, default=20,
                        help="unchanged-revision reads per fixture (default: 20)")
    parser.add_argument("--output", type=Path, help="optional JSON artifact path")
    args = parser.parse_args()
    if args.warm_reads < 0 or any(count <= 0 for count in args.orders):
        parser.error("--orders counts must be positive and --warm-reads must be nonnegative")
    report = {"benchmark": "prism_execution_accounting", "fixture": "temp SQLite + PaperExchange + BybitAdapter",
        "batch_size": BATCH_SIZE, "warm_reads_per_count": args.warm_reads,
        "results": [profile_count(count, args.warm_reads) for count in args.orders]}
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
