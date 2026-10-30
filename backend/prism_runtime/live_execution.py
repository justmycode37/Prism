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
from .execution_contract import EXECUTION_TOOLS
from .live_research import LiveResearch
from .risk_controls import ACTIVE_ORDERS, IntentRejected, protective_reasons, validate_intent
from .store import BudgetExceeded, encoded


_TERMINAL = {"filled", "cancelled", "rejected"}
_PROTECTED_EVENT_KINDS = {"candle_closed", "intrabar", "signal", "fill", "order", "position",
                          "risk", "scheduled_event", "decision_deadline", "data_health"}


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

    def __init__(self, store, adapter, market, memory, agents, *, eligibility=None, clock=None):
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
        self._mutation_lock = threading.RLock()
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
    def _strategy_map(store):
        with store.connection() as db:
            rows = db.execute("SELECT value FROM cascade_strategies ORDER BY rowid DESC").fetchall()
        result = {}
        for row in rows:
            value = json.loads(row[0])
            if isinstance(value, dict) and isinstance(value.get("id"), str):
                result.setdefault(value["id"], value)
        return result

    def _strategy(self, strategy_id):
        if not isinstance(strategy_id, str) or not strategy_id:
            return None
        return self._strategy_map(self.store).get(strategy_id)

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
        lots, _ = self._attributed_inventory(self._orders(), self._executions())
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

    def _orders(self):
        orders = self.adapter.orders(limit=5000)
        controls = {}
        with self.store.connection() as db:
            controls = {row["order_link_id"]: dict(row) for row in db.execute(
                "SELECT * FROM execution_order_controls").fetchall()}
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
        try:
            return self.adapter.executions(limit=10000)
        except (AttributeError, TypeError):
            return []

    @staticmethod
    def _summary_order(order):
        fields = ("order_link_id", "status", "strategy_id", "category", "symbol", "side", "order_type",
                  "quantity", "filled_quantity", "reference_price", "price", "reduce_only", "stop_loss",
                  "order_filter", "spot_protection", "parent_order_link_id", "expires_at_ms",
                  "average_price", "fees", "fee_currency", "fees_by_currency", "realized_pnl",
                  "unprotected_dust_quantity", "unprotected_dust_notional",
                  "protection_verified", "snapshot_id", "rationale")
        result = {key: order[key] for key in fields if key in order}
        if isinstance(result.get("rationale"), str):
            result["rationale"] = _bounded_text(result["rationale"], 400)
        return result

    def _actual_spot_positions(self, raw, strategy_map, orders, executions):
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
        fills, attribution_incomplete = self._attributed_inventory(orders, executions)
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
        for timestamp_ms, item, order, qty, price in sorted(normalized_execs, key=lambda row: (row[0], row[1].get("execId", ""))):
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

    def _account_rows(self, raw, orders, executions):
        strategy_map = self._strategy_map(self.store)
        fills, incomplete = self._attributed_inventory(orders, executions)
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
        spot, spot_incomplete = self._actual_spot_positions(raw, strategy_map, orders, executions)
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
        transaction_reader = getattr(self.adapter, "transactions", None)
        if callable(transaction_reader):
            try:
                for transaction in transaction_reader(since_ms=day_start_ms):
                    if not isinstance(transaction, dict):
                        continue
                    kind = str(transaction.get("type", "")).upper()
                    raw_flow = transaction.get("cash_flow", transaction.get("cashFlow", 0))
                    try:
                        flow = float(raw_flow or 0)
                    except (TypeError, ValueError, OverflowError):
                        cashflow_state_known = False
                        break
                    if any(token in kind for token in ("TRANSFER", "DEPOSIT", "WITHDRAW", "CASH_IN", "CASH_OUT")):
                        external_cashflow = True
                        break
                    # Bybit's transaction log documents cashFlow for transfers
                    # as well as internal realized P&L. Unknown nonzero types
                    # are treated as external to avoid claiming an unadjusted
                    # equity delta as daily strategy P&L.
                    if flow and kind not in {"TRADE", "SETTLEMENT", "CLOSE", "DELIVERY", "LIQUIDATION", "ADL", "BLOCK_TRADE"}:
                        external_cashflow = True
                        break
            except Exception:
                cashflow_state_known = False
        else:
            cashflow_state_known = False
        equity = raw.get("equity")
        if not _finite(equity) or equity <= 0:
            equity = None
        if equity is not None:
            baseline = saved.get("daily_baseline_equity") if saved.get("day") == day else equity
            peak = max(float(saved.get("peak_equity", equity)), equity)
            daily_pnl = equity - baseline
            drawdown = max(0., 1. - equity / peak) if peak > 0 else None
        else:
            baseline, peak, daily_pnl, drawdown = None, saved.get("peak_equity"), None, None
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
                                   equity is None or not cashflow_state_known)
        leverage = raw.get("leverage")
        if any(position.get("category") == "spot" for position in positions) and (not _finite(leverage) or leverage <= 0):
            # Bybit spot here is nonmargin base-coin only; risk_controls accepts
            # a verified cash leverage of one for this supported protocol.
            leverage = 1.
        account = {"venue": "bybit", "mode": raw.get("mode"), "account_type": account_type,
            "updated_at_ms": raw.get("updated_at_ms"), "reconciliation_required": reconciliation_required,
            "equity": equity, "available_balance": raw.get("available_balance"), "daily_pnl": daily_pnl,
            "drawdown_fraction": drawdown, "peak_equity": peak, "daily_baseline_equity": baseline,
            "day": day, "positions": positions, "leverage": leverage,
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
        orders = self._orders()
        executions = self._executions()
        positions, incomplete = self._account_rows(raw, orders, executions)
        account, scope = self._account_metrics(raw, positions, orders, incomplete)
        return {"raw": raw, "account": account, "orders": orders, "positions": positions,
                "executions": executions, "scope": scope,
                "reconciliation_required": account["reconciliation_required"]}

    def _state_value(self, world):
        return {"environment": self._environment(), "updated_at_ms": self._now_ms(),
                "account": self._account_projection(world["account"]),
                "positions": copy.deepcopy(world["positions"]),
                "orders": [self._summary_order(order) for order in world["orders"]],
                "reconciliation_required": world["account"]["reconciliation_required"],
                "risk_halted": world["account"]["risk_halted"],
                "halt_reasons": list(world["account"].get("halt_reasons", []))}

    @staticmethod
    def _account_projection(account):
        keys = ("venue", "mode", "account_type", "updated_at_ms", "reconciliation_required", "equity",
                "available_balance", "daily_pnl", "daily_baseline_equity", "drawdown_fraction", "peak_equity", "day", "leverage",
                "position_mode", "protection_supported", "protection_verified", "risk_halted",
                "halt_reasons", "portfolio_notional", "account_binding_verified",
                "external_cashflow_detected", "daily_pnl_verified")
        value = {key: account.get(key) for key in keys}
        return value

    def _record_memory(self, world, decision=None):
        orders = world["orders"]
        link_owner = {row.get("order_link_id"): row.get("strategy_id") for row in orders if row.get("strategy_id")}
        order_owner = {row.get("order_id"): row.get("strategy_id") for row in orders if row.get("strategy_id")}
        fills_by_strategy = {}
        for item in world.get("executions", []):
            sid = link_owner.get(item.get("orderLinkId")) or order_owner.get(item.get("orderId"))
            if not sid:
                continue
            safe = {key: item.get(key) for key in ("execId", "orderLinkId", "orderId", "category", "symbol",
                    "side", "execQty", "execPrice", "execFee", "feeCurrency", "execPnl", "execTime", "execType") if key in item}
            fills_by_strategy.setdefault(sid, []).append(safe)
        active_ids = set(fills_by_strategy)
        active_ids.update(row.get("strategy_id") for row in orders if row.get("strategy_id"))
        active_ids.update(row.get("strategy_id") for row in world["positions"] if row.get("strategy_id"))
        if decision and decision.get("strategy_id"):
            active_ids.add(decision["strategy_id"])
        for sid in active_ids:
            if not self._strategy(sid):
                continue
            selected_orders = [self._summary_order(row) for row in orders if row.get("strategy_id") == sid]
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

    def _eligibility(self, strategy_id):
        try:
            result = self.eligibility(strategy_id)
            return result if isinstance(result, dict) else {"eligible": False, "reasons": ["Eligibility service returned invalid state"]}
        except Exception as error:
            return {"eligible": False, "reasons": ["Eligibility unavailable (" + type(error).__name__ + ")"]}

    def _eligibility_projection(self, strategy_id):
        result = self._eligibility(strategy_id)
        keys = ("eligible", "status", "allocation", "policy_version", "observation_unit",
                "verified_observations", "sessions", "trades", "net_return", "drawdown",
                "benchmark_return", "cost_complete")
        projected = {key: result[key] for key in keys if key in result and
                     (isinstance(result[key], (str, int, float, bool)) or result[key] is None)}
        projected["reasons"] = [_bounded_text(item, 400) for item in result.get("reasons", [])[:12]]
        return projected

    @staticmethod
    def _active_order(order):
        return order.get("status") in ACTIVE_ORDERS

    def _risk_check(self, intent, *, strategy, decision_snapshot, current_market, world, orders,
                    eligibility, budget_available):
        now = self._now_ms()
        recent = sum(1 for order in world["orders"] if
                     isinstance(order.get("created_at_ms"), int) and order["created_at_ms"] >= now - 60_000)
        return validate_intent(intent, strategy=strategy, decision_snapshot=decision_snapshot,
            market=current_market, account=world["account"], orders=orders,
            eligibility=eligibility, config=self._config(), now_ms=now,
            budget_available=budget_available, recent_order_count=recent)

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

    def _submit_intent(self, name, args, tool_id, *, strategy_id, approved_snapshots):
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

    def _amend_order(self, args, tool_id, *, strategy_id, approved_snapshots):
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

    def _reduce_exposure(self, args, tool_id, *, strategy_id, approved_snapshots):
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

    def _handle_tool(self, name, args, tool_id, *, strategy_id, approved_snapshots, decision_snapshot, world):
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
            own = [row for row in current["orders"] if row.get("strategy_id") == strategy_id][-64:]
            foreign_active = sum(row.get("strategy_id") != strategy_id and row.get("status") in ACTIVE_ORDERS
                                 for row in current["orders"])
            return {"orders": own, "foreign_active_order_count": foreign_active,
                    "acknowledgements_are_not_fills": True}
        if name == "submit_order_intent":
            return self._submit_intent(name, args, tool_id, strategy_id=strategy_id,
                                       approved_snapshots=approved_snapshots)
        if name == "amend_order":
            return self._amend_order(args, tool_id, strategy_id=strategy_id,
                                     approved_snapshots=approved_snapshots)
        if name == "cancel_order":
            return self._cancel_order(args, tool_id, strategy_id=strategy_id)
        if name == "reduce_exposure":
            return self._reduce_exposure(args, tool_id, strategy_id=strategy_id,
                                         approved_snapshots=approved_snapshots)
        if name == "hold":
            return {"status": "held", "strategy_id": strategy_id,
                    "rationale": args.get("rationale", "")}
        raise IntentRejected("Execution tool is not permitted")

    def _dispatch_tool(self, name, args, tool_id, *, strategy_id, approved_snapshots, decision_snapshot, world):
        try:
            return self._handle_tool(name, args, tool_id, strategy_id=strategy_id,
                approved_snapshots=approved_snapshots, decision_snapshot=decision_snapshot, world=world)
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
        return {"policy_version": live.get("policy_version"), "execution_enabled": live.get("enabled"),
            "bybit_enabled": bybit.get("enabled"), "bybit_mode": bybit.get("mode"),
            "mainnet_enabled": bybit.get("mainnet_enabled"),
            "eligible_version": strategy_id in live.get("eligible_strategy_versions", []),
            "allocation": live.get("allocations", {}).get(strategy_id),
            "allowed_categories": list(bybit.get("allowed_categories", [])),
            "allowed_instruments": copy.deepcopy(bybit.get("allowed_instruments", [])),
            "limits": {key: live.get(key) for key in names}}

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
                             "AND status IN ('held','completed','submitted','reduction_requested','cancelled','cancel_requested','filled_during_cancel','amended') "
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
                    "submit_order_intent", "amend_order", "cancel_order", "reduce_exposure", "hold"}:
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
            dossier = self.memory.dossier(strategy_id, task='execution', max_bytes=16000)
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
        eligibility = self._eligibility_projection(strategy_id)
        budget_available = self._budget_available()
        fingerprint = identity([strategy_id, snapshot_id, event_payload, account_fingerprint,
            order_fingerprint, controls, eligibility, budget_available])
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
            "strategy_orders": [self._summary_order(row) for row in world["orders"] if row.get("strategy_id") == strategy_id][-64:],
            "portfolio_active_order_count": sum(row.get("status") in ACTIVE_ORDERS for row in world["orders"]),
            "previous_outcomes": self._recent_outcomes(strategy_id),
            "eligibility": self._eligibility_projection(strategy_id),
            "runtime_permission": True, "account_reconciliation_required": world["account"]["reconciliation_required"],
            "protection_state": safe_state["account"]}
        try:
            result = self.agents.tools(payload, EXECUTION_TOOLS,
                lambda name, args, key: self._dispatch_tool(name, args, key, strategy_id=strategy_id,
                    approved_snapshots=approved_snapshots, decision_snapshot=snapshot, world=world),
                task_id="execution-" + decision_id)
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
            "submit_order_intent", "amend_order", "cancel_order", "reduce_exposure"}]
        holds = [row for row in tool_rows if row.get("name") == "hold"]
        summary = _bounded_text(result.get("summary", "") if isinstance(result, dict) else "")
        action = operations[-1] if operations else (holds[-1] if holds else None)
        action_result = action.get("result", {}) if action else {}
        status_by_action = {"submit_order_intent": "submitted", "amend_order": "amended",
            "reduce_exposure": "reduction_requested", "hold": "held"}
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
            strategy_map = self._strategy_map(self.store)
            snapshots = {}
            relevant_strategies = {row.get("strategy_id") for row in world["orders"]
                if row.get("strategy_id") and self._active_order(row) and not row.get("reduce_only")}
            relevant_strategies.update(row.get("strategy_id") for row in world["orders"]
                if row.get("strategy_id") and row.get("spot_protection") is True and self._active_order(row))
            relevant_strategies.update(row.get("strategy_id") for row in world["positions"] if row.get("strategy_id"))
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
