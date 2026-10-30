"""Event-driven Sol execution over the host Bybit and market-data services.

The controller owns no exchange transport.  All market/account reads and order
mutations go through the injected services, so development and tests can use
offline fixtures while an explicitly enabled runtime can use Bybit V5.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
from datetime import datetime, timezone

from .bybit import BybitError, OrderOutcomeUnknown
from .domain import identity
from .execution_contract import EXECUTION_TOOLS, SOL_EXECUTION_TOOLS
from .live_research import LiveResearch
from .risk_controls import ACTIVE_ORDERS, IntentRejected, protective_reasons, validate_intent
from .store import BudgetExceeded, encoded
from .trade_review import TradeReviewer
from .trading_modes import is_sol_mode, mode_settings


_TERMINAL = {"filled", "cancelled", "rejected"}
_PROTECTED_EVENT_KINDS = {"candle_closed", "intrabar", "signal", "fill", "order", "position",
                          "risk", "scheduled_event", "decision_deadline", "data_health"}
_MAX_SAME_MILLISECOND_FILLS = 10_000


def _execution_sequence(item):
    value = item.get('seq')
    if type(value) is int and value >= 0:
        return value
    # Old local paper ledgers predate the explicit seq field. Their execution
    # suffix is the durable simulator counter, not a lexicographic identifier.
    ident = item.get('execId', '')
    if value is None and isinstance(ident, str) and ident.startswith('paper-exec-'):
        suffix = ident[len('paper-exec-'):]
        if suffix.isascii() and suffix.isdigit() and len(suffix) <= 20:
            return int(suffix)
    return None


def _inventory_timeline(rows):
    """Order timestamp ties using measured sequence, with bounded buffering.

    Input rows are time-sorted normalized (time,item,owner,qty,price) tuples.
    Opposing fills for one owner/instrument require an actual total order;
    random execution IDs provide a stable fallback, never verification.
    """
    from itertools import chain, groupby
    for _, same_time in groupby(rows, key=lambda row: row[0]):
        first = next(same_time)
        second = next(same_time, None)
        if second is None:
            yield (*first, False)
            continue
        groups = {}
        count = 0
        for row in chain((first, second), same_time):
            count += 1
            if count > _MAX_SAME_MILLISECOND_FILLS:
                raise BybitError('Too many same-millisecond executions to verify bounded inventory')
            owner = row[2]
            key = tuple(str(owner.get(field, '')) for field in ('strategy_id', 'category', 'symbol'))
            groups.setdefault(key, []).append(row)
        for key in sorted(groups):
            group = groups[key]
            sequenced = [(_execution_sequence(row[1]), row) for row in group]
            complete_sequence = all(seq is not None for seq, _ in sequenced)
            operations = {}
            for seq, row in sequenced:
                item, owner = row[1:3]
                operation = (item.get('side', owner.get('side')),
                             item.get('reduce_only') is True or owner.get('reduce_only') is True)
                operations.setdefault(operation, set()).add(seq)
            ambiguous = len(operations) > 1 and (not complete_sequence or
                len(set.union(*operations.values())) < sum(map(len, operations.values())))
            if complete_sequence:
                sequenced.sort(key=lambda pair: (pair[0], str(pair[1][1].get('execId', ''))))
            else:
                sequenced.sort(key=lambda pair: str(pair[1][1].get('execId', '')))
            for _, row in sequenced:
                yield (*row, ambiguous)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _bounded_text(value, limit=2000):
    return str(value or "")[:limit]


def _order_link(tool_id):
    return "prism-" + hashlib.sha256(tool_id.encode("utf-8")).hexdigest()[:26]


class ExecutionController:
    """Operational Sol execution coordinator.

    Args:
        store: Prism's SQLite ``Store``.
        adapter: ``BybitAdapter`` (including its injected transport, if offline).
        market: ``MarketDataService`` with ``snapshot`` and ``snapshot_status``.
        memory: ``ResearchMemory``; execution observations are recorded per strategy.
        agents: ``ResponsesAgent``. Sol is called only from ``handle_event``.
        eligibility: Optional ``eligibility(strategy_id)`` callback. Defaults to
            ``LiveResearch(store).eligibility``.
        clock: Callable returning epoch seconds.

    ``permission_check`` defaults to true and may be replaced by a runtime
    enabled-state callback. It is checked at the start of a model tool and again
    immediately before every exposure-increasing submit or amendment.
    """

    def __init__(self, store, adapter, market, memory, agents, *, eligibility=None, clock=None,
                 trade_reviewer=None):
        if store is None or not hasattr(store, "connection"):
            raise ValueError("ExecutionController requires a Prism Store")
        if adapter is None or not hasattr(adapter, "submit_order") or not hasattr(adapter, "reconcile"):
            raise ValueError("ExecutionController requires a BybitAdapter-compatible service")
        if market is None or not hasattr(market, "snapshot"):
            raise ValueError("ExecutionController requires a MarketDataService-compatible service")
        if memory is None or not hasattr(memory, "record_execution"):
            raise ValueError("ExecutionController requires ResearchMemory")
        self.store, self.adapter, self.market, self.memory, self.agents = store, adapter, market, memory, agents
        self.clock = clock or getattr(store, "_clock", None) or time.time
        self.eligibility = eligibility or LiveResearch(store, clock=self.clock).eligibility
        self.permission_check = lambda: True
        self.support_context_provider = None
        self.trade_reviewer = trade_reviewer or TradeReviewer(agents, store, clock=self.clock)
        self._mutation_lock = threading.RLock()
        self._accounting_cache_lock = threading.RLock()
        self._accounting_cache = None
        self._cashflow_cache_lock = threading.RLock()
        self._cashflow_cache = None
        self._schema()

    def _schema(self):
        with self.store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS execution_account_state (
                    scope TEXT PRIMARY KEY, environment TEXT NOT NULL, value TEXT NOT NULL,
                    updated_at_ms INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_decisions (
                    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, status TEXT NOT NULL, value TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_execution_decision_fingerprint
                    ON execution_decisions(strategy_id,fingerprint,created_at_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_execution_decision_recent
                    ON execution_decisions(strategy_id,created_at_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_execution_decision_global_recent
                    ON execution_decisions(created_at_ms DESC);
                CREATE TABLE IF NOT EXISTS execution_mutations (
                    tool_id TEXT PRIMARY KEY, action TEXT NOT NULL, args_hash TEXT NOT NULL,
                    status TEXT NOT NULL, order_link_id TEXT, value TEXT, created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_order_controls (
                    order_link_id TEXT PRIMARY KEY, expires_at_ms INTEGER NOT NULL,
                    rationale TEXT NOT NULL, updated_at_ms INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_spot_protections (
                    environment TEXT NOT NULL, strategy_id TEXT NOT NULL, symbol TEXT NOT NULL,
                    desired_hash TEXT NOT NULL, order_link_id TEXT NOT NULL, generation INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(environment,strategy_id,symbol));
            """)

    def _now_ms(self):
        value = self.clock()
        if not _finite(value) or value < 0:
            raise ValueError("Execution clock must return finite nonnegative epoch seconds")
        return round(value * 1000)

    def _config(self):
        return self.store.config()

    def _environment(self):
        return str(getattr(self.adapter, "mode", self._config()["bybit"].get("mode", "unknown")))

    def _scope(self, account_id):
        # Store only a one-way account namespace, never an account credential or
        # account identifier, in the execution-state key.
        return hashlib.sha256(encoded([self._environment(), account_id]).encode("utf-8")).hexdigest()

    @staticmethod
    def _strategy_map(store, strategy_ids=None):
        with store.connection() as db:
            if strategy_ids is None:
                rows = db.execute("SELECT value FROM cascade_strategies ORDER BY rowid DESC").fetchall()
            else:
                ids = list({value for value in strategy_ids if isinstance(value, str) and value})
                rows = []
                for offset in range(0, len(ids), 400):
                    batch = ids[offset:offset + 400]
                    rows.extend(db.execute('SELECT value FROM cascade_strategies WHERE id IN (' +
                        ','.join('?' for _ in batch) + ') ORDER BY rowid DESC', batch).fetchall())
        result = {}
        for row in rows:
            value = json.loads(row[0])
            if isinstance(value, dict) and isinstance(value.get("id"), str):
                result.setdefault(value["id"], value)
        return result

    def _strategy(self, strategy_id):
        if not isinstance(strategy_id, str) or not strategy_id:
            return None
        strategy = self._strategy_map(self.store, [strategy_id]).get(strategy_id)
        from .trading_modes import is_sol_mode
        if strategy:
            selected = is_sol_mode(self._config())
            managed = strategy.get('trading_mode') == 'sol_perpetual'
            if selected != managed or selected and strategy_id != self.store.get_document('sol_mode_strategy_id'):
                return None
        return strategy

    def _permission_allowed(self):
        try:
            return self.permission_check() is True
        except Exception:
            return False

    def _actual_strategy_target(self, strategy_id):
        strategy = self._strategy(strategy_id)
        policy = strategy.get("market_policy", {}) if strategy else {}
        category, symbol = policy.get("category"), policy.get("symbol")
        if category not in {"spot", "linear"} or not symbol:
            return 0
        lots = self._accounting_snapshot()["fills"]
        owned = lots.get((category, symbol), {}).get(strategy_id, {})
        quantity = owned.get("quantity", 0.)
        return int(_finite(quantity) and quantity > 1e-12)

    def _freeze_snapshot(self, strategy_id):
        current_target = self._actual_strategy_target(strategy_id)
        setter = getattr(self.market, "set_current_target", None)
        if callable(setter):
            setter(strategy_id, current_target)
            value = self.market.snapshot(strategy_id)
        else:
            try:
                value = self.market.snapshot(strategy_id, current_target=current_target)
            except TypeError:
                value = self.market.snapshot(strategy_id)
        if not isinstance(value, dict):
            raise ValueError("Market service returned no snapshot")
        ident = value.get("snapshot_id")
        if not isinstance(ident, str) or not ident:
            captured = value.get("captured_at_ms")
            if not isinstance(captured, int) or isinstance(captured, bool):
                raise ValueError("Market snapshot has no trustworthy capture time")
            ident = self.store.save_snapshot("bybit_market", value, captured)
        persisted = self.store.snapshot(ident)
        if not persisted or persisted.get("kind") != "bybit_market":
            raise ValueError("Market snapshot is unknown or expired")
        result = copy.deepcopy(persisted["value"])
        result["snapshot_id"] = ident
        return result

    def _snapshot_for_intent(self, strategy_id, snapshot_id, approved):
        if snapshot_id not in approved:
            raise IntentRejected("Snapshot was not supplied by the host during this decision")
        status = self.market.snapshot_status(snapshot_id) if hasattr(self.market, "snapshot_status") else {"valid": False}
        if not status.get("valid"):
            raise IntentRejected("Market snapshot is unknown, corrected, or expired")
        persisted = self.store.snapshot(snapshot_id)
        if not persisted or persisted.get("kind") != "bybit_market":
            raise IntentRejected("Market snapshot is unknown or expired")
        value = copy.deepcopy(persisted["value"])
        value["snapshot_id"] = snapshot_id
        if value.get("strategy_id") != strategy_id:
            raise IntentRejected("Market snapshot belongs to a different strategy")
        return value

    def _approve_snapshot(self, strategy_id, snapshot_id, approved):
        if snapshot_id not in approved:
            latest = self._fresh_market(strategy_id)
            if latest.get("snapshot_id") != snapshot_id:
                raise IntentRejected("Snapshot was not supplied by the host during this decision")
            approved.add(snapshot_id)
        return self._snapshot_for_intent(strategy_id, snapshot_id, approved)

    def _fresh_market(self, strategy_id):
        value = self._freeze_snapshot(strategy_id)
        if value.get("strategy_id") != strategy_id:
            raise IntentRejected("Market service returned a different strategy")
        return value

    def _protection_market(self, strategy_id):
        """Read quote state for deterministic protection without a decision freeze."""
        target = self._actual_strategy_target(strategy_id)
        setter = getattr(self.market, "set_current_target", None)
        if callable(setter):
            setter(strategy_id, target)
        sampler = getattr(self.market, "sample", None)
        if callable(sampler):
            value = sampler(strategy_id, current_target=target)
        else:
            value = self._fresh_market(strategy_id)
        if not isinstance(value, dict) or value.get("strategy_id") != strategy_id:
            raise IntentRejected("Market service returned no current protection quote")
        return value

    def _orders(self, *, accounting=None):
        orders = (self._accounting_snapshot() if accounting is None else accounting)["orders"]
        controls = {}
        with self.store.connection() as db:
            # Read deadlines for current/projected orders without scanning
            # controls belonging only to compacted closed history.
            links = [row['order_link_id'] for row in orders if row.get('order_link_id')]
            for offset in range(0, len(links), 400):
                batch = links[offset:offset + 400]
                controls.update({row['order_link_id']: dict(row) for row in db.execute(
                    'SELECT * FROM execution_order_controls WHERE order_link_id IN (' +
                    ','.join('?' for _ in batch) + ')', batch)})
        result = []
        for order in orders:
            row = dict(order)
            control = controls.get(row.get("order_link_id"))
            if control:
                row["expires_at_ms"] = control["expires_at_ms"]
                row["rationale"] = control["rationale"]
            result.append(row)
        return result

    def _executions(self):
        return self._accounting_snapshot()["executions"]

    def _accounting_metadata_signature(self, instruments):
        """Read only fee-classification metadata; never trigger a network refresh."""
        if not instruments:
            return ()
        mode = getattr(self.adapter, "metadata_mode", self._environment())
        signature = []
        with self.store.connection() as db:
            for category, symbol in sorted(instruments):
                row = db.execute("SELECT value FROM bybit_instruments WHERE mode=? AND category=? AND symbol=?",
                                 (mode, category, symbol)).fetchone()
                if not row:
                    return None
                value = json.loads(row[0])
                signature.append((category, symbol, value.get("base_coin"), value.get("quote_coin")))
        return tuple(signature)

    def _accounting_snapshot(self):
        """Return a revision-checked, compact full-ledger accounting snapshot.

        The durable order/execution iterators are consumed once after each
        accounting-relevant revision. The cache retains derived lots, all
        unresolved orders, 64 recent terminal orders, and 64 recent fills per
        owner; it never retains the unbounded execution/order history.
        """
        revision_reader = getattr(self.adapter, "accounting_revision", None)
        order_reader = getattr(self.adapter, "iter_orders", None)
        execution_reader = getattr(self.adapter, "iter_executions", None)
        if not callable(revision_reader) or not callable(order_reader) or not callable(execution_reader):
            # Compatibility with small injected adapters. Their legacy methods
            # are already bounded and cannot provide a safe revision cache.
            try:
                orders = self.adapter.orders(limit=5000)
            except (AttributeError, TypeError):
                orders = []
            try:
                executions = self.adapter.executions(limit=10000)
            except (AttributeError, TypeError):
                executions = []
            fills, incomplete = self._attributed_inventory(orders, executions)
            projected, recent_fills = self._accounting_projection(orders, executions)
            return {"revision": None, "orders": projected,
                    "executions": [item for rows in recent_fills.values() for item in rows],
                    "recent_fills": recent_fills, "fills": fills, "incomplete": incomplete,
                    "order_count": len(orders),
                    "active_order_count": sum(self._active_order(row) for row in orders),
                    "terminal_omitted": max(0, len(orders) - len(projected))}

        mode = str(getattr(self.adapter, "mode", self._environment()))
        with self._accounting_cache_lock:
            for _attempt in range(3):
                before = revision_reader()
                cached = self._accounting_cache
                if cached and cached["mode"] == mode and cached["revision"] == before:
                    signature = self._accounting_metadata_signature(cached["spot_instruments"])
                    if signature is not None and signature == cached["metadata_signature"]:
                        return cached
                rebuilt = self._rebuild_accounting_snapshot(order_reader, execution_reader,
                    revision_reader, mode, before)
                if rebuilt is not None:
                    self._accounting_cache = rebuilt
                    return rebuilt
        raise BybitError("Order or execution ledger kept changing during accounting reconciliation")

    def _accounting_projection(self, orders, executions):
        """Build bounded display orders and per-owner recent fills for legacy adapters."""
        active = [row for row in orders if self._active_order(row)]
        terminal = [row for row in orders if not self._active_order(row)]
        terminal.sort(key=lambda row: (row.get("updated_at_ms", 0), row.get("created_at_ms", 0),
                                       str(row.get("order_link_id", ""))), reverse=True)
        projected = [*active, *terminal[:64]]
        order_owner = {row.get("order_link_id"): row.get("strategy_id") for row in orders if row.get("order_link_id")}
        order_owner.update({row.get("order_id"): row.get("strategy_id") for row in orders if row.get("order_id")})
        recent = {}
        for item in executions:
            if not isinstance(item, dict) or item.get("execType", "Trade") != "Trade":
                continue
            sid = order_owner.get(item.get("orderLinkId")) or order_owner.get(item.get("orderId"))
            if not sid:
                continue
            rows = recent.setdefault(sid, [])
            rows.append({**copy.deepcopy(item), "_strategy_id": sid})
            if len(rows) > 64:
                del rows[0]
        return projected, recent

    def _rebuild_accounting_snapshot(self, order_reader, execution_reader, revision_reader, mode, expected_revision):
        """Stream a stable ledger into owner lots without retaining history rows."""
        import heapq
        from collections import deque

        owners_by_link, owners_by_id = {}, {}
        owner_rows = []
        active_orders = []
        recent_terminal = []
        spot_instruments = set()
        total_orders = 0
        for row in order_reader(batch_size=1000):
            total_orders += 1
            if self._active_order(row):
                active_orders.append(dict(row))
            else:
                updated = row.get("updated_at_ms", 0)
                created = row.get("created_at_ms", 0)
                link = str(row.get("order_link_id", ""))
                key = (updated if type(updated) is int else 0,
                       created if type(created) is int else 0, link)
                item = dict(row)
                if len(recent_terminal) < 64:
                    heapq.heappush(recent_terminal, (*key, item))
                elif key > recent_terminal[0][:3]:
                    heapq.heapreplace(recent_terminal, (*key, item))

            strategy_id = row.get("strategy_id")
            link = row.get("order_link_id")
            order_id = row.get("order_id")
            if not strategy_id or not link:
                continue
            info = {"strategy_id": strategy_id, "category": row.get("category"),
                "symbol": row.get("symbol"), "side": row.get("side"),
                "reduce_only": row.get("reduce_only") is True,
                "order_link_id": link, "order_id": order_id,
                "created_at_ms": row.get("created_at_ms", 0),
                "filled_quantity": row.get("filled_quantity", 0.), "volume": 0.}
            owners_by_link[link] = info
            if order_id:
                owners_by_id[order_id] = info
            owner_rows.append(info)
            if row.get("category") == "spot" and row.get("symbol"):
                spot_instruments.add(("spot", row["symbol"]))

        starting_metadata_signature = self._accounting_metadata_signature(spot_instruments)
        metadata = {}
        for key in sorted(spot_instruments):
            try:
                value = self.adapter.metadata(*key)
                metadata[key] = (value.get("base_coin"), value.get("quote_coin"))
            except Exception:
                metadata[key] = None
        # Bind the identity to the exact values used below, not just a later
        # cache read. A refresh can change and revert during a long rebuild.
        used_metadata_signature = tuple((category, symbol, *(metadata[(category, symbol)] or (None, None)))
                                        for category, symbol in sorted(spot_instruments))

        lots, recent_fills = {}, {}
        incomplete = False
        fill_buffers = {}
        def normalized_rows():
            nonlocal incomplete
            for item in execution_reader(batch_size=1000):
                if not isinstance(item, dict) or item.get("execType", "Trade") != "Trade":
                    continue
                owner = owners_by_link.get(item.get("orderLinkId")) or owners_by_id.get(item.get("orderId"))
                if not owner:
                    continue
                try:
                    qty, price = float(item.get("execQty")), float(item.get("execPrice"))
                    timestamp_ms = int(item.get("execTime") or item.get("execTimeMs") or owner.get("created_at_ms", 0))
                except (TypeError, ValueError, OverflowError):
                    incomplete = True
                    continue
                if not _finite(qty) or qty <= 0 or not _finite(price) or price <= 0:
                    incomplete = True
                    continue
                owner["volume"] += qty
                yield timestamp_ms, item, owner, qty, price

        for timestamp_ms, item, owner, qty, price, ambiguous in _inventory_timeline(normalized_rows()):
            incomplete |= ambiguous
            category, symbol = owner.get("category"), owner.get("symbol")
            if category not in {"spot", "linear"} or not symbol:
                incomplete = True
                continue
            side = item.get("side", owner.get("side"))
            reduce_only = item.get("reduce_only") is True or owner.get("reduce_only") is True
            key = (category, symbol)
            base_fee, quote_fee = 0., 0.
            if category == "spot":
                fee_key = metadata.get(key)
                try:
                    fee = float(item.get("execFee", 0.) or 0.)
                    if not fee_key or not _finite(fee):
                        raise ValueError("Spot fee metadata is unavailable")
                    if item.get("feeCurrency") == fee_key[0]:
                        base_fee = fee
                    elif item.get("feeCurrency") == fee_key[1]:
                        quote_fee = fee
                    elif fee:
                        incomplete = True
                except (ValueError, TypeError, OverflowError):
                    incomplete = True
            current = lots.setdefault(key, {}).setdefault(owner["strategy_id"],
                {"quantity": 0., "cost": 0., "opened_at_ms": 0})
            if side == "Buy" and not reduce_only:
                if qty - base_fee <= 0:
                    incomplete = True
                    continue
                if current["quantity"] <= 1e-12:
                    current["opened_at_ms"] = timestamp_ms
                current["cost"] += qty * price + quote_fee
                current["quantity"] += qty - base_fee
            elif side == "Sell" and reduce_only:
                if qty + base_fee > current["quantity"] + 1e-8:
                    incomplete = True
                removed = min(max(0., qty + base_fee), current["quantity"])
                if current["quantity"] > 0:
                    current["cost"] *= max(0., 1. - removed / current["quantity"])
                current["quantity"] = max(0., current["quantity"] - removed)
                if current["quantity"] <= 1e-12:
                    current["cost"], current["opened_at_ms"] = 0., 0
            else:
                incomplete = True
                continue
            rows = fill_buffers.setdefault(owner["strategy_id"], deque(maxlen=64))
            rows.append({**copy.deepcopy(item), "_strategy_id": owner["strategy_id"]})

        for owner in owner_rows:
            filled = owner.get("filled_quantity")
            if _finite(filled) and filled > owner["volume"] + 1e-8:
                incomplete = True
        terminal_orders = [entry[3] for entry in sorted(recent_terminal, key=lambda item: item[:3], reverse=True)]
        projected_orders = [*active_orders, *terminal_orders]
        if total_orders > len(projected_orders):
            # This counter supports native UI disclosure; active orders are not
            # capped, and the full history remains in the adapter ledger.
            terminal_omitted = max(0, total_orders - len(active_orders) - len(terminal_orders))
        else:
            terminal_omitted = 0
        recent_fills = {sid: list(rows) for sid, rows in fill_buffers.items()}
        metadata_signature = self._accounting_metadata_signature(spot_instruments)
        after = revision_reader()
        if (expected_revision != after or metadata_signature is None or
                starting_metadata_signature != metadata_signature or
                used_metadata_signature != metadata_signature):
            return None
        return {"mode": mode, "revision": expected_revision,
            "orders": projected_orders, "executions": [item for rows in recent_fills.values() for item in rows],
            "recent_fills": recent_fills, "fills": lots, "incomplete": incomplete,
            "order_count": total_orders, "active_order_count": len(active_orders),
            "terminal_omitted": terminal_omitted,
            "spot_instruments": tuple(sorted(spot_instruments)),
            "metadata_signature": metadata_signature}

    @staticmethod
    def _summary_order(order):
        fields = ("order_link_id", "status", "strategy_id", "category", "symbol", "side", "order_type",
                  "quantity", "filled_quantity", "reference_price", "price", "reduce_only", "stop_loss",
                  "order_filter", "spot_protection", "parent_order_link_id", "expires_at_ms",
                  "average_price", "fees", "fee_currency", "fees_by_currency", "realized_pnl",
                  "unprotected_dust_quantity", "unprotected_dust_notional",
                  "protection_verified", "snapshot_id", "rationale", "created_at_ms", "updated_at_ms")
        result = {key: order[key] for key in fields if key in order}
        if isinstance(result.get("rationale"), str):
            result["rationale"] = _bounded_text(result["rationale"], 400)
        return result

    def _order_history_projection(self, orders, *, terminal_limit=64):
        """Bound history while retaining every active or unresolved order."""
        def recent_key(row):
            updated = row.get("updated_at_ms", 0)
            created = row.get("created_at_ms", 0)
            return (updated if type(updated) is int else 0,
                    created if type(created) is int else 0,
                    str(row.get("order_link_id", "")))

        active = [row for row in orders if self._active_order(row)]
        terminal = [row for row in orders if not self._active_order(row)]
        active.sort(key=recent_key, reverse=True)
        terminal.sort(key=recent_key, reverse=True)
        selected = [*active, *terminal[:terminal_limit]]
        return [self._summary_order(row) for row in selected]

    def _actual_spot_positions(self, raw, strategy_map, orders, executions, attributed=None):
        """Create portfolio rows from configured base-coin wallet balances.

        Bybit's account snapshot reports linear positions but spot inventory is
        in wallet balances. Unknown non-USDT balances make reconciliation
        incomplete instead of being silently ignored.
        """
        rows, incomplete = [], False
        currencies = {str(item.get("currency")): item for item in raw.get("currencies", [])
                      if isinstance(item, dict) and isinstance(item.get("currency"), str)}
        instruments = self._config()["bybit"].get("allowed_instruments", [])
        covered = {"USDT"}
        fills, attribution_incomplete = attributed or self._attributed_inventory(orders, executions)
        incomplete |= attribution_incomplete
        for instrument in instruments:
            if instrument.get("category") != "spot":
                continue
            category, symbol = "spot", instrument.get("symbol")
            try:
                metadata = self.adapter.metadata(category, symbol)
                base = metadata.get("base_coin")
                if not base:
                    raise ValueError("instrument metadata has no base coin")
                covered.add(base)
                wallet = currencies.get(base, {})
                actual = max(0., float(wallet.get("wallet_balance", 0.) or 0.) - float(wallet.get("spot_borrow", 0.) or 0.))
                if actual <= 0:
                    continue
                quote = self.adapter.tickers(category, symbol)[0]
                mark = float(quote.get("bid") or quote.get("last_price") or 0.)
                if not _finite(mark) or mark <= 0:
                    raise ValueError("no measured spot mark")
                owners = fills.get((category, symbol), {})
                owned_total = sum(max(0., item["quantity"]) for item in owners.values())
                if owned_total > actual + 1e-8:
                    incomplete = True
                    rows.append({"strategy_id": None, "category": category, "symbol": symbol,
                                 "quantity": actual, "mark_price": mark, "entry_price": mark,
                                 "ownership_uncertain": True})
                    continue
                for sid, lot in owners.items():
                    quantity = max(0., lot["quantity"])
                    if quantity > 1e-12:
                        known_owner = sid in strategy_map
                        incomplete |= not known_owner
                        rows.append({"strategy_id": sid if known_owner else None, "category": category, "symbol": symbol,
                                     "quantity": quantity, "mark_price": mark,
                                     "entry_price": lot["cost"] / quantity if quantity else mark,
                                     "opened_at_ms": lot.get("opened_at_ms")})
                foreign = max(0., actual - owned_total)
                if foreign > 1e-12:
                    rows.append({"strategy_id": None, "category": category, "symbol": symbol,
                                 "quantity": foreign, "mark_price": mark, "entry_price": mark})
            except Exception:
                incomplete = True
        for currency, row in currencies.items():
            if currency != "USDT" and currency not in covered and float(row.get("wallet_balance", 0.) or 0.) > 1e-12:
                incomplete = True
        return rows, incomplete

    def _attributed_inventory(self, orders, executions=None):
        """Rebuild long-only holdings from actual executions and known owners."""
        lots = {}
        owners = {order.get("order_link_id"): order for order in orders if order.get("order_link_id")}
        owners_by_order_id = {order.get("order_id"): order for order in orders if order.get("order_id")}
        if executions is None:
            executions = []
        volume_by_link = {}
        normalized_execs = []
        for item in executions:
            if not isinstance(item, dict):
                continue
            if item.get('execType', 'Trade') != 'Trade':
                continue  # Funding/settlement fees do not acquire base inventory.
            link = item.get("orderLinkId")
            owner = owners.get(link) or owners_by_order_id.get(item.get("orderId"))
            if not owner or not owner.get("strategy_id"):
                continue
            try:
                qty, price = float(item.get("execQty")), float(item.get("execPrice"))
                timestamp_ms = int(item.get("execTime") or item.get("execTimeMs") or owner.get("created_at_ms", 0))
            except (TypeError, ValueError, OverflowError):
                continue
            if not _finite(qty) or qty <= 0 or not _finite(price) or price <= 0:
                continue
            link = owner.get("order_link_id")
            volume_by_link[link] = volume_by_link.get(link, 0.) + qty
            normalized_execs.append((timestamp_ms, item, owner, qty, price))
        incomplete = any(_finite(order.get("filled_quantity")) and order.get("filled_quantity", 0.) >
            volume_by_link.get(order.get("order_link_id"), 0.) + 1e-8
            for order in orders if order.get("strategy_id"))
        for timestamp_ms, item, order, qty, price, ambiguous in _inventory_timeline(
                sorted(normalized_execs, key=lambda row: row[0])):
            incomplete |= ambiguous
            sid, category, symbol = order.get("strategy_id"), order.get("category"), order.get("symbol")
            if category not in {"spot", "linear"} or not symbol:
                incomplete = True
                continue
            side = item.get("side", order.get("side"))
            reduce_only = item.get("reduce_only") is True or order.get("reduce_only") is True
            key = (category, symbol)
            base_fee, quote_fee = 0., 0.
            if category == 'spot':
                try:
                    fee = float(item.get('execFee', 0.) or 0.)
                    metadata = self.adapter.metadata(category, symbol)
                    currency = item.get('feeCurrency')
                    if not _finite(fee):
                        raise ValueError('Invalid fee')
                    if currency == metadata['base_coin']:
                        base_fee = fee
                    elif currency == metadata['quote_coin']:
                        quote_fee = fee
                    elif fee:
                        incomplete = True  # Unclassified fee currency needs reconciliation.
                except (ValueError, KeyError, TypeError, OverflowError):
                    incomplete = True
            current = lots.setdefault(key, {}).setdefault(sid, {"quantity": 0., "cost": 0., "opened_at_ms": 0})
            if side == "Buy" and not reduce_only:
                if qty - base_fee <= 0:
                    incomplete = True
                    continue
                if current["quantity"] <= 1e-12:
                    current["opened_at_ms"] = timestamp_ms
                current["cost"] += qty * price + quote_fee
                current["quantity"] += qty - base_fee
            elif side == "Sell" and reduce_only:
                if qty + base_fee > current['quantity'] + 1e-8 or qty + base_fee < 0:
                    incomplete = True
                removed = min(max(0., qty + base_fee), current["quantity"])
                if current["quantity"] > 0:
                    current["cost"] *= max(0., 1. - removed / current["quantity"])
                current["quantity"] = max(0., current["quantity"] - removed)
                if current["quantity"] <= 1e-12:
                    current["cost"], current["opened_at_ms"] = 0., 0
            else:
                incomplete = True
        return lots, incomplete

    def _account_rows(self, raw, orders, executions, *, attributed=None):
        fills, incomplete = attributed or self._attributed_inventory(orders, executions)
        owners = {sid for instrument in fills.values() for sid, lot in instrument.items()
                  if lot.get('quantity', 0) > 1e-12}
        strategy_map = self._strategy_map(self.store, owners)
        positions = []
        for actual in raw.get("positions", []):
            if not isinstance(actual, dict):
                incomplete = True
                continue
            category, symbol = actual.get("category"), actual.get("symbol")
            qty, mark = actual.get("quantity"), actual.get("mark_price")
            if not _finite(qty) or not _finite(mark) or mark <= 0:
                if _finite(qty) and qty == 0:
                    continue
                incomplete = True
                continue
            if qty <= 0:
                continue
            if actual.get("side") == "Sell":
                incomplete = True
                positions.append({"strategy_id": None, "category": category, "symbol": symbol,
                                  "quantity": -qty, "mark_price": mark, "entry_price": actual.get("entry_price", mark),
                                  "leverage": actual.get("leverage"), "stop_loss": actual.get("stop_loss", 0.)})
                continue
            owners = fills.get((category, symbol), {})
            owned_total = sum(max(0., lot["quantity"]) for lot in owners.values())
            if owned_total > qty + 1e-8:
                incomplete = True
                positions.append({"strategy_id": None, "category": category, "symbol": symbol,
                                  "quantity": qty, "mark_price": mark, "entry_price": actual.get("entry_price", mark),
                                  "leverage": actual.get("leverage"), "stop_loss": actual.get("stop_loss", 0.),
                                  "ownership_uncertain": True})
                continue
            for sid, lot in owners.items():
                owned = max(0., lot["quantity"])
                if owned > 1e-12:
                    known_owner = sid in strategy_map
                    incomplete |= not known_owner
                    positions.append({"strategy_id": sid if known_owner else None, "category": category, "symbol": symbol,
                        "quantity": owned, "mark_price": mark,
                        "entry_price": lot["cost"] / owned if owned else mark,
                        "opened_at_ms": lot.get("opened_at_ms"),
                        "leverage": actual.get("leverage"), "stop_loss": actual.get("stop_loss", 0.)})
            foreign = max(0., qty - owned_total)
            if foreign > 1e-12:
                positions.append({"strategy_id": None, "category": category, "symbol": symbol,
                                  "quantity": foreign, "mark_price": mark,
                                  "entry_price": actual.get("entry_price", mark),
                                  "leverage": actual.get("leverage"), "stop_loss": actual.get("stop_loss", 0.)})
        # ``incomplete`` above includes attribution gaps; preserve them while
        # adding any account-side mark or spot-wallet gaps.
        spot, spot_incomplete = self._actual_spot_positions(raw, strategy_map, orders, executions,
            attributed=(fills, incomplete))
        positions.extend(spot)
        incomplete |= spot_incomplete
        return positions, incomplete

    def _persist_account(self, scope, raw, value):
        with self.store.connection() as db:
            db.execute("INSERT INTO execution_account_state(scope,environment,value,updated_at_ms) VALUES (?,?,?,?) "
                       "ON CONFLICT(scope) DO UPDATE SET environment=excluded.environment,value=excluded.value,updated_at_ms=excluded.updated_at_ms",
                       (scope, self._environment(), encoded(value), self._now_ms()))

    def _account_metrics(self, raw, positions, orders, incomplete):
        config = self._config()
        now_ms = self._now_ms()
        account_id = raw.get("account_id")
        expected_id = config["bybit"].get("account_id", "").strip()
        account_type = raw.get("account_type")
        reported_account_type = raw.get("raw_wallet", {}).get("accountType") if isinstance(raw.get("raw_wallet"), dict) else None
        binding_ok = (isinstance(account_id, str) and bool(account_id.strip()) and bool(expected_id) and
                      account_id == expected_id and reported_account_type == "UNIFIED" and
                      raw.get("mode") == self._environment() and account_type == "UNIFIED")
        scope = self._scope(account_id if binding_ok else "unbound")
        with self.store.connection() as db:
            row = db.execute("SELECT value FROM execution_account_state WHERE scope=?", (scope,)).fetchone()
        saved = json.loads(row[0]) if row else {}
        day = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
        day_start_ms = int(datetime.fromtimestamp(now_ms / 1000, timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        external_cashflow = False
        cashflow_state_known = True
        transaction_reader = getattr(self.adapter, "iter_transactions", None)
        if not callable(transaction_reader):
            transaction_reader = getattr(self.adapter, "transactions", None)
        if callable(transaction_reader):
            def scan_transaction_history():
                external = False
                known = True
                failed = False
                try:
                    for transaction in transaction_reader(since_ms=day_start_ms):
                        if not isinstance(transaction, dict):
                            continue
                        kind = str(transaction.get("type", "")).upper()
                        raw_flow = transaction.get("cash_flow", transaction.get("cashFlow", 0))
                        try:
                            flow = float(raw_flow or 0)
                        except (TypeError, ValueError, OverflowError):
                            known = False
                            break
                        if not math.isfinite(flow):
                            # Decimal source fields can be finite yet overflow
                            # a float. Even an internal transaction type cannot
                            # make invalid amounts verify a daily P&L baseline.
                            known = False
                            break
                        if any(token in kind for token in ("TRANSFER", "DEPOSIT", "WITHDRAW", "CASH_IN", "CASH_OUT")):
                            external = True
                            break
                        # Bybit's transaction log documents cashFlow for transfers
                        # as well as internal realized P&L. Unknown nonzero types
                        # are treated as external to avoid claiming an unadjusted
                        # equity delta as daily strategy P&L.
                        if flow and kind not in {"TRADE", "SETTLEMENT", "CLOSE", "DELIVERY", "LIQUIDATION", "ADL", "BLOCK_TRADE"}:
                            external = True
                            break
                except Exception:
                    known = False
                    failed = True
                return external, known, failed

            revision_reader = getattr(self.adapter, "transaction_revision", None)
            revision = None
            if callable(revision_reader):
                try:
                    revision = revision_reader()
                except Exception:
                    # A broken revision API disables caching for this read;
                    # retain the original full-reader fail-closed behavior.
                    revision = None
                if type(revision) is not int or revision < 0:
                    revision = None
            if revision is None:
                with self._cashflow_cache_lock:
                    self._cashflow_cache = None
                external_cashflow, cashflow_state_known, _failed = scan_transaction_history()
            else:
                mode = str(getattr(self.adapter, "mode", self._environment()))
                cache_key = (mode, self._environment(), scope, day, day_start_ms, revision)
                with self._cashflow_cache_lock:
                    cached = self._cashflow_cache
                    if cached is not None and cached[0] == cache_key:
                        external_cashflow, cashflow_state_known = cached[1], cached[2]
                    else:
                        self._cashflow_cache = None
                        for _attempt in range(3):
                            external_cashflow, cashflow_state_known, failed = scan_transaction_history()
                            try:
                                revision_after = revision_reader()
                            except Exception:
                                revision_after = None
                            if type(revision_after) is not int or revision_after < 0:
                                revision_after = None
                            if revision_after == revision:
                                if not failed:
                                    self._cashflow_cache = (cache_key, external_cashflow,
                                                            cashflow_state_known)
                                break
                            # A reconnect can append an ordinary trade while
                            # the complete day scan is running. Retry a bounded
                            # number of times before persisting an uncertainty
                            # halt; never accept the mixed-revision result.
                            if revision_after is None:
                                cashflow_state_known = False
                                break
                            revision = revision_after
                            cache_key = (*cache_key[:-1], revision)
                        else:
                            cashflow_state_known = False
        else:
            with self._cashflow_cache_lock:
                self._cashflow_cache = None
            cashflow_state_known = False
        equity = raw.get("equity")
        if not _finite(equity):
            equity = None
        saved_baseline = saved.get("daily_baseline_equity") if saved.get("day") == day else None
        if not _finite(saved_baseline) or saved_baseline <= 0:
            saved_baseline = None
        saved_peak = saved.get("peak_equity")
        if not _finite(saved_peak) or saved_peak <= 0:
            saved_peak = None
        if equity is not None:
            baseline = saved_baseline if saved_baseline is not None else equity
            peak = max(saved_peak if saved_peak is not None else equity, equity)
            daily_pnl = equity - baseline
            drawdown = max(0., 1. - equity / peak) if peak > 0 else None
        else:
            # An unavailable wallet read cannot erase a loss baseline or a
            # lifetime high-water mark. Retain valid checkpoints and let a
            # later verified reading recover without hiding intervening loss.
            baseline, peak, daily_pnl, drawdown = saved_baseline, saved_peak, None, None
        reasons = list(saved.get("halt_reasons", []))
        if external_cashflow or not cashflow_state_known:
            daily_pnl = None
            cashflow_reason = ("External account cash flow detected; daily P&L is not comparable" if external_cashflow
                               else "Account transaction history is unavailable; daily P&L is unverified")
            reasons = list(dict.fromkeys([*reasons, cashflow_reason]))
        account_for_checks = {"daily_pnl": daily_pnl, "drawdown_fraction": drawdown,
                              "risk_halted": bool(saved.get("risk_halted")) or external_cashflow or not cashflow_state_known}
        current_reasons = protective_reasons(account_for_checks, config)
        if current_reasons:
            reasons = list(dict.fromkeys([*reasons, *current_reasons]))
        halted = bool(saved.get("risk_halted")) or bool(current_reasons) or external_cashflow or not cashflow_state_known
        reconciliation_required = (bool(raw.get("reconciliation_required")) or incomplete or not binding_ok or
                                   equity is None or equity <= 0 or not cashflow_state_known)
        leverage = raw.get("leverage")
        leverage_by_instrument = raw.get("leverage_by_instrument")
        if not isinstance(leverage_by_instrument, dict):
            leverage_by_instrument = {}
        else:
            verified = {}
            for key, value in leverage_by_instrument.items():
                category, separator, symbol = key.partition(":") if isinstance(key, str) else ("", "", "")
                if (separator and category in {"spot", "linear"} and symbol and
                        _finite(value) and value > 0):
                    verified[key] = value
            leverage_by_instrument = verified
        if any(position.get("category") == "spot" for position in positions) and (not _finite(leverage) or leverage <= 0):
            # Bybit spot here is nonmargin base-coin only; risk_controls accepts
            # a verified cash leverage of one for this supported protocol.
            leverage = 1.
        account = {"venue": "bybit", "mode": raw.get("mode"), "account_type": account_type,
            "updated_at_ms": raw.get("updated_at_ms"), "reconciliation_required": reconciliation_required,
            "equity": equity, "available_balance": raw.get("available_balance"), "daily_pnl": daily_pnl,
            "drawdown_fraction": drawdown, "peak_equity": peak, "daily_baseline_equity": baseline,
            "day": day, "positions": positions, "leverage": leverage,
            "leverage_by_instrument": leverage_by_instrument,
            "position_mode": raw.get("position_mode"), "protection_supported": raw.get("protection_supported") is True,
            "protection_verified": raw.get("protection_verified") is True,
            "risk_halted": halted, "halt_reasons": reasons,
            "external_cashflow_detected": external_cashflow,
            "daily_pnl_verified": cashflow_state_known and not external_cashflow,
            "portfolio_notional": sum(abs(float(p["quantity"]) * float(p["mark_price"])) for p in positions),
            "account_binding_verified": binding_ok}
        saved.update(day=day, daily_baseline_equity=baseline, peak_equity=peak,
                     risk_halted=halted, halt_reasons=reasons,
                     last_equity=equity, last_account_update_ms=raw.get("updated_at_ms"))
        if binding_ok:
            self._persist_account(scope, raw, saved)
        return account, scope

    def _world(self):
        """Refresh adapter truth and derive risk-ready host state; caller locks mutations."""
        config = self._config()
        if not config["bybit"].get("enabled"):
            raise BybitError("Bybit is disabled by the operator")
        if self._environment() != config["bybit"].get("mode"):
            raise BybitError("Adapter environment changed; restart the paused runtime")
        result = self.adapter.reconcile()
        raw = result.get("account") if isinstance(result, dict) else None
        if not isinstance(raw, dict):
            raise BybitError("Bybit reconciliation returned no account")
        accounting = self._accounting_snapshot()
        orders = self._orders(accounting=accounting)
        executions = accounting["executions"]
        positions, incomplete = self._account_rows(raw, orders, executions,
            attributed=(accounting["fills"], accounting["incomplete"]))
        account, scope = self._account_metrics(raw, positions, orders, incomplete)
        return {"raw": raw, "account": account, "orders": orders, "positions": positions,
                "executions": executions, "recent_fills": accounting.get("recent_fills", {}),
                "ledger_revision": accounting.get("revision"),
                "order_count": accounting.get("order_count", len(orders)),
                "active_order_count": accounting.get("active_order_count",
                    sum(self._active_order(row) for row in orders)),
                "terminal_orders_omitted": accounting.get("terminal_omitted", 0),
                "scope": scope,
                "reconciliation_required": account["reconciliation_required"]}

    def _state_value(self, world):
        orders = self._order_history_projection(world["orders"])
        total_orders = world.get("order_count", len(world["orders"]))
        return {"environment": self._environment(), "account_scope": world.get('scope'),
                "updated_at_ms": self._now_ms(),
                "account": self._account_projection(world["account"]),
                "positions": copy.deepcopy(world["positions"]),
                "orders": orders, "order_count": total_orders,
                "active_order_count": world.get("active_order_count",
                    sum(self._active_order(row) for row in world["orders"])),
                "order_history_truncated": max(0, total_orders - len(orders)),
                "reconciliation_required": world["account"]["reconciliation_required"],
                "risk_halted": world["account"]["risk_halted"],
                "halt_reasons": list(world["account"].get("halt_reasons", []))}

    @staticmethod
    def _account_projection(account):
        keys = ("venue", "mode", "account_type", "updated_at_ms", "reconciliation_required", "equity",
                "available_balance", "daily_pnl", "daily_baseline_equity", "drawdown_fraction", "peak_equity", "day", "leverage",
                "position_mode", "protection_supported", "protection_verified", "risk_halted",
                "halt_reasons", "portfolio_notional", "account_binding_verified",
                "external_cashflow_detected", "daily_pnl_verified", "leverage_by_instrument")
        value = {key: account.get(key) for key in keys}
        return value

    def _record_memory(self, world, decision=None):
        orders = world["orders"]
        sol = is_sol_mode(self._config())
        selected = self.store.get_document('sol_mode_strategy_id') if sol else None
        if sol:
            if not isinstance(selected, str) or not selected:
                return
            # Other SOL versions are rejected by _strategy below. Filter before
            # history grouping and lookups so old owners do not add database
            # work to every account/protection poll.
            orders = [row for row in orders if row.get('strategy_id') == selected]
        link_owner = {row.get("order_link_id"): row.get("strategy_id") for row in orders if row.get("strategy_id")}
        order_owner = {row.get("order_id"): row.get("strategy_id") for row in orders if row.get("strategy_id")}
        fills_by_strategy = {}
        for item in world.get("executions", []):
            sid = (item.get("_strategy_id") or link_owner.get(item.get("orderLinkId")) or
                   order_owner.get(item.get("orderId")))
            if not sid or sol and sid != selected:
                continue
            safe = {key: item.get(key) for key in ("execId", "orderLinkId", "orderId", "category", "symbol",
                    "side", "execQty", "execPrice", "execFee", "feeCurrency", "execPnl", "execTime", "execType") if key in item}
            fills_by_strategy.setdefault(sid, []).append(safe)
        active_ids = set(fills_by_strategy)
        active_ids.update(row.get("strategy_id") for row in orders if row.get("strategy_id"))
        active_ids.update(row.get("strategy_id") for row in world["positions"] if row.get("strategy_id"))
        if decision and decision.get("strategy_id"):
            active_ids.add(decision["strategy_id"])
        if sol:
            active_ids.intersection_update({selected})
        for sid in active_ids:
            if not self._strategy(sid):
                continue
            selected_orders = self._order_history_projection(
                [row for row in orders if row.get("strategy_id") == sid])
            selected_positions = [copy.deepcopy(row) for row in world["positions"] if row.get("strategy_id") == sid]
            latest = copy.deepcopy(decision) if decision and decision.get("strategy_id") == sid else {
                "id": "reconcile-" + identity([self._environment(), sid, world["raw"].get("updated_at_ms")]),
                "strategy_id": sid, "status": "reconciled", "rationale": "Exchange account, orders, and fills reconciled."}
            try:
                self.memory.record_execution(sid, latest, positions=selected_positions,
                    orders=selected_orders, fills=fills_by_strategy.get(sid, []),
                    risk={key: copy.deepcopy(world['account'].get(key)) for key in
                          ('risk_halted', 'halt_reasons', 'reconciliation_required', 'protection_verified')})
            except (KeyError, ValueError):
                # ResearchMemory may not have a linked experiment for a legacy
                # strategy. Keep execution durable and do not fabricate one.
                continue

    def reconcile(self):
        """Refresh Bybit account/order/fill truth without invoking a model."""
        if not self._config()["bybit"].get("enabled"):
            return self.state()
        with self._mutation_lock:
            world = self._world()
            self.store.set_document("live_execution_state", self._state_value(world))
            self._record_memory(world)
            return copy.deepcopy(self._state_value(world))

    def _mutation_row(self, tool_id):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM execution_mutations WHERE tool_id=?", (tool_id,)).fetchone()
        return dict(row) if row else None

    def _mutation_begin(self, tool_id, action, args, order_link_id=None):
        digest = identity(args)
        now = self._now_ms()
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM execution_mutations WHERE tool_id=?", (tool_id,)).fetchone()
            if row:
                if row["action"] != action or row["args_hash"] != digest:
                    raise IntentRejected("Durable tool identity was reused for a different mutation")
                return dict(row), False
            db.execute("INSERT INTO execution_mutations VALUES (?,?,?,?,?,?,?,?)",
                       (tool_id, action, digest, "running", order_link_id, None, now, now))
        return self._mutation_row(tool_id), True

    def _mutation_finish(self, tool_id, result, status="completed"):
        with self.store.connection() as db:
            db.execute("UPDATE execution_mutations SET status=?,value=?,updated_at_ms=? WHERE tool_id=?",
                       (status, encoded(result), self._now_ms(), tool_id))
        return result

    def _mutation_existing(self, tool_id, action, args):
        row, created = self._mutation_begin(tool_id, action, args)
        if created:
            return None
        if row["status"] == "completed" and row.get("value"):
            return {"mutation_status": "completed", "result": json.loads(row["value"]), **row}
        return {"mutation_status": row["status"], **row}

    def _budget_available(self):
        try:
            return bool(self.agents and self.agents.budget.available("execution"))
        except Exception:
            return False

    @staticmethod
    def _review_world_binding(world):
        account = world.get("account", {})
        raw = world.get("raw", {})
        # Bind wallet quantities and host safety state. Equity, available margin,
        # daily P&L, and marks move with quotes; the full current values still go
        # to the reviewer and are revalidated by risk_controls immediately before
        # mutation. Wallet coin quantities catch actual balance changes.
        safety = {key: copy.deepcopy(account.get(key)) for key in (
            "venue", "mode", "account_type", "reconciliation_required", "risk_halted",
            "halt_reasons", "external_cashflow_detected", "daily_pnl_verified",
            "account_binding_verified", "position_mode", "leverage", "leverage_by_instrument", "protection_supported",
            "protection_verified")}
        currencies = [{key: copy.deepcopy(row.get(key)) for key in
            ("currency", "wallet_balance", "spot_borrow")} for row in raw.get("currencies", [])
            if isinstance(row, dict)]
        stable_positions = [{key: copy.deepcopy(row.get(key)) for key in (
            "strategy_id", "category", "symbol", "quantity", "entry_price", "leverage",
            "stop_loss", "ownership_uncertain")} for row in world.get("positions", [])]
        return {"account_safety": safety,
                "wallet_balances": {key: raw.get(key) for key in
                    ("usdt_wallet_balance", "usdt_available_balance")},
                "currencies": currencies, "positions": stable_positions,
                "orders": [ExecutionController._summary_order(row) for row in world.get("orders", [])],
                "ledger_revision": world.get("ledger_revision")}

    def _trade_review_binding(self, *, strategy_id, snapshot_id, world, config, permission):
        return identity({"mode": "sol_perpetual" if is_sol_mode(config) else "research",
            "environment": self._environment(), "strategy_id": strategy_id,
            "snapshot_id": snapshot_id, "permission": bool(permission),
            "bybit": {key: config["bybit"].get(key) for key in
                ("mode", "enabled", "mainnet_enabled", "allowed_categories", "allowed_instruments")},
            "live_execution": config["live_execution"],
            "world": self._review_world_binding(world),
            "eligibility": self._eligibility_projection(strategy_id),
            "constraints": self._constraints(strategy_id)})

    def _prepare_trade_review(self, name, args, tool_id, *, strategy_id, decision_snapshot,
                              decision_world=None, decision_config=None, decision_eligibility=None):
        """Call the reviewer with immutable measured evidence, outside the mutation lock."""
        config = self._config()
        if not is_sol_mode(config):
            return None
        reviewer = self.trade_reviewer
        review_task = "trade-review-" + identity(tool_id)
        base_evidence = {"tool_id": tool_id, "tool_name": name, "arguments": copy.deepcopy(args),
                         "strategy_id": strategy_id, "mode": "sol_perpetual"}
        def block(reason, evidence=None):
            return reviewer.record_block(evidence or base_evidence, task_id=review_task, reason=reason)
        try:
            if not self._permission_allowed():
                return block("Execution permission is paused")
            # Populate the read-only venue limit cache before both the reviewer
            # packet and the later locked recheck. Exposure-increasing host
            # validation still fetches and enforces the live venue ceiling.
            try:
                self._venue_leverage_limit()
            except Exception:
                pass
            # Refresh current state before presenting the exact proposal. This is
            # deliberately outside _mutation_lock; the locked check below detects
            # any state change while the model was reviewing.
            review_world = self._world()
            strategy = self._strategy(strategy_id)
            if not strategy:
                return block("Strategy version is unavailable")
            snapshot_id = args.get("snapshot_id")
            snapshot = self._approve_snapshot(strategy_id, snapshot_id, {snapshot_id})
            latest = self._fresh_market(strategy_id)
            reviewed_signal = snapshot.get("signal_input_id")
            latest_signal = latest.get("signal_input_id")
            reviewed_candles = snapshot.get("candles", [])
            latest_candles = latest.get("candles", [])
            reviewed_end = reviewed_candles[-1].get("end_ms") if reviewed_candles else None
            latest_end = latest_candles[-1].get("end_ms") if latest_candles else None
            if (reviewed_signal is not None and latest_signal != reviewed_signal) or (
                    reviewed_signal is None and (reviewed_end is None or latest_end != reviewed_end)):
                return block("A newer primary market observation superseded this proposal")
            if decision_world is not None:
                original_config = decision_config if isinstance(decision_config, dict) else config
                original_binding = self._trade_review_binding(strategy_id=strategy_id,
                    snapshot_id=snapshot_id, world=decision_world,
                    config=original_config, permission=True)
                current_binding = self._trade_review_binding(strategy_id=strategy_id,
                    snapshot_id=snapshot_id, world=review_world, config=config, permission=True)
                if (current_binding != original_binding or
                        (isinstance(decision_eligibility, dict) and
                         self._eligibility_projection(strategy_id) != decision_eligibility)):
                    return block("Account, orders, positions, config, or eligibility changed during Sol deliberation")
            now = self._now_ms()
            expiry = args.get("expires_at_ms")
            lifetime = min(config["live_execution"]["max_decision_age_seconds"],
                           strategy["market_policy"]["max_decision_latency_seconds"]) * 1000
            bounds = [snapshot["captured_at_ms"] + lifetime]
            attention = snapshot.get("attention_deadline_ms")
            if type(attention) is int:
                bounds.append(attention)
            if type(expiry) is int:
                bounds.append(expiry)
            valid_until = int(min(bounds))
            if type(expiry) is not int or now >= valid_until or now < snapshot["captured_at_ms"]:
                return block("Trade proposal snapshot or expiry is stale")
            binding = self._trade_review_binding(strategy_id=strategy_id, snapshot_id=snapshot_id,
                world=review_world, config=config, permission=True)
            evidence = {"tool_id": tool_id, "tool_name": name, "arguments": copy.deepcopy(args),
                "strategy_id": strategy_id, "mode": "sol_perpetual", "config": {"bybit": {
                    key: copy.deepcopy(config["bybit"].get(key)) for key in
                    ("mode", "enabled", "mainnet_enabled", "allowed_categories", "allowed_instruments")},
                    "live_execution": copy.deepcopy(config["live_execution"])},
                "strategy": self._strategy_projection(strategy),
                "decision_snapshot": self._market_projection(snapshot),
                "account": self._account_projection(review_world["account"]),
                "positions": copy.deepcopy(review_world["positions"]),
                "orders": self._order_history_projection(review_world["orders"]),
                "constraints": self._constraints(strategy_id),
                "eligibility": self._eligibility_projection(strategy_id),
                "runtime_permission": True, "state_binding": binding,
                "approval_expires_at_ms": valid_until}
            if callable(self.support_context_provider):
                try:
                    evidence["support_reports"] = self.support_context_provider(strategy_id, snapshot)
                except Exception:
                    evidence["support_reports"] = {"status": "unavailable", "reports": []}
            # One durable review slot per model tool identity. An exact replay
            # reuses the record; changed arguments or evidence hit the same slot
            # and are rejected without another paid request.
            result = reviewer.review(evidence, task_id=review_task, deadline_ms=valid_until)
            if not isinstance(result, dict):
                return {"status": "blocked", "reason": "Trade reviewer returned no decision"}
            result = copy.deepcopy(result)
            result.update(_approval={"state_binding": binding, "valid_until_ms": valid_until,
                                     "snapshot_id": snapshot_id, "tool_id": tool_id,
                                     "tool_name": name, "arguments_hash": identity(args),
                                     "proposal_hash": reviewer.proposal_hash(tool_id, name, args)})
            return result
        except Exception as error:
            return block("Trade review unavailable (" + type(error).__name__ + ")")

    def _verify_trade_approval(self, approval, name, args, tool_id, strategy_id):
        """Rebind the reviewer decision to live state under the mutation lock."""
        config = self._config()
        if not is_sol_mode(config):
            return  # Research mode retains its existing behavior.
        if not isinstance(approval, dict):
            raise IntentRejected("Dedicated SOL trade approval is missing")
        binding = approval.get("_approval") if isinstance(approval, dict) else None
        if (approval.get("status") != "approved" or not isinstance(binding, dict) or
                binding.get("tool_id") != tool_id or binding.get("tool_name") != name or
                binding.get("arguments_hash") != identity(args) or
                binding.get("proposal_hash") != self.trade_reviewer.proposal_hash(tool_id, name, args)):
            raise IntentRejected("Dedicated trade approval is missing or does not match this proposal")
        durable = self.trade_reviewer.approval_for_tool(tool_id, name, args) if self.trade_reviewer else None
        if not isinstance(durable, dict) or durable.get("review_id") != approval.get("review_id") or \
                durable.get("_approval") != binding:
            raise IntentRejected("Dedicated trade approval has no matching durable reviewer record")
        now = self._now_ms()
        if now >= binding.get("valid_until_ms", 0) or not self._permission_allowed():
            raise IntentRejected("Trade approval expired or execution permission changed")
        if not self.market.snapshot_status(binding["snapshot_id"]).get("valid"):
            raise IntentRejected("Reviewed market snapshot is no longer current")
        world = self._world()
        current = self._trade_review_binding(strategy_id=strategy_id, snapshot_id=binding["snapshot_id"],
            world=world, config=config, permission=True)
        if current != binding.get("state_binding"):
            raise IntentRejected("Account, orders, positions, config, or eligibility changed after trade review")
        # New primary observations supersede an approval even when the earlier
        # snapshot has not yet aged out of the market service's validity window.
        latest = self._fresh_market(strategy_id)
        reviewed = self.store.snapshot(binding["snapshot_id"])
        reviewed_value = reviewed.get("value", {}) if reviewed else {}
        if latest.get("signal_input_id") != reviewed_value.get("signal_input_id"):
            raise IntentRejected("A newer primary market observation superseded trade approval")

    def _eligibility(self, strategy_id):
        try:
            result = self.eligibility(strategy_id)
            return result if (isinstance(result, dict) and type(result.get('eligible')) is bool) else {
                "eligible": False, "reasons": ["Eligibility service returned invalid state"]}
        except Exception as error:
            return {"eligible": False, "reasons": ["Eligibility unavailable (" + type(error).__name__ + ")"]}

    def _eligibility_projection(self, strategy_id, result=None):
        result = result if isinstance(result, dict) else self._eligibility(strategy_id)
        keys = ("eligible", "status", "allocation", "policy_version", "observation_unit",
                "verified_observations", "sessions", "trades", "net_return", "drawdown",
                "benchmark_return", "cost_complete")
        projected = {key: result[key] for key in keys if key in result and
                     (isinstance(result[key], (str, int, float, bool)) or result[key] is None)}
        projected["reasons"] = [_bounded_text(item, 400) for item in result.get("reasons", [])[:12]]
        return projected

    def support_preflight(self, strategy_id):
        """Cheap, host-only support fanout gate for the continuous SOL worker.

        ``needed`` stays true whenever reconciliation or qualification is
        uncertain. It is false only when SOL is inactive, execution permission
        is definitively off, the strategy is unavailable, or the reconciled
        flat strategy cannot make a discretionary mutation.
        """
        config = self._config()
        if not is_sol_mode(config):
            return {"needed": False, "reason": "SOL Perpetual mode is not selected"}
        if not config["bybit"].get("enabled"):
            return {"needed": False, "reason": "Bybit is disabled by the operator"}
        if not config["live_execution"].get("enabled"):
            return {"needed": False, "reason": "Live execution is disabled by the operator"}
        if not self._permission_allowed():
            return {"needed": False, "reason": "Runtime permission is paused"}
        try:
            strategy = self._strategy(strategy_id)
        except Exception as error:
            return {"needed": True, "reason": "Strategy state is uncertain (" + type(error).__name__ + ")"}
        if not strategy:
            return {"needed": False, "reason": "Strategy version is unavailable"}
        try:
            world = self._world()
            eligibility = self._eligibility(strategy_id)
        except Exception as error:
            return {"needed": True, "reason": "Host state is uncertain (" + type(error).__name__ + ")"}
        try:
            no_action = self._flat_no_action_preflight(strategy_id, world, eligibility, config,
                                                       budget_available=self._budget_available())
        except Exception as error:
            return {"needed": True, "reason": "Host preflight is uncertain (" + type(error).__name__ + ")"}
        if no_action:
            return {"needed": False,
                "reason": "Host controls block discretionary mutations and no position or active order needs management"}
        return {"needed": True, "reason": "A discretionary decision may be actionable"}

    def _continuation_stop_reason(self, strategy_id, strategy, snapshot, config, eligibility):
        """Lightweight authorization and source freshness check between model rounds."""
        current = self._config()
        if not is_sol_mode(current):
            return "Authorization changed: SOL Perpetual mode is no longer selected"
        if not self._permission_allowed():
            return "Authorization changed: runtime permission is paused"
        if not current["bybit"].get("enabled") or not current["live_execution"].get("enabled"):
            return "Authorization changed: exchange or execution permission is disabled"
        if (current["bybit"].get("mode") != config["bybit"].get("mode") or
                current["bybit"].get("account_id") != config["bybit"].get("account_id") or
                current["bybit"].get("mainnet_enabled") != config["bybit"].get("mainnet_enabled") or
                current["bybit"].get("allowed_categories") != config["bybit"].get("allowed_categories") or
                current["bybit"].get("allowed_instruments") != config["bybit"].get("allowed_instruments") or
                current["live_execution"] != config["live_execution"]):
            return "Authorization changed: execution configuration changed during the decision"
        current_strategy = self._strategy(strategy_id)
        if not current_strategy or self._strategy_projection(current_strategy) != self._strategy_projection(strategy):
            return "Authorization changed: strategy version changed during the decision"
        current_eligibility = self._eligibility_projection(strategy_id)
        if current_eligibility != eligibility:
            return "Authorization changed: strategy eligibility changed during the decision"

        now = self._now_ms()
        captured = snapshot.get("captured_at_ms")
        if not isinstance(captured, int) or isinstance(captured, bool):
            return "Decision expired or superseded: source snapshot capture time is unavailable"
        lifetime = min(config["live_execution"]["max_decision_age_seconds"],
                       strategy["market_policy"]["max_decision_latency_seconds"]) * 1000
        deadline = captured + lifetime
        attention = snapshot.get("attention_deadline_ms")
        if type(attention) is int:
            deadline = min(deadline, attention)
        if now < captured or now >= deadline:
            return "Decision expired or superseded: decision deadline passed"
        try:
            status = self.market.snapshot_status(snapshot.get("snapshot_id"))
            if not isinstance(status, dict) or status.get("valid") is not True:
                return "Decision expired or superseded: source snapshot is no longer current"
            sampler = getattr(self.market, "sample", None)
            latest = sampler(strategy_id) if callable(sampler) else self._fresh_market(strategy_id)
            if not isinstance(latest, dict) or latest.get("strategy_id") != strategy_id:
                return "Decision expired or superseded: current market state is unavailable"
        except Exception:
            return "Decision expired or superseded: current market state is unavailable"
        reviewed_signal = snapshot.get("signal_input_id")
        if reviewed_signal is not None:
            if latest.get("signal_input_id") != reviewed_signal:
                return "Decision expired or superseded: a newer primary market observation arrived"
        else:
            old_bars, new_bars = snapshot.get("candles", []), latest.get("candles", [])
            if (not old_bars or not new_bars or
                    old_bars[-1].get("end_ms") != new_bars[-1].get("end_ms")):
                return "Decision expired or superseded: a newer primary market observation arrived"
        return None

    def _flat_no_action_preflight(self, strategy_id, world, eligibility, config, *, budget_available=None):
        """Whether host facts rule out every discretionary mutation for this event.

        This deliberately does not inspect the market signal: an otherwise
        qualified flat SOL strategy may still choose leverage. It only skips
        when a deterministic host eligibility, instrument, risk, or budget
        gate prevents discretionary mutation. Any owned inventory or live
        order keeps the model lane available for management.
        """
        if (not isinstance(eligibility, dict) or
                (eligibility.get("eligible") is not True and eligibility.get("eligible") is not False)):
            return False
        limits = config.get("live_execution", {})
        operator_approved = strategy_id in limits.get("eligible_strategy_versions", [])
        strategy = self._strategy(strategy_id)
        policy = strategy.get("market_policy", {}) if strategy else {}
        instrument_allowed = (policy.get("category") in config.get("bybit", {}).get("allowed_categories", []) and
            {"category": policy.get("category"), "symbol": policy.get("symbol")} in
                config.get("bybit", {}).get("allowed_instruments", []))
        account = world.get("account", {})
        try:
            risk_blocked = (limits.get("kill_switch") is True or account.get("risk_halted") is True or
                account.get("daily_pnl") is None or account.get("drawdown_fraction") is None or
                bool(protective_reasons(account, config)))
        except Exception:
            return False
        hard_gate_blocks = (eligibility["eligible"] is not True or not operator_approved or
            not instrument_allowed or risk_blocked or budget_available is False)
        if not hard_gate_blocks:
            return False
        reasons = eligibility.get("reasons", [])
        if eligibility["eligible"] is False and any(
               isinstance(reason, str) and reason.startswith(("Eligibility unavailable",
                   "Eligibility service returned invalid state"))
               for reason in reasons):
            return False
        if world.get("account", {}).get("reconciliation_required") is True:
            return False
        if any(row.get("strategy_id") == strategy_id and
               (not _finite(row.get("quantity")) or abs(row.get("quantity")) > 1e-12)
               for row in world.get("positions", [])):
            return False
        if any(row.get("strategy_id") == strategy_id and self._active_order(row)
               for row in world.get("orders", [])):
            return False
        return True

    @staticmethod
    def _active_order(order):
        return order.get("status") in ACTIVE_ORDERS

    def _risk_check(self, intent, *, strategy, decision_snapshot, current_market, world, orders,
                    eligibility, budget_available):
        now = self._now_ms()
        counter = getattr(self.adapter, 'recent_order_count', None)
        recent = (counter(since_ms=max(0, now - 60_000)) if callable(counter) else
                  sum(1 for order in world["orders"] if
                      type(order.get("created_at_ms")) is int and order["created_at_ms"] >= now - 60_000))
        config = self._config()
        if is_sol_mode(config) and intent.get('reduce_only') is not True:
            config = copy.deepcopy(config)
            config['live_execution']['max_leverage'] = self._venue_leverage_limit()
        checked = validate_intent(intent, strategy=strategy, decision_snapshot=decision_snapshot,
            market=current_market, account=world["account"], orders=orders,
            eligibility=eligibility, config=config, now_ms=now,
            budget_available=budget_available, recent_order_count=recent)
        if is_sol_mode(config) and intent.get('reduce_only') is not True:
            equity = world['account']['equity']
            stop = config['live_execution']['stop_loss_fraction']
            fee_fraction = max(6., config['risk']['fee_bps']) / 10000
            slippage = config['live_execution']['max_slippage_bps'] / 10000
            # Bound modeled dollar loss on total instrument exposure, including
            # outstanding buys, rather than treating leverage as extra equity.
            exposure = sum(abs(float(p.get('quantity', 0)) * float(p['mark_price']))
                for p in world['positions'] if p.get('category') == 'linear' and p.get('symbol') == 'SOLUSDT')
            pending = sum(max(0., float(o.get('quantity', 0)) - float(o.get('filled_quantity', 0))) *
                float(o.get('price', o.get('reference_price', 0))) for o in orders
                if o.get('status') in ACTIVE_ORDERS and o.get('category') == 'linear' and
                   o.get('symbol') == 'SOLUSDT' and not o.get('reduce_only'))
            modeled_risk = (checked['checked_notional'] + exposure + pending) * (stop + 2 * fee_fraction + slippage)
            if modeled_risk > equity * mode_settings(config)['max_position_risk_fraction']:
                raise IntentRejected('SOL mode modeled position stop-risk budget exceeded')
        return checked

    def _venue_leverage_limit(self, *, cached=False):
        metadata = None
        if cached:
            with self.store.connection() as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bybit_instruments'").fetchone()
                row = db.execute('SELECT value,updated_at_ms FROM bybit_instruments WHERE mode=? AND category=? AND symbol=?',
                    (getattr(self.adapter, 'metadata_mode', self._environment()), 'linear', 'SOLUSDT')).fetchone() if exists else None
            if row and 0 <= self._now_ms() - row['updated_at_ms'] <= 60_000:
                metadata = json.loads(row['value'])
        else:
            metadata = self.adapter.metadata('linear', 'SOLUSDT')
        try:
            limit = float(metadata.get('max_leverage')) if metadata else None
        except (ValueError, TypeError):
            limit = None
        if limit is None or not math.isfinite(limit) or limit < 1:
            if cached:
                return None
            raise IntentRejected('The current SOLUSDT venue leverage limit is unavailable')
        return limit

    def _set_leverage(self, args, tool_id, *, strategy_id, approved_snapshots, trade_approval=None):
        """A journalled, flat-only leverage choice, separate from order authority."""
        if not is_sol_mode(self._config()) or args.get('strategy_id') != strategy_id:
            raise IntentRejected('Agent leverage selection is scoped to SOL Perpetual mode')
        if not self._permission_allowed():
            raise IntentRejected('Pause blocks agent leverage changes')
        with self._mutation_lock:
            config = self._config()
            strategy = self._strategy(strategy_id)
            if not strategy or not config['bybit']['enabled'] or not config['live_execution']['enabled']:
                raise IntentRejected('Explicit exchange execution permissions are required for leverage changes')
            if {'category': 'linear', 'symbol': 'SOLUSDT'} not in config['bybit']['allowed_instruments']:
                raise IntentRejected('SOLUSDT linear must be explicitly allowed before leverage changes')
            snapshot = self._approve_snapshot(strategy_id, args.get('snapshot_id'), approved_snapshots)
            now = self._now_ms()
            lifetime = min(config['live_execution']['max_decision_age_seconds'],
                           strategy['market_policy']['max_decision_latency_seconds']) * 1000
            expiry = args.get('expires_at_ms')
            if (type(expiry) is not int or not now < expiry <= snapshot['captured_at_ms'] + lifetime or
                    not 0 <= now - snapshot['captured_at_ms'] <= lifetime):
                raise IntentRejected('Leverage decision is stale or expired')
            requested = args.get('leverage')
            if not _finite(requested) or not 1 <= requested <= self._venue_leverage_limit():
                raise IntentRejected('Requested leverage exceeds the verified venue ceiling')
            if not isinstance(args.get('rationale'), str) or not args['rationale'].strip():
                raise IntentRejected('A leverage rationale is required')
            eligibility = self._eligibility(strategy_id)
            if eligibility.get('eligible') is not True or strategy_id not in config['live_execution']['eligible_strategy_versions']:
                raise IntentRejected('Qualified strategy and explicit operator approval are required')
            if not self._budget_available():
                raise IntentRejected('Model budget blocks discretionary leverage changes')
            world = self._world()
            account = world['account']
            if (account.get('reconciliation_required', True) or config['live_execution']['kill_switch'] or
                    account.get('daily_pnl') is None or account.get('drawdown_fraction') is None or
                    protective_reasons(account, config) or not 0 <= now - account['updated_at_ms'] <=
                    config['live_execution']['max_account_age_seconds'] * 1000):
                raise IntentRejected('Fresh reconciled account and clear risk state are required')
            if (any(p.get('category') == 'linear' and p.get('symbol') == 'SOLUSDT' and
                    abs(float(p.get('quantity', 0))) > 0 for p in world['positions']) or
                    any(o.get('category') == 'linear' and o.get('symbol') == 'SOLUSDT' and
                        self._active_order(o) for o in world['orders'])):
                raise IntentRejected('SOLUSDT must be flat with no unresolved orders before leverage changes')
            previous = self._mutation_existing(tool_id, 'set_leverage', args)
            if isinstance(previous, dict):
                if previous.get('mutation_status') == 'completed':
                    return previous['result']
                return {'status': 'unknown', 'reason': 'Prior leverage mutation requires read-only reconciliation; it is not replayed'}
            if not self._permission_allowed() or self._config() != config or self._now_ms() >= expiry:
                raise IntentRejected('Execution permission changed before leverage mutation')
            self._verify_trade_approval(trade_approval, 'set_leverage', args, tool_id, strategy_id)
            try:
                result = self.adapter.set_leverage('linear', 'SOLUSDT', requested)
            except Exception as error:
                return self._mutation_finish(tool_id, {'status': 'unknown',
                    'reason': 'Leverage readback required (' + type(error).__name__ + ')'}, 'uncertain')
            if result.get('status') != 'verified' or result.get('leverage') != requested:
                return self._mutation_finish(tool_id, {'status': 'unknown',
                    'reason': 'Exchange leverage was not verified'}, 'uncertain')
            result.update(rationale=args['rationale'], expires_at_ms=expiry)
            self.store.set_document('live_execution_state', self._state_value(self._world()))
            return self._mutation_finish(tool_id, result)

    def _check_order(self, link_id, strategy_id, orders, *, allow_expired=False, allow_terminal=False):
        if not isinstance(link_id, str) or not link_id:
            raise IntentRejected("A durable order link ID is required")
        order = next((row for row in orders if row.get("order_link_id") == link_id), None)
        if not order:
            raise IntentRejected("Order is not in the durable Prism ledger")
        if order.get("strategy_id") != strategy_id:
            raise IntentRejected("Order is not owned by this strategy version")
        if not self._active_order(order) and not allow_terminal:
            raise IntentRejected("Order is already terminal")
        if not allow_expired and order.get("expires_at_ms") and self._now_ms() >= order["expires_at_ms"]:
            raise IntentRejected("Order intent has expired")
        return order

    def _order_result(self, order, *, acknowledgement=True):
        result = self._summary_order(order)
        if acknowledgement:
            result["acknowledgement_is_not_fill"] = True
        return result

    def _amend_applied(self, order, args):
        if not order:
            return False
        try:
            price = args.get("limit_price") if args.get("limit_price") is not None else order.get("price")
            normalized_price = float(self.adapter._normalize_price(order["category"], order["symbol"], price, order["side"]))
            normalized_quantity = float(self.adapter.normalize_quantity(order["category"], order["symbol"],
                args["quantity"], price=normalized_price, side=order["side"])["quantity"])
            return (math.isclose(float(order.get("price", 0)), normalized_price, rel_tol=1e-9, abs_tol=1e-9)
                and math.isclose(float(order.get("quantity", 0)), normalized_quantity, rel_tol=1e-9, abs_tol=1e-9))
        except Exception:
            return False

    def _uncertain_cancel(self, order, link, error_name=None):
        if order and order.get("status") in _TERMINAL:
            return self._order_result(order)
        result = {"status": "unknown", "order_link_id": link,
                  "observed_status": order.get("status") if order else None,
                  "acknowledgement_is_not_fill": True}
        if error_name:
            result["reason"] = error_name
        return result

    def _submit_intent(self, name, args, tool_id, *, strategy_id, approved_snapshots, trade_approval=None):
        if args.get("strategy_id") != strategy_id:
            raise IntentRejected("Tool is scoped to the event strategy version")
        if not self._permission_allowed():
            raise IntentRejected("Runtime permission is paused; new exposure is blocked")
        link_id = _order_link(tool_id)
        previous = self._mutation_existing(tool_id, name, args)
        if isinstance(previous, dict):
            if previous.get("mutation_status") == "completed":
                return previous["result"]
            # A prior submit uses a stable orderLinkId. Query the same durable
            # identity; BybitAdapter itself never replays an existing create.
            try:
                order = self.adapter.query_order(link_id, reconcile=True)
            except Exception as error:
                return self._mutation_finish(tool_id, {"status": "unknown", "reason": type(error).__name__,
                    "order_link_id": link_id, "acknowledgement_is_not_fill": True}, "uncertain")
            if order:
                result = self._order_result(order)
                result.update(rationale=args.get("rationale"), expires_at_ms=args.get("expires_at_ms"),
                              decision_snapshot_id=args.get("snapshot_id"))
                return self._mutation_finish(tool_id, result)
            # The adapter has no durable local intent, so no create reached its
            # transport. Stable link identity still makes the next call safe.
        with self._mutation_lock:
            config = self._config()
            if not config["bybit"].get("enabled"):
                raise IntentRejected("Bybit is disabled by the operator")
            if not self._permission_allowed():
                raise IntentRejected("Runtime permission changed before submission")
            strategy = self._strategy(strategy_id)
            if not strategy:
                raise IntentRejected("Strategy version is no longer available")
            decision_snapshot = self._approve_snapshot(strategy_id, args.get("snapshot_id"), approved_snapshots)
            current_market = self._fresh_market(strategy_id)
            world = self._world()
            intent = {**args, "order_type": args.get("order_type"), "limit_price": args.get("limit_price")}
            eligibility = self._eligibility(strategy_id)
            approved = self._risk_check(intent, strategy=strategy, decision_snapshot=decision_snapshot,
                current_market=current_market, world=world, orders=world["orders"],
                eligibility=eligibility, budget_available=self._budget_available())
            if not self._permission_allowed():
                raise IntentRejected("Runtime permission changed immediately before submission")
            if self._config() != config:
                raise IntentRejected("Execution configuration changed during submission validation")
            self._verify_trade_approval(trade_approval, name, args, tool_id, strategy_id)
            payload = {"category": strategy["market_policy"]["category"], "symbol": strategy["market_policy"]["symbol"],
                "side": approved["side"], "order_type": approved["order_type"], "quantity": approved["quantity"],
                "price": approved["limit_price"], "reduce_only": approved["reduce_only"],
                "order_link_id": link_id, "strategy_id": strategy_id,
                "snapshot_id": approved["snapshot_id"], "rationale": approved["rationale"],
                "expires_at_ms": approved["expires_at_ms"]}
            if approved["reduce_only"]:
                payload["stop_loss"] = None
            elif payload["category"] == "linear":
                payload["stop_loss"] = float(current_market["quote"]["bid"]) * (1. - float(config["live_execution"]["stop_loss_fraction"]))
            # Persist the host-enforced GTC deadline before transport. If the
            # create is accepted and its acknowledgement is lost, protection
            # still knows the intent's expiry and rationale.
            with self.store.connection() as db:
                db.execute("INSERT OR REPLACE INTO execution_order_controls VALUES (?,?,?,?)",
                           (link_id, approved["expires_at_ms"], approved["rationale"], self._now_ms()))
            try:
                order = self.adapter.submit_order(payload)
            except OrderOutcomeUnknown:
                try:
                    order = self.adapter.reconcile_order(link_id)
                except Exception:
                    order = None
                result = self._order_result(order) if order else {"order_link_id": link_id, "status": "unknown",
                    "filled_quantity": 0., "acknowledgement_is_not_fill": True}
                result.update(rationale=approved["rationale"], expires_at_ms=approved["expires_at_ms"],
                              decision_snapshot_id=approved["snapshot_id"])
                return self._mutation_finish(tool_id, result, "completed" if order else "uncertain")
            if not order:
                result = {"order_link_id": link_id, "status": "unknown", "filled_quantity": 0.,
                          "acknowledgement_is_not_fill": True, "rationale": approved["rationale"],
                          "expires_at_ms": approved["expires_at_ms"], "snapshot_id": approved["snapshot_id"]}
            else:
                result = self._order_result(order)
                result["rationale"] = approved["rationale"]
                result["expires_at_ms"] = approved["expires_at_ms"]
                result["decision_snapshot_id"] = approved["snapshot_id"]
            self.store.set_document("live_execution_state", self._state_value(world))
            return self._mutation_finish(tool_id, result, "completed" if result["status"] != "unknown" else "uncertain")

    def _amend_order(self, args, tool_id, *, strategy_id, approved_snapshots, trade_approval=None):
        if not self._permission_allowed():
            raise IntentRejected("Runtime permission is paused; order amendment is blocked")
        existing_mutation = self._mutation_existing(tool_id, "amend_order", args)
        if isinstance(existing_mutation, dict):
            if existing_mutation.get("mutation_status") == "completed":
                return existing_mutation["result"]
            link = existing_mutation.get("order_link_id") or args.get("order_link_id")
            try:
                order = self.adapter.reconcile_order(link)
            except Exception as error:
                return self._mutation_finish(tool_id, {"status": "unknown", "order_link_id": link,
                    "reason": type(error).__name__, "acknowledgement_is_not_fill": True}, "uncertain")
            if self._amend_applied(order, args):
                with self.store.connection() as db:
                    db.execute("INSERT OR REPLACE INTO execution_order_controls VALUES (?,?,?,?)",
                               (link, args["expires_at_ms"], args["rationale"], self._now_ms()))
                result = self._order_result(order)
                result.update(rationale=args["rationale"], expires_at_ms=args["expires_at_ms"],
                              decision_snapshot_id=args["snapshot_id"])
                return self._mutation_finish(tool_id, result)
            return self._mutation_finish(tool_id, {"status": "unknown", "order_link_id": link,
                "reason": "Amend outcome is ambiguous; the same mutation will not be replayed",
                "acknowledgement_is_not_fill": True}, "uncertain")
        with self._mutation_lock:
            config = self._config()
            if not self._permission_allowed():
                raise IntentRejected("Runtime permission changed before amendment")
            strategy = self._strategy(strategy_id)
            if not strategy:
                raise IntentRejected("Strategy version is no longer available")
            world = self._world()
            order = self._check_order(args.get("order_link_id"), strategy_id, world["orders"])
            if order.get("order_type") != "Limit" or order.get("reduce_only"):
                raise IntentRejected("Only strategy owned opening limit orders can be amended")
            decision_snapshot = self._approve_snapshot(strategy_id, args.get("snapshot_id"), approved_snapshots)
            current_market = self._fresh_market(strategy_id)
            price = args.get("limit_price") if args.get("limit_price") is not None else order.get("price")
            intent = {"strategy_id": strategy_id, "snapshot_id": args["snapshot_id"], "side": order["side"],
                "order_type": "Limit", "quantity": args["quantity"], "limit_price": price,
                "reduce_only": False, "expires_at_ms": args["expires_at_ms"], "rationale": args["rationale"]}
            other_orders = [row for row in world["orders"] if row.get("order_link_id") != order["order_link_id"]]
            eligibility = self._eligibility(strategy_id)
            approved = self._risk_check(intent, strategy=strategy, decision_snapshot=decision_snapshot,
                current_market=current_market, world=world, orders=other_orders,
                eligibility=eligibility, budget_available=self._budget_available())
            if not self._permission_allowed():
                raise IntentRejected("Runtime permission changed immediately before amendment")
            if self._config() != config:
                raise IntentRejected("Execution configuration changed during amendment validation")
            self._verify_trade_approval(trade_approval, "amend_order", args, tool_id, strategy_id)
            old_expiry = order.get("expires_at_ms") or approved["expires_at_ms"]
            # Until the venue result is known, an uncertain amendment keeps the
            # earlier local deadline. A completed amendment may extend it.
            with self.store.connection() as db:
                db.execute("INSERT OR REPLACE INTO execution_order_controls VALUES (?,?,?,?)",
                           (order["order_link_id"], min(old_expiry, approved["expires_at_ms"]),
                            approved["rationale"], self._now_ms()))
            try:
                changed = self.adapter.amend_order(order["order_link_id"], price=approved["limit_price"],
                    quantity=approved["quantity"])
            except OrderOutcomeUnknown:
                try:
                    changed = self.adapter.reconcile_order(order["order_link_id"])
                except Exception:
                    changed = None
                if self._amend_applied(changed, args):
                    with self.store.connection() as db:
                        db.execute("INSERT OR REPLACE INTO execution_order_controls VALUES (?,?,?,?)",
                            (order["order_link_id"], approved["expires_at_ms"], approved["rationale"], self._now_ms()))
                    result = self._order_result(changed)
                    result.update(rationale=approved["rationale"], expires_at_ms=approved["expires_at_ms"],
                                  decision_snapshot_id=approved["snapshot_id"])
                    return self._mutation_finish(tool_id, result)
                result = {"status": "unknown", "order_link_id": order["order_link_id"],
                    "observed_status": changed.get("status") if changed else None,
                    "acknowledgement_is_not_fill": True, "rationale": approved["rationale"],
                    "expires_at_ms": approved["expires_at_ms"],
                    "decision_snapshot_id": approved["snapshot_id"]}
                return self._mutation_finish(tool_id, result, "uncertain")
            with self.store.connection() as db:
                db.execute("INSERT OR REPLACE INTO execution_order_controls VALUES (?,?,?,?)",
                           (order["order_link_id"], approved["expires_at_ms"], approved["rationale"], self._now_ms()))
            if changed is None:
                changed = next((row for row in self._orders() if row.get("order_link_id") == order["order_link_id"]), order)
            result = self._order_result(changed)
            result.update(rationale=approved["rationale"], expires_at_ms=approved["expires_at_ms"],
                          decision_snapshot_id=approved["snapshot_id"])
            return self._mutation_finish(tool_id, result)

    def _cancel_order(self, args, tool_id, *, strategy_id):
        previous = self._mutation_existing(tool_id, "cancel_order", args)
        if isinstance(previous, dict):
            if previous.get("mutation_status") == "completed":
                return previous["result"]
            link = args["order_link_id"]
            try:
                order = self.adapter.reconcile_order(link)
            except Exception as error:
                result = self._uncertain_cancel(None, link, type(error).__name__)
                result["decision_rationale"] = args["rationale"]
                return self._mutation_finish(tool_id, result, "uncertain")
            result = self._uncertain_cancel(order, link)
            result["decision_rationale"] = args["rationale"]
            result["expires_at_ms"] = order.get("expires_at_ms") if order else None
            result["snapshot_id"] = order.get("snapshot_id") if order else None
            return self._mutation_finish(tool_id, result,
                "completed" if result["status"] != "unknown" else "uncertain")
        with self._mutation_lock:
            world = self._world()
            order = self._check_order(args.get("order_link_id"), strategy_id, world["orders"],
                                      allow_expired=True, allow_terminal=True)
            if order.get("spot_protection") is True:
                raise IntentRejected("Spot exchange protection is host managed; request a reduction to exit safely")
            try:
                result = self.adapter.cancel_order(order["order_link_id"])
            except OrderOutcomeUnknown:
                try:
                    result = self.adapter.reconcile_order(order["order_link_id"])
                except Exception:
                    result = None
                value = self._uncertain_cancel(result, order["order_link_id"])
                value["decision_rationale"] = args["rationale"]
                value["expires_at_ms"] = order.get("expires_at_ms")
                value["snapshot_id"] = order.get("snapshot_id")
                return self._mutation_finish(tool_id, value,
                    "completed" if value["status"] != "unknown" else "uncertain")
            if not result:
                result = {"status": "unknown", "order_link_id": order["order_link_id"]}
            if "order_link_id" not in result:
                result = {**result, "order_link_id": order["order_link_id"]}
            result["decision_rationale"] = args["rationale"]
            result["expires_at_ms"] = order.get("expires_at_ms")
            result["snapshot_id"] = order.get("snapshot_id")
            result["acknowledgement_is_not_fill"] = True
            return self._mutation_finish(tool_id, result,
                "uncertain" if result.get("status") == "unknown" else "completed")

    def _reduce_exposure(self, args, tool_id, *, strategy_id, approved_snapshots, trade_approval=None):
        if args.get("strategy_id") != strategy_id:
            raise IntentRejected("Tool is scoped to the event strategy version")
        previous = self._mutation_existing(tool_id, "reduce_exposure", args)
        link = _order_link(tool_id)
        if isinstance(previous, dict):
            if previous.get("mutation_status") == "completed":
                return previous["result"]
            try:
                order = self.adapter.query_order(link, reconcile=True)
            except Exception as error:
                return self._mutation_finish(tool_id, {"status": "unknown", "order_link_id": link,
                    "reason": type(error).__name__, "acknowledgement_is_not_fill": True}, "uncertain")
            if order:
                result = self._order_result(order)
                result.update(rationale=args.get("rationale"), expires_at_ms=args.get("expires_at_ms"),
                              decision_snapshot_id=args.get("snapshot_id"))
                return self._mutation_finish(tool_id, result)
        with self._mutation_lock:
            config = self._config()
            if not config["bybit"].get("enabled"):
                raise IntentRejected("Bybit is disabled by the operator")
            strategy = self._strategy(strategy_id)
            if not strategy:
                raise IntentRejected("Strategy version is no longer available")
            decision_snapshot = self._approve_snapshot(strategy_id, args.get("snapshot_id"), approved_snapshots)
            world = self._world()
            policy = strategy.get("market_policy", {})
            if policy.get("category") == "spot":
                if self._foreign_spot_sell_conflicts(strategy_id, policy["symbol"], world["orders"]):
                    return self._mutation_finish(tool_id, {"status": "foreign_sell_conflict",
                        "reason": "A foreign Spot sell order conflicts with this reserved inventory",
                        "ordinary_sell_submitted": False}, "uncertain")
                safe, cancellations = self._cancel_spot_protections(strategy_id, policy["symbol"],
                                                                      orders=world["orders"])
                if not safe:
                    return self._mutation_finish(tool_id, {"status": "cancel_unconfirmed",
                        "reason": "Spot conditional cancellation or fill reconciliation is incomplete",
                        "cancellations": cancellations, "ordinary_sell_submitted": False}, "uncertain")
                world = self._world()
                owned_now = [row for row in world["positions"] if row.get("strategy_id") == strategy_id and
                             row.get("category") == "spot" and row.get("symbol") == policy["symbol"]]
                if not owned_now:
                    return self._mutation_finish(tool_id, {"status": "position_reconciled",
                        "reason": "Spot exit filled while its stop was being cancelled",
                        "ordinary_sell_submitted": False})
            current_market = self._fresh_market(strategy_id)
            intent = {"strategy_id": strategy_id, "snapshot_id": args["snapshot_id"], "side": "Sell",
                "order_type": "Market", "quantity": args["quantity"], "limit_price": None,
                "reduce_only": True, "expires_at_ms": args["expires_at_ms"], "rationale": args["rationale"]}
            approved = self._risk_check(intent, strategy=strategy, decision_snapshot=decision_snapshot,
                current_market=current_market, world=world, orders=world["orders"],
                eligibility={"eligible": False}, budget_available=self._budget_available())
            self._verify_trade_approval(trade_approval, "reduce_exposure", args, tool_id, strategy_id)
            with self.store.connection() as db:
                db.execute("INSERT OR REPLACE INTO execution_order_controls VALUES (?,?,?,?)",
                    (link, approved["expires_at_ms"], approved["rationale"], self._now_ms()))
            payload = {"category": strategy["market_policy"]["category"], "symbol": strategy["market_policy"]["symbol"],
                "side": "Sell", "order_type": "Market", "quantity": approved["quantity"], "price": None,
                "reduce_only": True, "order_link_id": link, "strategy_id": strategy_id,
                "snapshot_id": approved["snapshot_id"], "rationale": approved["rationale"],
                "expires_at_ms": approved["expires_at_ms"]}
            try:
                order = self.adapter.submit_order(payload)
            except OrderOutcomeUnknown:
                try:
                    order = self.adapter.reconcile_order(link)
                except Exception:
                    order = None
            result = self._order_result(order) if order else {"status": "unknown", "order_link_id": link,
                "filled_quantity": 0., "acknowledgement_is_not_fill": True,
                "rationale": approved["rationale"], "expires_at_ms": approved["expires_at_ms"],
                "snapshot_id": approved["snapshot_id"]}
            if order:
                result["rationale"] = approved["rationale"]
                result["expires_at_ms"] = approved["expires_at_ms"]
                result["decision_snapshot_id"] = approved["snapshot_id"]
            self._mutation_finish(tool_id, result, "completed" if result["status"] != "unknown" else "uncertain")
            return result

    def _handle_tool(self, name, args, tool_id, *, strategy_id, approved_snapshots, decision_snapshot, world,
                     trade_approval=None):
        if name == "inspect_strategies":
            strategy = self._strategy(strategy_id)
            return {"strategy": self._strategy_projection(strategy),
                    "eligibility": self._eligibility_projection(strategy_id),
                    "allocation": self._config()["live_execution"].get("allocations", {}).get(strategy_id),
                    "approved_version": strategy_id in self._config()["live_execution"].get("eligible_strategy_versions", [])}
        if name == "inspect_market":
            if args.get("strategy_id") != strategy_id:
                raise IntentRejected("Tool is scoped to the event strategy version")
            snapshot = self._fresh_market(strategy_id)
            approved_snapshots.add(snapshot["snapshot_id"])
            return self._market_projection(snapshot)
        if name == "inspect_account":
            current = self.reconcile()
            own = [row for row in current["positions"] if row.get("strategy_id") == strategy_id][:32]
            foreign = [row for row in current["positions"] if row.get("strategy_id") is None]
            return {"account": current["account"], "strategy_positions": own,
                    "foreign_position_count": len(foreign),
                    "foreign_position_notional": sum(abs(float(row.get("quantity", 0.)) * float(row.get("mark_price", 0.))) for row in foreign)}
        if name == "inspect_orders":
            current = self.reconcile()
            own = self._order_history_projection(
                [row for row in current["orders"] if row.get("strategy_id") == strategy_id])
            foreign_active = sum(row.get("strategy_id") != strategy_id and row.get("status") in ACTIVE_ORDERS
                                 for row in current["orders"])
            return {"orders": own, "foreign_active_order_count": foreign_active,
                    "acknowledgements_are_not_fills": True}
        if name == "submit_order_intent":
            return self._submit_intent(name, args, tool_id, strategy_id=strategy_id,
                                       approved_snapshots=approved_snapshots, trade_approval=trade_approval)
        if name == 'set_leverage':
            return self._set_leverage(args, tool_id, strategy_id=strategy_id,
                                      approved_snapshots=approved_snapshots, trade_approval=trade_approval)
        if name == "amend_order":
            return self._amend_order(args, tool_id, strategy_id=strategy_id,
                                     approved_snapshots=approved_snapshots, trade_approval=trade_approval)
        if name == "cancel_order":
            return self._cancel_order(args, tool_id, strategy_id=strategy_id)
        if name == "reduce_exposure":
            return self._reduce_exposure(args, tool_id, strategy_id=strategy_id,
                                         approved_snapshots=approved_snapshots, trade_approval=trade_approval)
        if name == "hold":
            return {"status": "held", "strategy_id": strategy_id,
                    "rationale": args.get("rationale", "")}
        raise IntentRejected("Execution tool is not permitted")

    def _dispatch_tool(self, name, args, tool_id, *, strategy_id, approved_snapshots, decision_snapshot, world,
                       decision_config=None, decision_eligibility=None):
        try:
            trade_approval = None
            reviewed_tools = {"submit_order_intent", "amend_order", "reduce_exposure", "set_leverage"}
            if is_sol_mode(self._config()) and name in reviewed_tools:
                # Existing durable tool identities retain their exact idempotent
                # replay/reconciliation semantics and never trigger a new review.
                if self._mutation_row(tool_id) is None:
                    trade_approval = self._prepare_trade_review(name, args, tool_id,
                        strategy_id=strategy_id, decision_snapshot=decision_snapshot,
                        decision_world=world, decision_config=decision_config,
                        decision_eligibility=decision_eligibility)
                    if not isinstance(trade_approval, dict) or trade_approval.get("status") != "approved":
                        return {"status": "blocked", "reason": _bounded_text(
                            (trade_approval or {}).get("reason", "Dedicated trade approval is required")),
                            "trade_review": {key: trade_approval.get(key) for key in
                                ("status", "review_id", "decision", "rationale", "reason", "proposal_hash")
                                if key in trade_approval} if isinstance(trade_approval, dict) else
                                {"status": "blocked"}}
                else:
                    # Reconciliation can safely finish an uncertain mutation
                    # only while its original approval still matches current
                    # state. Completed idempotent outcomes return without use.
                    trade_approval = self.trade_reviewer.approval_for_tool(tool_id, name, args)
            result = self._handle_tool(name, args, tool_id, strategy_id=strategy_id,
                approved_snapshots=approved_snapshots, decision_snapshot=decision_snapshot, world=world,
                trade_approval=trade_approval)
            if is_sol_mode(self._config()) and name in reviewed_tools and isinstance(result, dict):
                review = (trade_approval if trade_approval and trade_approval.get("status") == "approved"
                          else self.trade_reviewer.outcome_for_tool(tool_id) if self.trade_reviewer else None)
                if review:
                    result = {**result, "trade_review": {key: review[key] for key in
                        ("status", "review_id", "decision", "rationale", "reason", "proposal_hash")
                        if key in review}}
            return result
        except IntentRejected as error:
            return {"status": "blocked", "reason": _bounded_text(str(error))}
        except (ValueError, TypeError, KeyError) as error:
            return {"status": "blocked", "reason": _bounded_text(str(error))}
        except Exception as error:
            # Never reflect transport exception text into the durable tool log;
            # provider and venue errors can contain external response content.
            return {"status": "blocked", "reason": "Host execution operation failed (" + type(error).__name__ + ")"}

    @staticmethod
    def _strategy_projection(strategy):
        if not strategy:
            return None
        return {key: strategy.get(key) for key in ("id", "version", "name", "symbol", "status", "phase",
                "policy_version", "market_policy", "spec") if key in strategy}

    @staticmethod
    def _market_projection(snapshot):
        keys = ("snapshot_id", "captured_at_ms", "venue", "environment", "category", "symbol",
                "primary_timeframe", "signal_mode", "ready", "stale", "reason", "quote",
                "signal", "indicators", "data_quality", "attention_deadline_ms",
                "current_target", "signal_input_id", "forming_candle", "supporting_timeframes")
        result = {key: copy.deepcopy(snapshot.get(key)) for key in keys if key in snapshot}
        if "candles" in snapshot:
            # MarketData already bounds history to the frozen policy. Preserve
            # that declared history and context; the request byte cap rejects
            # an oversized packet instead of silently changing model inputs.
            result["candles"] = copy.deepcopy(snapshot["candles"])
        if isinstance(result.get("reason"), str):
            result["reason"] = _bounded_text(result["reason"], 400)
        return result

    def _constraints(self, strategy_id):
        config = self._config()
        live = config["live_execution"]
        bybit = config["bybit"]
        names = ("max_portfolio_notional", "max_position_notional", "max_leverage", "max_margin_fraction",
            "max_daily_loss_usd", "max_drawdown_fraction", "max_spread_bps", "max_slippage_bps",
            "max_participation_fraction", "max_quote_age_seconds", "max_account_age_seconds",
            "max_decision_age_seconds", "max_orders_per_minute", "stop_loss_fraction",
            "position_ttl_seconds", "kill_switch", "require_exchange_protection")
        limits = {key: live.get(key) for key in names}
        if is_sol_mode(config):
            limits['max_leverage'] = self._venue_leverage_limit(cached=True)
            limits['max_position_risk_fraction'] = mode_settings(config)['max_position_risk_fraction']
            limits['leverage_policy'] = 'agent_with_venue_ceiling'
        return {"policy_version": live.get("policy_version"), "execution_enabled": live.get("enabled"),
            "bybit_enabled": bybit.get("enabled"), "bybit_mode": bybit.get("mode"),
            "mainnet_enabled": bybit.get("mainnet_enabled"),
            "eligible_version": strategy_id in live.get("eligible_strategy_versions", []),
            "allocation": live.get("allocations", {}).get(strategy_id),
            "allowed_categories": list(bybit.get("allowed_categories", [])),
            "allowed_instruments": copy.deepcopy(bybit.get("allowed_instruments", [])),
            "limits": limits}

    def _decision_value(self, *, decision_id, event_id, strategy_id, fingerprint, status,
                        snapshot_id, rationale, result=None, expiry=None):
        value = {"id": decision_id, "event_id": event_id, "strategy_id": strategy_id,
            "strategy_version": strategy_id, "snapshot_id": snapshot_id, "status": status,
            "rationale": _bounded_text(rationale), "expires_at_ms": expiry,
            "constraints": self._constraints(strategy_id),
            "result": copy.deepcopy(result or {}), "created_at_ms": self._now_ms()}
        with self.store.connection() as db:
            db.execute("INSERT OR REPLACE INTO execution_decisions VALUES (?,?,?,?,?,?,?)",
                       (decision_id, event_id, strategy_id, fingerprint, status, encoded(value), value["created_at_ms"]))
        return value

    def _duplicate_decision(self, strategy_id, fingerprint):
        with self.store.connection() as db:
            row = db.execute("SELECT value FROM execution_decisions WHERE strategy_id=? AND fingerprint=? "
                             "AND status IN ('held','completed','submitted','reduction_requested','cancelled','cancel_requested','filled_during_cancel','amended','leverage_set','superseded') "
                             "ORDER BY created_at_ms DESC LIMIT 1", (strategy_id, fingerprint)).fetchone()
        return json.loads(row[0]) if row else None

    def _recent_outcomes(self, strategy_id, limit=8):
        with self.store.connection() as db:
            rows = db.execute("SELECT value FROM execution_decisions WHERE strategy_id=? ORDER BY created_at_ms DESC LIMIT ?",
                              (strategy_id, limit)).fetchall()
        outcomes = []
        for row in reversed(rows):
            value = json.loads(row[0])
            result = value.get("result", {})
            actions = []
            for item in result.get("tools", []) if isinstance(result, dict) else []:
                if not isinstance(item, dict) or item.get("name") not in {
                    "submit_order_intent", "amend_order", "cancel_order", "reduce_exposure", "set_leverage", "hold"}:
                    continue
                detail = item.get("result", {})
                if not isinstance(detail, dict):
                    detail = {}
                action = {"tool": item["name"], "status": detail.get("status")}
                for key in ("order_link_id", "filled_quantity", "expires_at_ms"):
                    if key in detail:
                        action[key] = detail[key]
                why = detail.get("reason") or detail.get("rationale") or detail.get("decision_rationale")
                if why:
                    action["rationale"] = _bounded_text(why, 400)
                actions.append(action)
            outcomes.append({"id": value.get("id"), "status": value.get("status"),
                "snapshot_id": value.get("snapshot_id"), "created_at_ms": value.get("created_at_ms"),
                "rationale": _bounded_text(value.get("rationale"), 400),
                "expires_at_ms": value.get("expires_at_ms"), "actions": actions[:8]})
        return outcomes

    def _event_summary(self, event):
        allowed = ("kind", "snapshot_id", "priority", "attention_deadline_ms", "signal_input_id", "reason")
        result = {}
        for key in allowed:
            value = event.get(key)
            if isinstance(value, str):
                result[key] = _bounded_text(value, 400 if key == "reason" else 160)
            elif isinstance(value, (int, float, bool)):
                result[key] = value
        result["kind"] = event.get("kind", "signal")
        task = event.get('schedule_task')
        if isinstance(task, dict):
            result['scheduled_task'] = {key: copy.deepcopy(task.get(key)) for key in
                ('id', 'kind', 'target', 'phase', 'trigger_at', 'expires_at', 'rationale', 'evidence_refs', 'triggers')}
        calendar = event.get('calendar_event')
        if isinstance(calendar, dict):
            result['calendar_event'] = {key: copy.deepcopy(calendar.get(key)) for key in
                ('event_id', 'revision', 'kind', 'target', 'status', 'event_at', 'event_at_utc',
                 'source_time', 'source_timezone', 'source_url', 'provenance', 'observed_at',
                 'freshness_seconds', 'uncertainty_seconds', 'stale') if key in calendar}
        return result

    def handle_event(self, event):
        """Run one bounded Sol conversation for a durable runtime event."""
        if not isinstance(event, dict):
            return {"status": "blocked", "reason": "Invalid execution event"}
        strategy_id = event.get("strategy_id")
        if event.get("kind") not in _PROTECTED_EVENT_KINDS:
            return {"status": "ignored", "reason": "Event kind is outside the execution lane"}
        strategy = self._strategy(strategy_id)
        event_id = str(event.get("id") or identity(event))[:256]
        if not strategy:
            return {"status": "blocked", "event_id": event_id, "reason": "Strategy version is unavailable"}
        config = self._config()
        account_scope = self._scope(config["bybit"].get("account_id", "unbound"))
        decision_id = identity([self._environment(), account_scope, strategy_id, event_id])
        with self.store.connection() as db:
            row = db.execute("SELECT value FROM execution_decisions WHERE id=?", (decision_id,)).fetchone()
        if row:
            return json.loads(row[0])
        if not config["bybit"].get("enabled") or not config["live_execution"].get("enabled") or not self._permission_allowed():
            reason = ("Bybit is disabled by the operator" if not config["bybit"].get("enabled") else
                      "Live execution is disabled by the operator" if not config["live_execution"].get("enabled") else
                      "Runtime permission is paused; discretionary Sol execution is blocked.")
            value = self._decision_value(decision_id=decision_id, event_id=event_id, strategy_id=strategy_id,
                fingerprint=identity([strategy_id, event.get("kind"), event.get("snapshot_id"), reason]),
                status="blocked", snapshot_id=event.get("snapshot_id"), rationale=reason)
            return value
        try:
            world = self._world()
            snapshot = self._fresh_market(strategy_id)
        except Exception as error:
            value = self._decision_value(decision_id=decision_id, event_id=event_id, strategy_id=strategy_id,
                fingerprint=identity([strategy_id, event.get("kind"), event.get("snapshot_id"), "unavailable"]),
                status="blocked", snapshot_id=event.get("snapshot_id"),
                rationale="Host market/account reconciliation failed (" + type(error).__name__ + ")")
            return value
        snapshot_id = snapshot["snapshot_id"]
        event_payload = self._event_summary(event)
        account = world["account"]
        account_fingerprint = {key: account.get(key) for key in ("equity", "available_balance", "daily_pnl",
            "drawdown_fraction", "reconciliation_required", "risk_halted")}
        account_fingerprint["positions"] = [(row.get("strategy_id"), row.get("category"), row.get("symbol"),
            row.get("quantity"), row.get("mark_price")) for row in world["positions"]]
        order_fingerprint = [(row.get("order_link_id"), row.get("status"), row.get("quantity"),
            row.get("filled_quantity"), row.get("price"), row.get("reduce_only")) for row in world["orders"]]
        config = self._config()
        controls = {"bybit": {key: config["bybit"].get(key) for key in
            ("mode", "enabled", "mainnet_enabled", "allowed_categories", "allowed_instruments")},
            "live_execution": config["live_execution"]}
        eligibility_result = self._eligibility(strategy_id)
        eligibility = self._eligibility_projection(strategy_id, eligibility_result)
        budget_available = self._budget_available()
        fingerprint = identity([strategy_id, snapshot_id, event_payload, account_fingerprint,
            order_fingerprint, controls, eligibility, budget_available, world.get("ledger_revision")])
        previous = self._duplicate_decision(strategy_id, fingerprint)
        if previous:
            return {"status": "unchanged", "strategy_id": strategy_id,
                    "snapshot_id": snapshot_id, "previous_decision_id": previous["id"],
                    "rationale": "The host snapshot and account outcomes have not changed."}
        if not self._permission_allowed():
            value = self._decision_value(decision_id=decision_id, event_id=event_id, strategy_id=strategy_id,
                fingerprint=fingerprint, status="blocked", snapshot_id=snapshot_id,
                rationale="Runtime permission is paused; discretionary Sol execution is blocked.")
            self._record_memory(world, value)
            return value
        try:
            no_action = is_sol_mode(config) and self._flat_no_action_preflight(strategy_id, world,
                eligibility_result, config, budget_available=budget_available)
        except Exception:
            no_action = False
        if no_action:
            rationale = ("Host preflight held this event: qualification, operator, instrument, risk, or budget "
                         "controls block discretionary mutations and there is no position or active "
                         "order requiring management.")
            value = self._decision_value(decision_id=decision_id, event_id=event_id,
                strategy_id=strategy_id, fingerprint=fingerprint, status="held",
                snapshot_id=snapshot_id, rationale=rationale,
                result={"summary": rationale, "tools": []})
            self._record_memory(world, value)
            return value
        try:
            dossier = self.memory.dossier(strategy_id, task='execution', max_bytes=16000)
        except Exception as error:
            value = self._decision_value(decision_id=decision_id, event_id=event_id, strategy_id=strategy_id,
                fingerprint=fingerprint, status="blocked", snapshot_id=snapshot_id,
                rationale="Execution memory unavailable (" + type(error).__name__ + ")")
            self._record_memory(world, value)
            return value
        approved_snapshots = {snapshot_id}
        safe_state = self._state_value(world)
        payload = {"event": event_payload, "strategy": self._strategy_projection(strategy),
            "dossier": dossier, "constraints": self._constraints(strategy_id),
            "decision_snapshot": self._market_projection(snapshot),
            "account": self._account_projection(world["account"]),
            "strategy_positions": [row for row in world["positions"] if row.get("strategy_id") == strategy_id][:32],
            "foreign_exposure": {"position_count": sum(row.get("strategy_id") is None for row in world["positions"]),
                "notional": sum(abs(float(row.get("quantity", 0.)) * float(row.get("mark_price", 0.)))
                    for row in world["positions"] if row.get("strategy_id") is None)},
            "strategy_orders": self._order_history_projection(
                [row for row in world["orders"] if row.get("strategy_id") == strategy_id]),
            "portfolio_active_order_count": sum(row.get("status") in ACTIVE_ORDERS for row in world["orders"]),
            "previous_outcomes": self._recent_outcomes(strategy_id),
            "eligibility": eligibility,
            "runtime_permission": True, "account_reconciliation_required": world["account"]["reconciliation_required"],
            "protection_state": safe_state["account"]}
        if callable(self.support_context_provider):
            try:
                payload['support_swarm'] = self.support_context_provider(strategy_id, snapshot)
            except Exception:
                payload['support_swarm'] = {'status': 'unavailable', 'reports': []}
        payload['experiment_id'] = strategy.get('experiment_id', strategy_id)
        request_check = None
        tool_check = None
        if is_sol_mode(config):
            request_check = lambda: self._continuation_stop_reason(
                strategy_id, strategy, snapshot, config, eligibility)
            safe_tools = {"inspect_strategies", "inspect_market", "inspect_account",
                          "inspect_orders", "hold", "cancel_order"}
            tool_check = lambda name, args: (None if name in safe_tools else
                self._continuation_stop_reason(strategy_id, strategy, snapshot, config, eligibility))
        decision_deadline_ms = None
        if is_sol_mode(config):
            captured = snapshot.get("captured_at_ms")
            if type(captured) is int:
                lifetime_ms = min(config["live_execution"]["max_decision_age_seconds"],
                    strategy["market_policy"]["max_decision_latency_seconds"]) * 1000
                decision_deadline_ms = int(captured + lifetime_ms)
                attention = snapshot.get("attention_deadline_ms")
                if type(attention) is int:
                    decision_deadline_ms = min(decision_deadline_ms, attention)
        try:
            result = self.agents.tools(payload, SOL_EXECUTION_TOOLS if is_sol_mode(config) else EXECUTION_TOOLS,
                lambda name, args, key: self._dispatch_tool(name, args, key, strategy_id=strategy_id,
                    approved_snapshots=approved_snapshots, decision_snapshot=snapshot, world=world,
                    decision_config=copy.deepcopy(config), decision_eligibility=copy.deepcopy(eligibility)),
                task_id="execution-" + decision_id, request_check=request_check,
                tool_check=tool_check, deadline_ms=decision_deadline_ms)
        except BudgetExceeded as error:
            status = "blocked"
            reason = "Sol execution budget unavailable (" + type(error).__name__ + ")"
            value = self._decision_value(decision_id=decision_id, event_id=event_id, strategy_id=strategy_id,
                fingerprint=fingerprint, status=status, snapshot_id=snapshot_id, rationale=reason,
                result={"summary": reason})
            self.store.event("execution", reason, "warning")
            self._record_memory(world, value)
            return value
        except Exception as error:
            stopped_reason = None
            if callable(request_check):
                try:
                    stopped_reason = request_check()
                except Exception:
                    stopped_reason = None
            if isinstance(stopped_reason, str) and stopped_reason:
                status = "superseded" if stopped_reason.startswith("Decision expired or superseded:") else "blocked"
                reason = stopped_reason
                value = self._decision_value(decision_id=decision_id, event_id=event_id,
                    strategy_id=strategy_id, fingerprint=fingerprint, status=status,
                    snapshot_id=snapshot_id, rationale=reason, result={"summary": reason})
                self.store.event("execution", reason, "warning")
                self._record_memory(world, value)
                return value
            status = "blocked"
            reason = "Sol execution unavailable (" + type(error).__name__ + ")"
            value = self._decision_value(decision_id=decision_id, event_id=event_id, strategy_id=strategy_id,
                fingerprint=fingerprint, status=status, snapshot_id=snapshot_id, rationale=reason,
                result={"summary": reason})
            self.store.event("execution", reason, "warning")
            self._record_memory(world, value)
            return value
        tools = result.get("tools", []) if isinstance(result, dict) else []
        tool_rows = [row for row in tools if isinstance(row, dict)]
        operations = [row for row in tool_rows if row.get("name") in {
            "submit_order_intent", "amend_order", "cancel_order", "reduce_exposure", "set_leverage"}]
        holds = [row for row in tool_rows if row.get("name") == "hold"]
        summary = _bounded_text(result.get("summary", "") if isinstance(result, dict) else "")
        action = operations[-1] if operations else (holds[-1] if holds else None)
        action_result = action.get("result", {}) if action else {}
        status_by_action = {"submit_order_intent": "submitted", "amend_order": "amended",
            "reduce_exposure": "reduction_requested", "hold": "held", "set_leverage": "leverage_set"}
        status = status_by_action.get(action.get("name"), "held") if action else "held"
        if action and action.get("name") == "cancel_order":
            cancel_status = action_result.get("status")
            status = ("cancelled" if cancel_status == "cancelled" else
                      "filled_during_cancel" if cancel_status == "filled" else
                      "blocked" if cancel_status in {"rejected", "unknown", "blocked"} else "cancel_requested")
        if any((row.get("result") or {}).get("status") in {"blocked", "unknown", "uncertain", "rejected"}
               for row in tool_rows):
            status = "blocked"
        if action:
            rationale = (action_result.get("reason") if status == "blocked" else None)
            action_rationale = (action_result.get("decision_rationale") if action.get("name") == "cancel_order"
                else action_result.get("rationale"))
            rationale = rationale or action_rationale or action_result.get("rationale") or action_result.get("decision_rationale")
            rationale = rationale or summary
            if not rationale and action.get("name") == "hold":
                rationale = "Sol recorded a hold."
        else:
            rationale = summary or "Sol made no execution proposal; the host recorded a hold."
        if result.get("status") == "stopped" and action is None:
            stop_reason = _bounded_text(result.get("reason") or summary)
            status = "superseded" if stop_reason.startswith("Decision expired or superseded") else "blocked"
            rationale = stop_reason or "Host stopped the decision before any tool action."
        rationale = _bounded_text(rationale)
        decision_snapshot_id = (action_result.get("decision_snapshot_id") or action_result.get("snapshot_id")
            or snapshot_id)
        expiry = action_result.get("expires_at_ms") if action else None
        final = self._decision_value(decision_id=decision_id, event_id=event_id, strategy_id=strategy_id,
            fingerprint=fingerprint, status=status, snapshot_id=decision_snapshot_id, rationale=rationale,
            expiry=expiry,
            result={"summary": _bounded_text(result.get("summary", "")), "tools": tools,
                    "rounds": result.get("rounds")})
        try:
            with self._mutation_lock:
                final_world = self._world()
                self.store.set_document("live_execution_state", self._state_value(final_world))
                self._record_memory(final_world, final)
        except Exception:
            # The order journal remains authoritative if the refresh fails. A
            # later reconciliation captures fills; an acknowledgement is never
            # treated as a fill here.
            self.store.set_document("live_execution_state", safe_state)
            self._record_memory(world, final)
        return final

    def _has_active_reduction(self, strategy_id, category, symbol, orders):
        return any(row.get("strategy_id") == strategy_id and row.get("category") == category and
                   row.get("symbol") == symbol and row.get("reduce_only") and self._active_order(row)
                   for row in orders)

    def _spot_protection_link(self, strategy_id, symbol, quantity, stop_loss):
        normalized = self.adapter.normalize_spot_protection(symbol, quantity, stop_loss)
        desired_hash = identity([normalized["quantity"], normalized["stop_loss"]])
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM execution_spot_protections WHERE environment=? AND strategy_id=? AND symbol=?",
                             (self._environment(), strategy_id, symbol)).fetchone()
        generation = int(row["generation"]) if row else 0
        link_id = row["order_link_id"] if row and row["desired_hash"] == desired_hash else None
        prior = next((item for item in self._orders() if item.get("order_link_id") == link_id), None) if link_id else None
        if row and (row["desired_hash"] != desired_hash or
                    (prior and prior.get("status") in _TERMINAL)):
            generation += 1
            link_id = None
        if not link_id:
            digest = hashlib.sha256(identity([self._environment(), strategy_id, symbol,
                normalized["quantity"], normalized["stop_loss"], generation]).encode()).hexdigest()[:20]
            link_id = "prism-spot-sl-" + digest
        with self.store.connection() as db:
            db.execute("INSERT INTO execution_spot_protections VALUES (?,?,?,?,?,?,?) "
                       "ON CONFLICT(environment,strategy_id,symbol) DO UPDATE SET "
                       "desired_hash=excluded.desired_hash,order_link_id=excluded.order_link_id,"
                       "generation=excluded.generation,updated_at_ms=excluded.updated_at_ms",
                       (self._environment(), strategy_id, symbol, desired_hash, link_id, generation, self._now_ms()))
        return normalized, link_id

    def _cancel_spot_protections(self, strategy_id, symbol, *, orders=None, keep_link=None):
        selected = self._orders() if orders is None else orders
        results = []
        for order in selected:
            if (order.get("strategy_id") != strategy_id or order.get("category") != "spot" or
                    order.get("symbol") != symbol or order.get("spot_protection") is not True or
                    order.get("order_link_id") == keep_link or not self._active_order(order)):
                continue
            try:
                result = self.adapter.cancel_spot_exchange_protection(order["order_link_id"])
            except Exception as error:
                result = {"order_link_id": order.get("order_link_id"), "status": "unknown",
                          "cancellation_verified": False, "reason": type(error).__name__}
            results.append(result)
        safe = all((item.get("status") == "cancelled" and item.get("cancellation_verified") is True) or
                   item.get("status") in {"filled", "rejected"} for item in results)
        return safe, results

    def _foreign_spot_sell_conflicts(self, strategy_id, symbol, orders):
        return [row for row in orders if row.get("category") == "spot" and row.get("symbol") == symbol and
                row.get("side") == "Sell" and row.get("strategy_id") != strategy_id and
                self._active_order(row) and not (row.get("strategy_id") and row.get("spot_protection") is True)]

    def _confirm_cancel_order(self, order):
        """Cancel a normal order and reconcile its fill/cancel race."""
        try:
            result = self.adapter.cancel_order(order["order_link_id"])
        except OrderOutcomeUnknown:
            result = None
        except Exception:
            result = None
        if result and result.get("status") in {"cancelled", "filled", "rejected"}:
            return result
        try:
            result = self.adapter.reconcile_order(order["order_link_id"])
        except Exception:
            return {"status": "unknown", "order_link_id": order.get("order_link_id")}
        return result or {"status": "unknown", "order_link_id": order.get("order_link_id")}

    def _halt_spot_protection(self, account, reason):
        account["risk_halted"] = True
        account["halt_reasons"] = list(dict.fromkeys([*account.get("halt_reasons", []), reason]))

    def _ensure_spot_protection(self, strategy_id, strategy, position, snapshot, world):
        symbol = position["symbol"]
        entry = position.get("entry_price")
        if not _finite(entry) or entry <= 0:
            reason = "Spot position has no verified attributed entry price"
            self._halt_spot_protection(world["account"], reason)
            return {"action": "spot_protection", "strategy_id": strategy_id, "symbol": symbol,
                    "status": "blocked", "reason": reason}

        # Foreign sell orders can spend base inventory the conditional stop
        # does not lock at Bybit. Leave them untouched and fail closed.
        live_orders = self._orders()
        foreign_sells = self._foreign_spot_sell_conflicts(strategy_id, symbol, live_orders)
        if foreign_sells:
            reason = "Foreign Spot sell order conflicts with reserved base inventory"
            self._halt_spot_protection(world["account"], reason)
            return {"action": "spot_protection", "strategy_id": strategy_id, "symbol": symbol,
                    "status": "blocked", "reason": reason}

        stop = entry * (1. - float(self._config()["live_execution"]["stop_loss_fraction"]))
        try:
            normalized, desired_link = self._spot_protection_link(
                strategy_id, symbol, position["quantity"], stop)
        except Exception as error:
            reason = "Spot exchange protection cannot be sized (" + type(error).__name__ + ")"
            self._halt_spot_protection(world["account"], reason)
            fallback = self._protect_reduce(strategy_id, position, reason, live_orders)
            return {"action": "spot_protection", "strategy_id": strategy_id, "symbol": symbol,
                    "status": "unavailable", "reason": reason, "fallback": fallback}

        protections = [row for row in live_orders if row.get("strategy_id") == strategy_id and
            row.get("category") == "spot" and row.get("symbol") == symbol and
            row.get("spot_protection") is True and self._active_order(row)]
        exact = next((row for row in protections if row.get("order_link_id") == desired_link), None)
        if exact:
            try:
                check = self.adapter.verify_spot_exchange_protection(desired_link)
            except Exception as error:
                check = {"verified": False, "reason": type(error).__name__}
            if check.get("verified") is True:
                return {"action": "spot_protection", "strategy_id": strategy_id, "symbol": symbol,
                    "order_link_id": desired_link, "quantity": normalized["quantity"],
                    "stop_loss": normalized["stop_loss"], "status": "verified",
                    "unprotected_dust_quantity": normalized['unprotected_dust_quantity'],
                    "unprotected_dust_notional": normalized['unprotected_dust_notional'],
                    "acknowledgement_is_not_verification": True}

        ordinary_sells = [row for row in live_orders if row.get("strategy_id") == strategy_id and
            row.get("category") == "spot" and row.get("symbol") == symbol and
            row.get("side") == "Sell" and row.get("spot_protection") is not True and
            self._active_order(row)]
        if protections or ordinary_sells:
            safe, cancelled = self._cancel_spot_protections(strategy_id, symbol, orders=protections)
            if not safe:
                reason = "Spot protection cancellation was not confirmed; ordinary sells are blocked"
                self._halt_spot_protection(world["account"], reason)
                return {"action": "spot_protection", "strategy_id": strategy_id, "symbol": symbol,
                        "status": "cancel_unconfirmed", "reason": reason, "cancellations": cancelled}
            ordinary_cancelled = []
            for order in ordinary_sells:
                result = self._confirm_cancel_order(order)
                ordinary_cancelled.append(result)
                if result.get("status") not in {"cancelled", "filled", "rejected"}:
                    reason = "Spot exit cancellation was not confirmed; duplicate sales are blocked"
                    self._halt_spot_protection(world["account"], reason)
                    return {"action": "spot_protection", "strategy_id": strategy_id, "symbol": symbol,
                            "status": "cancel_unconfirmed", "reason": reason,
                            "cancellations": [*cancelled, *ordinary_cancelled]}
            try:
                world = self._world()
            except Exception as error:
                reason = "Spot inventory could not be reconciled after cancellation (" + type(error).__name__ + ")"
                self._halt_spot_protection(world["account"], reason)
                return {"action": "spot_protection", "strategy_id": strategy_id, "symbol": symbol,
                        "status": "reconciliation_required", "reason": reason}
            position = next((row for row in world["positions"] if row.get("strategy_id") == strategy_id and
                             row.get("category") == "spot" and row.get("symbol") == symbol), None)
            if not position or position.get("quantity", 0) <= 1e-12:
                return {"action": "spot_protection", "strategy_id": strategy_id, "symbol": symbol,
                        "status": "position_reconciled", "cancellations": [*cancelled, *ordinary_cancelled]}
            # A racing conditional/ordinary fill may have changed both size
            # and average entry. Recompute the idempotent protection identity.
            stop = position["entry_price"] * (1. - float(self._config()["live_execution"]["stop_loss_fraction"]))
            try:
                normalized, desired_link = self._spot_protection_link(
                    strategy_id, symbol, position["quantity"], stop)
            except Exception as error:
                reason = "Spot exchange protection cannot be resized (" + type(error).__name__ + ")"
                self._halt_spot_protection(world["account"], reason)
                return {"action": "spot_protection", "strategy_id": strategy_id, "symbol": symbol,
                        "status": "unavailable", "reason": reason}

        # Never leave an unconfirmed ordinary Spot sell beside a conditional
        # sell. _risk_check reserves active conditional quantities; these
        # execution controls only replace one after cancellation/reconciliation.
        try:
            result = self.adapter.set_spot_exchange_protection(symbol, position["quantity"], stop,
                order_link_id=desired_link, strategy_id=strategy_id,
                snapshot_id=(snapshot or {}).get("snapshot_id"),
                rationale="Host-calculated cash Spot stop protection")
            check = result.get("verification", {})
        except OrderOutcomeUnknown:
            try:
                check = self.adapter.verify_spot_exchange_protection(desired_link)
            except Exception as error:
                check = {"verified": False, "reason": type(error).__name__}
            result = {"status": "unknown", "order_link_id": desired_link}
        except Exception as error:
            check = {"verified": False, "reason": type(error).__name__}
            result = {"status": "blocked", "order_link_id": desired_link}
        if check.get("verified") is True:
            return {"action": "spot_protection", "strategy_id": strategy_id, "symbol": symbol,
                    "order_link_id": desired_link, "quantity": normalized["quantity"],
                    "stop_loss": normalized["stop_loss"], "status": "verified",
                    "unprotected_dust_quantity": normalized['unprotected_dust_quantity'],
                    "unprotected_dust_notional": normalized['unprotected_dust_notional'],
                    "acknowledgement_is_not_verification": True}
        reason = "Spot exchange-side stop could not be verified"
        self._halt_spot_protection(world["account"], reason)
        fallback = self._protect_reduce(strategy_id, position, reason, self._orders())
        return {"action": "spot_protection", "strategy_id": strategy_id, "symbol": symbol,
                "status": "unverified", "reason": reason, "order_link_id": desired_link,
                "fallback": fallback, "acknowledgement_is_not_verification": True}

    def _protect_order(self, order, reason):
        try:
            result = self.adapter.cancel_order(order["order_link_id"])
            return {"action": "cancel", "order_link_id": order["order_link_id"],
                    "status": result.get("status", "cancel_pending"), "reason": reason}
        except OrderOutcomeUnknown:
            try:
                result = self.adapter.reconcile_order(order["order_link_id"])
                status = result.get("status", "unknown") if result else "unknown"
            except Exception as error:
                status = "unknown"
            return {"action": "cancel", "order_link_id": order["order_link_id"],
                    "status": status, "reason": reason}
        except Exception as error:
            return {"action": "cancel", "order_link_id": order["order_link_id"],
                    "status": "blocked", "reason": type(error).__name__}

    def _protect_reduce(self, strategy_id, position, reason, orders):
        if position["category"] == "spot":
            symbol = position["symbol"]
            if self._foreign_spot_sell_conflicts(strategy_id, symbol, orders):
                return {"action": "reduce", "strategy_id": strategy_id, "symbol": symbol,
                        "status": "foreign_sell_conflict", "reason": reason,
                        "ordinary_sell_submitted": False}
            safe, cancellations = self._cancel_spot_protections(strategy_id, position["symbol"], orders=orders)
            if not safe:
                return {"action": "reduce", "strategy_id": strategy_id, "symbol": position["symbol"],
                        "status": "cancel_unconfirmed", "reason": reason,
                        "cancellations": cancellations, "ordinary_sell_submitted": False}
            try:
                fresh = self._world()
            except Exception as error:
                return {"action": "reduce", "strategy_id": strategy_id, "symbol": position["symbol"],
                        "status": "reconciliation_required", "reason": reason,
                        "error": type(error).__name__, "ordinary_sell_submitted": False}
            position = next((row for row in fresh["positions"] if row.get("strategy_id") == strategy_id and
                             row.get("category") == "spot" and row.get("symbol") == symbol), None)
            orders = fresh["orders"]
            if not position or position.get("quantity", 0) <= 1e-12:
                return {"action": "reduce", "strategy_id": strategy_id, "symbol": symbol,
                        "status": "position_reconciled", "reason": reason, "ordinary_sell_submitted": False}
        if self._has_active_reduction(strategy_id, position["category"], position["symbol"], orders):
            return {"action": "reduce", "strategy_id": strategy_id, "symbol": position["symbol"],
                    "status": "pending", "reason": reason}
        quantity = float(position["quantity"])
        if quantity <= 0:
            return None
        attempt = sum(1 for row in orders if row.get("strategy_id") == strategy_id and
            row.get("category") == position["category"] and row.get("symbol") == position["symbol"] and
            row.get("reduce_only") and row.get("rationale") == reason)
        link_id = "prism-protect-" + hashlib.sha256(identity([self._environment(), strategy_id,
            position["category"], position["symbol"], reason, attempt]).encode()).hexdigest()[:20]
        try:
            result = self.adapter.submit_order({"category": position["category"], "symbol": position["symbol"],
                "side": "Sell", "order_type": "Market", "quantity": quantity, "price": None,
                "reduce_only": True, "order_link_id": link_id, "strategy_id": strategy_id,
                "snapshot_id": "host-protection", "rationale": _bounded_text(reason),
                "expires_at_ms": self._now_ms() + 60_000})
            return {"action": "reduce", "strategy_id": strategy_id, "symbol": position["symbol"],
                    "status": result.get("status", "unknown"), "filled_quantity": result.get("filled_quantity", 0.),
                    "acknowledgement_is_not_fill": True, "reason": reason}
        except OrderOutcomeUnknown:
            try:
                result = self.adapter.reconcile_order(link_id)
                status = result.get("status", "unknown") if result else "unknown"
                filled = result.get("filled_quantity", 0.) if result else 0.
            except Exception:
                status, filled = "unknown", 0.
            return {"action": "reduce", "strategy_id": strategy_id, "symbol": position["symbol"],
                    "status": status, "filled_quantity": filled, "acknowledgement_is_not_fill": True,
                    "reason": reason}
        except Exception as error:
            return {"action": "reduce", "strategy_id": strategy_id, "symbol": position["symbol"],
                    "status": "blocked", "reason": type(error).__name__}

    def _position_trigger(self, strategy, position, market, account):
        config = self._config()["live_execution"]
        reason = None
        if config.get("kill_switch"):
            reason = "Operator kill switch"
        elif strategy.get("status") in {"retired", "rejected"}:
            reason = "Strategy version retired"
        elif account.get("risk_halted"):
            reason = "; ".join(account.get("halt_reasons") or ["Persistent risk halt"])
        elif float(account.get("drawdown_fraction") or 0.) >= float(config["max_drawdown_fraction"]):
            reason = "Portfolio drawdown limit"
        if reason:
            return reason
        # Position age comes from exchange executions and remains actionable
        # even when the market quote is stale.
        opened = position.get("opened_at_ms")
        if isinstance(opened, int) and self._now_ms() - opened >= float(config["position_ttl_seconds"]) * 1000:
            return "Position holding-time limit"
        quote = (market or {}).get("quote", {})
        bid, quote_at = quote.get("bid"), quote.get("timestamp_ms")
        if not _finite(bid) or bid <= 0 or not isinstance(quote_at, int) or isinstance(quote_at, bool):
            return None
        if self._now_ms() - quote_at > min(config["max_quote_age_seconds"],
                strategy["market_policy"]["max_data_age_seconds"]) * 1000:
            return None
        entry = position.get("entry_price")
        if _finite(entry) and entry > 0 and bid <= entry * (1. - float(config["stop_loss_fraction"])):
            return "Host stop loss"
        return None

    def protect(self):
        """Run model-free cancellation, risk, stop, TTL, and retirement controls."""
        config = self._config()
        if not config["bybit"].get("enabled"):
            return {"status": "idle", "reason": "Bybit is disabled", "actions": []}
        actions = []
        with self._mutation_lock:
            try:
                world = self._world()
            except Exception as error:
                actions = []
                try:
                    for order in self._orders():
                        if self._active_order(order) and not order.get("reduce_only"):
                            actions.append(self._protect_order(order, "Account reconciliation failed"))
                except Exception:
                    pass
                return {"status": "blocked", "reason": "Account protection refresh failed (" + type(error).__name__ + ")",
                        "actions": actions}
            account = world["account"]
            risk_reasons = protective_reasons(account, config)
            data_bad = {}
            snapshots = {}
            relevant_strategies = {row.get("strategy_id") for row in world["orders"]
                if row.get("strategy_id") and self._active_order(row) and not row.get("reduce_only")}
            relevant_strategies.update(row.get("strategy_id") for row in world["orders"]
                if row.get("strategy_id") and row.get("spot_protection") is True and self._active_order(row))
            relevant_strategies.update(row.get("strategy_id") for row in world["positions"] if row.get("strategy_id"))
            strategy_map = self._strategy_map(self.store, relevant_strategies)
            for sid in relevant_strategies:
                strategy = strategy_map.get(sid)
                if not strategy:
                    continue
                if strategy.get("market_policy", {}).get("venue") != "bybit":
                    continue
                try:
                    snapshot = self._protection_market(sid)
                    snapshots[sid] = snapshot
                    data_bad[sid] = not snapshot.get("ready") or snapshot.get("stale", True)
                except Exception:
                    data_bad[sid] = True
            cancel_reasons = []
            if risk_reasons:
                cancel_reasons.extend(risk_reasons)
            if account["reconciliation_required"]:
                cancel_reasons.append("Account reconciliation required")
            if not config["live_execution"].get("enabled") or not self._permission_allowed():
                cancel_reasons.append("Runtime execution permission is paused")
            for order in world["orders"]:
                if not self._active_order(order) or order.get("reduce_only"):
                    continue
                strategy_id = order.get("strategy_id")
                if order.get("expires_at_ms") and self._now_ms() >= order["expires_at_ms"]:
                    actions.append(self._protect_order(order, "Order intent expired"))
                elif cancel_reasons:
                    actions.append(self._protect_order(order, "; ".join(cancel_reasons)))
                elif strategy_id in data_bad and data_bad[strategy_id]:
                    actions.append(self._protect_order(order, "Strategy market data is not ready or fresh"))
                elif strategy_id and strategy_map.get(strategy_id, {}).get("status") in {"retired", "rejected"}:
                    actions.append(self._protect_order(order, "Strategy version retired"))
            positions_by_strategy = {}
            for position in world["positions"]:
                if position.get("strategy_id"):
                    positions_by_strategy.setdefault(position["strategy_id"], []).append(position)
            for strategy_id, positions in positions_by_strategy.items():
                strategy = strategy_map.get(strategy_id)
                if not strategy:
                    continue
                snapshot = snapshots.get(strategy_id)
                trigger = None
                for position in positions:
                    trigger = self._position_trigger(strategy, position, snapshot, account)
                    if trigger:
                        actions.append(self._protect_reduce(strategy_id, position, trigger, world["orders"]))
                        continue
                    if position.get("category") == "linear":
                        actual_stop = position.get("stop_loss")
                        entry = position.get("entry_price")
                        reason = None
                        if not _finite(entry) or entry <= 0:
                            reason = "Linear position has no verified entry price"
                        else:
                            expected = entry * (1. - float(config["live_execution"]["stop_loss_fraction"]))
                            try:
                                foreign_same_instrument = any(row.get("strategy_id") is None and
                                    row.get("category") == "linear" and row.get("symbol") == position["symbol"]
                                    for row in world["positions"])
                                if _finite(actual_stop) and actual_stop > 0 and actual_stop >= expected:
                                    check = self.adapter.verify_exchange_protection("linear", position["symbol"], actual_stop)
                                elif foreign_same_instrument:
                                    check = {"verified": False}
                                    reason = "Foreign position shares this instrument; exchange stop cannot be safely amended"
                                else:
                                    check = self.adapter.verify_exchange_protection("linear", position["symbol"], expected)
                                    if not check.get("verified"):
                                        check = self.adapter.set_exchange_stop_loss("linear", position["symbol"], expected)
                                if not check.get("verified"):
                                    reason = reason or "Exchange linear stop could not be verified"
                                else:
                                    actual_verified_stop = check.get("actual_stop_loss", expected)
                                    for owned in world["positions"]:
                                        if owned.get("category") == "linear" and owned.get("symbol") == position["symbol"]:
                                            owned["stop_loss"] = actual_verified_stop
                                    account["protection_verified"] = True
                                    continue
                            except Exception:
                                reason = "Exchange linear stop verification failed"
                        # A protection failure is durable and blocks new entries;
                        # reduce only this explicitly attributed Prism quantity.
                        account["risk_halted"] = True
                        account["halt_reasons"] = list(dict.fromkeys([*account.get("halt_reasons", []), reason]))
                        actions.append(self._protect_reduce(strategy_id, position, reason, world["orders"]))
                        continue
                    if position.get("category") == "spot":
                        # Candle readiness and decision attention deadlines do
                        # not gate this model-free exchange protection path.
                        actions.append(self._ensure_spot_protection(strategy_id, strategy, position, snapshot, world))
            for order in world["orders"]:
                if (order.get("spot_protection") is not True or not self._active_order(order) or
                        not order.get("strategy_id")):
                    continue
                still_owned = any(row.get("strategy_id") == order["strategy_id"] and
                    row.get("category") == "spot" and row.get("symbol") == order.get("symbol") and
                    row.get("quantity", 0) > 1e-12 for row in world["positions"])
                if still_owned:
                    continue
                try:
                    result = self.adapter.cancel_spot_exchange_protection(order["order_link_id"])
                except Exception as error:
                    result = {"status": "unknown", "cancellation_verified": False,
                              "reason": type(error).__name__}
                if result.get("status") not in {"cancelled", "filled", "rejected"}:
                    self._halt_spot_protection(account,
                        "Stale Spot protection cancellation could not be confirmed")
                actions.append({"action": "spot_protection_cleanup", "strategy_id": order["strategy_id"],
                    "symbol": order.get("symbol"), "order_link_id": order.get("order_link_id"),
                    "status": result.get("status", "unknown"),
                    "cancellation_verified": result.get("cancellation_verified") is True})
            try:
                latest_world = self._world()
                if account.get("risk_halted"):
                    latest_world["account"]["risk_halted"] = True
                    latest_world["account"]["halt_reasons"] = list(dict.fromkeys([
                        *latest_world["account"].get("halt_reasons", []), *account.get("halt_reasons", [])]))
                world = latest_world
                account = world["account"]
            except Exception:
                # Preserve the last complete protection pass. In particular,
                # a position stop does not depend on the candle attention loop.
                pass
            # Keep a newly detected protection failure sticky across restarts.
            if account.get("risk_halted"):
                scope = world["scope"]
                with self.store.connection() as db:
                    row = db.execute("SELECT value FROM execution_account_state WHERE scope=?", (scope,)).fetchone()
                saved = json.loads(row[0]) if row else {"day": account.get("day"),
                    "daily_baseline_equity": account.get("daily_baseline_equity"),
                    "peak_equity": account.get("peak_equity")}
                saved["risk_halted"] = True
                saved["halt_reasons"] = account.get("halt_reasons", [])
                self._persist_account(scope, world["raw"], saved)
            state = self._state_value(world)
            self.store.set_document("live_execution_state", state)
            self._record_memory(world)
        return {"status": "protected" if actions else "clear", "risk_reasons": risk_reasons,
                "data_health": {sid: not bad for sid, bad in data_bad.items()}, "actions": actions}

    def state(self):
        """Return the last durable, credential-free execution projection."""
        value = self.store.get_document("live_execution_state", {}) or {}
        with self.store.connection() as db:
            decisions = db.execute("SELECT value FROM execution_decisions ORDER BY created_at_ms DESC LIMIT 32").fetchall()
        value["decisions"] = [json.loads(row[0]) for row in reversed(decisions)]
        return value
