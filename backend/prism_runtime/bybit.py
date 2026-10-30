"""Bybit V5 market, account, order, and private-stream adapter.

Construction is offline and lazy.  A transport is opened only by a public
method that explicitly makes a request or by ``run_private_stream``.  SQLite
tables owned by this module form a durable intent, order, execution, and
account-transaction journal; credentials are never written to that journal.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN
import hashlib
import hmac
import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .credentials import get_bybit_credentials
from .market_policy import timeframe
from .risk_controls import ACTIVE_ORDERS
from .websocket_transport import WebSocketTransport


PAPER_REST_URL = "https://paper.bybit.invalid"
PAPER_PRIVATE_WS_URL = "wss://paper.bybit.invalid/v5/private"
TESTNET_REST_URL = "https://api-testnet.bybit.com"
MAINNET_REST_URL = "https://api.bybit.com"
TESTNET_PRIVATE_WS_URL = "wss://stream-testnet.bybit.com/v5/private"
MAINNET_PRIVATE_WS_URL = "wss://stream.bybit.com/v5/private"
REST_URLS = {"paper": PAPER_REST_URL, "testnet": TESTNET_REST_URL, "mainnet": MAINNET_REST_URL}
PRIVATE_WS_URLS = {"paper": PAPER_PRIVATE_WS_URL, "testnet": TESTNET_PRIVATE_WS_URL,
                   "mainnet": MAINNET_PRIVATE_WS_URL}
INTERVAL_MS = {str(value): value * 60_000 for value in (1, 3, 5, 15, 30, 60, 120, 240, 360, 720)}
INTERVAL_MS.update({"D": 86_400_000, "W": 604_800_000})
ORDER_LINK_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,36}$")
SUPPORTED_CATEGORIES = {"spot", "linear"}
MAX_CURSOR_PAGES = 1000
DAY_MS = 86_400_000
SEVEN_DAYS_MS = 7 * DAY_MS
RECONCILIATION_ORDER_STATUSES = {'unknown', 'submitting', 'cancel_pending', 'acknowledged'}


class BybitError(RuntimeError):
    """Base adapter error."""


class BybitAPIError(BybitError):
    def __init__(self, code, message, *, endpoint=None):
        super().__init__(f"Bybit API error {code}: {message}")
        self.code = code
        self.endpoint = endpoint


class OrderOutcomeUnknown(BybitError):
    """A create request may have reached Bybit; reconcile its same link ID."""

    def __init__(self, order_link_id, message="Order outcome is unknown; reconcile before taking further action"):
        super().__init__(message)
        self.order_link_id = order_link_id


class _OfflineOnlyTransport:
    def request(self, method, url, *, headers=None, body=None, timeout=10):
        raise RuntimeError("Paper mode requires an injected offline REST transport")


class _UrllibTransport:
    """Lazily used stdlib HTTPS transport."""

    def request(self, method, url, *, headers=None, body=None, timeout=10):
        request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read(4_000_001).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read(1_000_000)
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise BybitError(f"HTTP {exc.code} from Bybit") from exc


def _clock_value(clock):
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Bybit clock must return a finite, nonnegative epoch time")
    return float(value)


def _number(value, name, *, positive=False, optional=False):
    if optional and value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"Invalid {name}")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid {name}") from exc
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError(f"Invalid {name}")
    return number


def _float(value, name, *, positive=False, default=None):
    if value in (None, ""):
        if default is not None:
            return default
        raise ValueError(f"Missing {name}")
    result = float(_number(value, name, positive=positive and default != 0))
    if positive and default == 0 and result < 0:
        raise ValueError(f"Invalid {name}")
    return result


def _decimal_string(value):
    return format(value.normalize(), "f") if value else "0"


def _json(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"), ensure_ascii=False)


class BybitAdapter:
    """Bybit V5 adapter with a durable local order and accounting journal.

    Args:
        store: Prism ``Store`` (its SQLite connection context manager is used).
        config: Full Prism config or the ``bybit`` settings section.
        credential_loader: Optional ``loader(mode) -> {api_key, api_secret,
            account_id}``. Called only for authenticated calls and never in paper.
        transport: Optional object with ``request(method, url, headers=,
            body=, timeout=)`` or a callable with that same signature.
        websocket_factory: Optional ``factory(url)`` returning an object with
            ``send_json``, ``recv_json(timeout=...)``, and ``close``.
        clock: Callable returning epoch seconds. Used for signing and freshness.
        sleeper: Callable used only for private-stream reconnect backoff.
    """

    def __init__(self, store, config, credential_loader=None, transport=None,
                 websocket_factory=None, clock=None, *, sleeper=None,
                 request_timeout=10, recv_window=5000):
        if store is None or not hasattr(store, "connection"):
            raise ValueError("BybitAdapter requires a Prism store")
        if config is None:
            config = store.config()
        if not isinstance(config, dict):
            raise ValueError("Bybit config must be an object")
        config = deepcopy(config)
        self.config = deepcopy(config.get("bybit", config))
        self.live_config = deepcopy(config.get("live_execution", {}))
        self.store = store
        self.mode = self.config.get("mode", "paper")
        if self.mode not in REST_URLS:
            raise ValueError("Bybit mode must be paper, testnet, or mainnet")
        self.rest_url = REST_URLS[self.mode]
        self.private_ws_url = PRIVATE_WS_URLS[self.mode]
        self.credential_loader = credential_loader or get_bybit_credentials
        self._transport_injected = transport is not None
        self.transport = transport or (_OfflineOnlyTransport() if self.mode == "paper" else _UrllibTransport())
        # Public-only paper facades may read testnet. Their metadata must never
        # reuse the mainnet/paper specification cache.
        self.metadata_mode = 'testnet' if 'api-testnet.' in getattr(self.transport, 'base_url', '') else self.mode
        self._websocket_factory_injected = websocket_factory is not None
        self.websocket_factory = websocket_factory or WebSocketTransport
        self.clock = clock or time.time
        self.sleeper = sleeper or time.sleep
        if (isinstance(request_timeout, bool) or not isinstance(request_timeout, (int, float)) or
                not math.isfinite(request_timeout) or request_timeout <= 0):
            raise ValueError("Request timeout must be positive")
        if isinstance(recv_window, bool) or not isinstance(recv_window, int) or not 1000 <= recv_window <= 20000:
            raise ValueError("Receive window must be from 1000 to 20000 milliseconds")
        self.request_timeout = float(request_timeout)
        self.recv_window = recv_window
        self._credentials = None
        self._verified_account = None
        self._server_offset_ms = None
        self._clock_sync_at_ms = None
        self._schema()

    def _schema(self):
        with self.store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bybit_orders (
                    mode TEXT NOT NULL,
                    order_link_id TEXT NOT NULL,
                    order_id TEXT,
                    category TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    filled_qty TEXT NOT NULL DEFAULT '0',
                    average_price TEXT,
                    fee_total TEXT NOT NULL DEFAULT '0',
                    realized_pnl TEXT NOT NULL DEFAULT '0',
                    stop_loss TEXT,
                    protection_verified INTEGER NOT NULL DEFAULT 0,
                    exchange_status TEXT,
                    last_mutation TEXT,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(mode, order_link_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_bybit_orders_order_id
                    ON bybit_orders(mode, order_id) WHERE order_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_bybit_orders_status
                    ON bybit_orders(mode, status, category, symbol);
                CREATE INDEX IF NOT EXISTS idx_bybit_orders_mode_created_link
                    ON bybit_orders(mode, created_at_ms, order_link_id);
                CREATE INDEX IF NOT EXISTS idx_bybit_orders_instrument_created
                    ON bybit_orders(mode, category, symbol, created_at_ms, order_link_id);
                CREATE TABLE IF NOT EXISTS bybit_executions (
                    mode TEXT NOT NULL,
                    exec_id TEXT NOT NULL,
                    order_link_id TEXT,
                    order_id TEXT,
                    category TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    qty TEXT NOT NULL,
                    price TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_currency TEXT,
                    realized_pnl TEXT NOT NULL,
                    exec_time_ms INTEGER NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY(mode, exec_id)
                );
                CREATE INDEX IF NOT EXISTS idx_bybit_executions_link
                    ON bybit_executions(mode, order_link_id, exec_time_ms);
                CREATE INDEX IF NOT EXISTS idx_bybit_executions_mode_time_id
                    ON bybit_executions(mode, exec_time_ms, exec_id);
                CREATE INDEX IF NOT EXISTS idx_bybit_executions_instrument_time
                    ON bybit_executions(mode, category, symbol, exec_time_ms, exec_id);
                CREATE TABLE IF NOT EXISTS bybit_transactions (
                    mode TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    category TEXT,
                    symbol TEXT,
                    currency TEXT,
                    transaction_time_ms INTEGER NOT NULL,
                    funding TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    cash_flow TEXT NOT NULL,
                    change_amount TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY(mode, transaction_id)
                );
                CREATE INDEX IF NOT EXISTS idx_bybit_transactions_mode_time_id
                    ON bybit_transactions(mode, transaction_time_ms, transaction_id);
                CREATE TABLE IF NOT EXISTS bybit_instruments (
                    mode TEXT NOT NULL,
                    category TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY(mode, category, symbol)
                );
                CREATE TABLE IF NOT EXISTS bybit_account_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    captured_at_ms INTEGER NOT NULL,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bybit_positions (
                    mode TEXT NOT NULL,
                    category TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    position_idx INTEGER NOT NULL,
                    captured_at_ms INTEGER NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY(mode, category, symbol, position_idx)
                );
                CREATE TABLE IF NOT EXISTS bybit_wallets (
                    mode TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    captured_at_ms INTEGER NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY(mode, currency)
                );
                CREATE TABLE IF NOT EXISTS bybit_meta (
                    mode TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY(mode, key)
                );
            """)
            columns = {row[1] for row in db.execute('PRAGMA table_info(bybit_orders)')}
            if 'fee_breakdown' not in columns:
                db.execute('ALTER TABLE bybit_orders ADD COLUMN fee_breakdown TEXT')
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bybit_ledger_revision (
                    mode TEXT PRIMARY KEY,
                    orders INTEGER NOT NULL DEFAULT 0,
                    executions INTEGER NOT NULL DEFAULT 0
                );
                CREATE TRIGGER IF NOT EXISTS bybit_orders_revision_insert
                AFTER INSERT ON bybit_orders BEGIN
                    INSERT INTO bybit_ledger_revision(mode,orders,executions) VALUES (NEW.mode,1,0)
                    ON CONFLICT(mode) DO UPDATE SET orders=orders+1;
                END;
                CREATE TRIGGER IF NOT EXISTS bybit_orders_revision_update
                AFTER UPDATE ON bybit_orders
                WHEN OLD.mode IS NOT NEW.mode OR OLD.order_link_id IS NOT NEW.order_link_id OR
                     OLD.order_id IS NOT NEW.order_id OR OLD.category IS NOT NEW.category OR
                     OLD.symbol IS NOT NEW.symbol OR OLD.status IS NOT NEW.status OR
                     OLD.intent IS NOT NEW.intent OR OLD.filled_qty IS NOT NEW.filled_qty OR
                     OLD.average_price IS NOT NEW.average_price OR OLD.fee_total IS NOT NEW.fee_total OR
                     OLD.realized_pnl IS NOT NEW.realized_pnl OR OLD.stop_loss IS NOT NEW.stop_loss OR
                     OLD.protection_verified IS NOT NEW.protection_verified OR
                     OLD.exchange_status IS NOT NEW.exchange_status OR OLD.last_mutation IS NOT NEW.last_mutation OR
                     OLD.created_at_ms IS NOT NEW.created_at_ms OR OLD.fee_breakdown IS NOT NEW.fee_breakdown
                BEGIN
                    INSERT INTO bybit_ledger_revision(mode,orders,executions) VALUES (OLD.mode,1,0)
                    ON CONFLICT(mode) DO UPDATE SET orders=orders+1;
                    INSERT INTO bybit_ledger_revision(mode,orders,executions) VALUES (NEW.mode,1,0)
                    ON CONFLICT(mode) DO UPDATE SET orders=orders+CASE WHEN NEW.mode IS OLD.mode THEN 0 ELSE 1 END;
                END;
                CREATE TRIGGER IF NOT EXISTS bybit_orders_revision_delete
                AFTER DELETE ON bybit_orders BEGIN
                    INSERT INTO bybit_ledger_revision(mode,orders,executions) VALUES (OLD.mode,1,0)
                    ON CONFLICT(mode) DO UPDATE SET orders=orders+1;
                END;
                CREATE TRIGGER IF NOT EXISTS bybit_executions_revision_insert
                AFTER INSERT ON bybit_executions BEGIN
                    INSERT INTO bybit_ledger_revision(mode,orders,executions) VALUES (NEW.mode,0,1)
                    ON CONFLICT(mode) DO UPDATE SET executions=executions+1;
                END;
                CREATE TRIGGER IF NOT EXISTS bybit_executions_revision_update
                AFTER UPDATE ON bybit_executions BEGIN
                    INSERT INTO bybit_ledger_revision(mode,orders,executions) VALUES (OLD.mode,0,1)
                    ON CONFLICT(mode) DO UPDATE SET executions=executions+1;
                    INSERT INTO bybit_ledger_revision(mode,orders,executions) VALUES (NEW.mode,0,1)
                    ON CONFLICT(mode) DO UPDATE SET executions=executions+CASE WHEN NEW.mode IS OLD.mode THEN 0 ELSE 1 END;
                END;
                CREATE TRIGGER IF NOT EXISTS bybit_executions_revision_delete
                AFTER DELETE ON bybit_executions BEGIN
                    INSERT INTO bybit_ledger_revision(mode,orders,executions) VALUES (OLD.mode,0,1)
                    ON CONFLICT(mode) DO UPDATE SET executions=executions+1;
                END;
                CREATE TABLE IF NOT EXISTS bybit_transaction_revision (
                    mode TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TRIGGER IF NOT EXISTS bybit_transactions_revision_insert
                AFTER INSERT ON bybit_transactions BEGIN
                    INSERT INTO bybit_transaction_revision(mode,revision) VALUES (NEW.mode,1)
                    ON CONFLICT(mode) DO UPDATE SET revision=revision+1;
                END;
                CREATE TRIGGER IF NOT EXISTS bybit_transactions_revision_update
                AFTER UPDATE ON bybit_transactions
                WHEN OLD.mode IS NOT NEW.mode OR OLD.transaction_id IS NOT NEW.transaction_id OR
                     OLD.category IS NOT NEW.category OR OLD.symbol IS NOT NEW.symbol OR
                     OLD.currency IS NOT NEW.currency OR OLD.transaction_time_ms IS NOT NEW.transaction_time_ms OR
                     OLD.funding IS NOT NEW.funding OR OLD.fee IS NOT NEW.fee OR
                     OLD.cash_flow IS NOT NEW.cash_flow OR OLD.change_amount IS NOT NEW.change_amount OR
                     OLD.value IS NOT NEW.value
                BEGIN
                    INSERT INTO bybit_transaction_revision(mode,revision) VALUES (OLD.mode,1)
                    ON CONFLICT(mode) DO UPDATE SET revision=revision+1;
                    INSERT INTO bybit_transaction_revision(mode,revision) VALUES (NEW.mode,1)
                    ON CONFLICT(mode) DO UPDATE SET revision=revision+CASE WHEN NEW.mode IS OLD.mode THEN 0 ELSE 1 END;
                END;
                CREATE TRIGGER IF NOT EXISTS bybit_transactions_revision_delete
                AFTER DELETE ON bybit_transactions BEGIN
                    INSERT INTO bybit_transaction_revision(mode,revision) VALUES (OLD.mode,1)
                    ON CONFLICT(mode) DO UPDATE SET revision=revision+1;
                END;
            """)

    def _now_ms(self):
        return round(_clock_value(self.clock) * 1000)

    def _effective_now_ms(self):
        return self._now_ms() + (self._server_offset_ms or 0)

    def _meta_get(self, key, default=None):
        with self.store.connection() as db:
            row = db.execute("SELECT value FROM bybit_meta WHERE mode=? AND key=?", (self.mode, key)).fetchone()
        return json.loads(row[0]) if row else default

    def _meta_set(self, key, value):
        with self.store.connection() as db:
            db.execute("INSERT INTO bybit_meta(mode,key,value) VALUES (?,?,?) "
                       "ON CONFLICT(mode,key) DO UPDATE SET value=excluded.value",
                       (self.mode, key, _json(value)))

    def _guard_environment(self):
        """Fail closed before even an injected transport sees a mainnet request."""
        if self.mode != "mainnet":
            return
        if self.config.get("enabled") is not True or self.config.get("mainnet_enabled") is not True:
            raise BybitError("Mainnet is disabled by the explicit adapter configuration gate")
        if os.environ.get("PRISM_BYBIT_MAINNET_ENABLED") != "1":
            raise BybitError("Mainnet requires PRISM_BYBIT_MAINNET_ENABLED=1")
        configured_account = str(self.config.get("account_id", "")).strip()
        if not configured_account:
            raise BybitError("Mainnet requires an explicitly configured account_id")
        credential = self._get_credentials()
        if credential.get("account_id") != configured_account:
            raise BybitError("Mainnet credential account does not match configured account_id")

    def _get_credentials(self):
        if self.mode == "paper":
            raise BybitError("Paper mode never loads exchange credentials")
        if self._credentials is None:
            credentials = self.credential_loader(self.mode)
            if not isinstance(credentials, dict) or set(credentials) != {"api_key", "api_secret", "account_id"}:
                raise BybitError("Authenticated Bybit request requires a mode-specific credential")
            if any(not isinstance(credentials[name], str) or not credentials[name] for name in credentials):
                raise BybitError("Invalid Bybit credential")
            expected = str(self.config.get("account_id", "")).strip()
            if expected and credentials["account_id"] != expected:
                raise BybitError("Bybit credential account does not match configured account_id")
            self._credentials = {key: credentials[key] for key in ("api_key", "api_secret", "account_id")}
        return self._credentials

    def _transport_request(self, method, url, headers, body):
        transport = self.transport
        if hasattr(transport, "request"):
            response = transport.request(method, url, headers=headers, body=body, timeout=self.request_timeout)
        else:
            response = transport(method, url, headers=headers, body=body, timeout=self.request_timeout)
        if isinstance(response, bytes):
            response = json.loads(response.decode("utf-8"))
        elif isinstance(response, str):
            response = json.loads(response)
        if not isinstance(response, dict):
            raise BybitError("Bybit transport returned a non-object response")
        return response

    def _verify_account_identity(self, *, category=None, write=False, position=False):
        if self.mode == 'paper':
            return
        now = self._now_ms()
        if self._verified_account is None or not 0 <= now - self._verified_account['at_ms'] <= 300_000:
            raw = self._request('GET', '/v5/user/query-api', private=True)
            uid = str(raw.get('userID', ''))
            expected = str(self.config.get('account_id', ''))
            if not uid or uid != expected:
                raise BybitError('Exchange-verified UID does not match the explicitly enabled account')
            bound = self._meta_get('verified_account_uid')
            if bound and bound != uid:
                raise BybitError('This ledger belongs to another account; use a separate Prism state directory')
            # The endpoint also returns apiKey/secret fields. Never retain its
            # raw response in packets, documents, logs or model context.
            self._verified_account = {'uid': uid, 'at_ms': now,
                'read_only': raw.get('readOnly') != 0, 'permissions': raw.get('permissions', {})}
            self._meta_set('verified_account_uid', uid)
        if write:
            verification = self._verified_account
            permissions = verification['permissions']
            if verification['read_only']:
                raise BybitError('Exchange API key is read-only')
            permitted = ('SpotTrade' in permissions.get('Spot', []) if category == 'spot' else
                         ('Position' if position else 'Order') in permissions.get('ContractTrade', [])
                         or 'DerivativesTrade' in permissions.get('Derivatives', []))
            if not permitted:
                raise BybitError('Exchange-verified API permission does not authorize this operation')

    @staticmethod
    def _query_string(params):
        clean = [(key, value) for key, value in params.items() if value is not None and value != ""]
        return urllib.parse.urlencode(clean, doseq=True, safe="-_.~")

    def _request(self, method, path, *, params=None, payload=None, private=False):
        self._guard_environment()
        params = params or {}
        body_text = _json(payload or {}) if method == "POST" else ""
        query = self._query_string(params) if method == "GET" else ""
        url = self.rest_url + path + ("?" + query if query else "")
        headers = {"Accept": "application/json", "User-Agent": "Prism-BybitAdapter/1.0"}
        body = body_text.encode("utf-8") if method == "POST" else None
        if method == "POST":
            headers["Content-Type"] = "application/json"
        if private and self.mode != "paper":
            credentials = self._get_credentials()
            sync_is_stale = (self._server_offset_ms is None or self._clock_sync_at_ms is None or
                             self._now_ms() - self._clock_sync_at_ms > 300_000)
            if sync_is_stale and path != "/v5/market/time":
                self.synchronize_clock()
            timestamp_ms = str(self._now_ms() + (self._server_offset_ms or 0))
            recv = str(self.recv_window)
            sign_input = timestamp_ms + credentials["api_key"] + recv + (query if method == "GET" else body_text)
            signature = hmac.new(credentials["api_secret"].encode("utf-8"), sign_input.encode("utf-8"), hashlib.sha256).hexdigest()
            headers.update({"X-BAPI-API-KEY": credentials["api_key"], "X-BAPI-TIMESTAMP": timestamp_ms,
                            "X-BAPI-RECV-WINDOW": recv, "X-BAPI-SIGN": signature})
        response = self._transport_request(method, url, headers, body)
        if "retCode" in response:
            try:
                code = int(response["retCode"])
            except (ValueError, TypeError):
                code = -1
            if code != 0:
                raise BybitAPIError(code, str(response.get("retMsg", "request failed")), endpoint=path)
            result = response.get("result", {})
            if not isinstance(result, dict):
                raise BybitError("Malformed Bybit result object")
            return result
        return response

    def synchronize_clock(self):
        """Measure exchange time against the midpoint of a local clock sample."""
        before = self._now_ms()
        response = self._request("GET", "/v5/market/time")
        after = self._now_ms()
        if "timeNano" in response:
            server_ms = int(response["timeNano"]) // 1_000_000
        elif "timeSecond" in response:
            server_ms = int(response["timeSecond"]) * 1000
        elif "time" in response:
            server_ms = int(response["time"])
        else:
            raise BybitError("Bybit server-time response is incomplete")
        self._server_offset_ms = server_ms - ((before + after) // 2)
        self._clock_sync_at_ms = after
        return {"server_time_ms": server_ms, "offset_ms": self._server_offset_ms,
                "measured_at_ms": after, "round_trip_ms": max(0, after - before)}

    def _check_category_symbol(self, category, symbol):
        if category not in SUPPORTED_CATEGORIES:
            raise ValueError("Only Bybit spot and linear categories are supported")
        if not isinstance(symbol, str) or not 3 <= len(symbol) <= 32 or not symbol.isalnum() or symbol != symbol.upper():
            raise ValueError("Use an exact uppercase Bybit symbol")
        categories = self.config.get("allowed_categories", ["spot", "linear"])
        if category not in categories:
            raise ValueError("Category is outside the configured Bybit category allowlist")

    def _check_instrument_allowlist(self, category, symbol):
        allowed = self.config.get("allowed_instruments", [])
        if {"category": category, "symbol": symbol} not in allowed:
            raise ValueError("Exact category/symbol is outside the configured Bybit instrument allowlist")

    def metadata(self, category, symbol):
        """Return normalized exact contract/spot lot and price filters."""
        self._check_category_symbol(category, symbol)
        with self.store.connection() as db:
            row = db.execute("SELECT value,updated_at_ms FROM bybit_instruments WHERE mode=? AND category=? AND symbol=?",
                             (self.metadata_mode, category, symbol)).fetchone()
        if row and 0 <= self._now_ms() - row['updated_at_ms'] <= 60_000:
            return json.loads(row['value'])
        params = {"category": category, "symbol": symbol}
        if category != 'spot':
            params['limit'] = 1000
        result = self._request("GET", "/v5/market/instruments-info",
                               params=params)
        match = next((item for item in result.get("list", []) if item.get("symbol") == symbol), None)
        if not match:
            raise ValueError(f"Bybit did not return exact {category}/{symbol} instrument metadata")
        lot = match.get("lotSizeFilter", {})
        price = match.get("priceFilter", {})
        normalized = {
            "venue": "bybit", "category": category, "symbol": symbol,
            "status": match.get("status"), "base_coin": match.get("baseCoin"),
            "quote_coin": match.get("quoteCoin"), "settle_coin": match.get("settleCoin"),
            "contract_type": match.get("contractType"),
            "tick_size": lot_value(price, "tickSize"),
            "qty_step": lot_value(lot, "qtyStep", "basePrecision"),
            "min_order_qty": lot_value(lot, "minOrderQty", "basePrecision"),
            "max_order_qty": lot_value(lot, "maxOrderQty"),
            "max_market_order_qty": lot_value(lot, "maxMktOrderQty", "maxMarketOrderQty"),
            "min_notional": lot_value(lot, "minNotionalValue", "minOrderAmt"),
            "max_limit_order_qty": lot_value(lot, "maxLimitOrderQty"),
            "max_leverage": match.get("leverageFilter", {}).get("maxLeverage"),
            "funding_interval_minutes": match.get("fundingInterval"),
            "symbol_id": match.get("symbolId"), "market_region": match.get("marketRegion"),
            "symbol_type": match.get("symbolType"), "display_name": match.get("displayName"),
            "launch_time_ms": match.get("launchTime"), "delivery_time_ms": match.get("deliveryTime"),
            "verified_at_ms": self._now_ms(),
            "raw": match,
        }
        leverage_filter = match.get("leverageFilter", {})
        if isinstance(leverage_filter, dict):
            if leverage_filter.get("minLeverage") not in (None, ""):
                normalized["min_leverage"] = leverage_filter["minLeverage"]
            if leverage_filter.get("leverageStep") not in (None, ""):
                normalized["leverage_step"] = leverage_filter["leverageStep"]
        if normalized["status"] != "Trading":
            raise ValueError(f"Bybit instrument status is {normalized['status']!r}; only Trading instruments are accepted")
        if normalized['market_region']:
            raise ValueError('TradFi instruments require a separate venue-session policy; the implemented Bybit protocol supports continuous crypto products')
        if not all(normalized[key] for key in ("tick_size", "qty_step", "min_order_qty")):
            raise ValueError("Bybit instrument metadata is missing price or quantity filters")
        if category == "linear":
            if normalized["contract_type"] != "LinearPerpetual":
                raise ValueError("Only linear USDT perpetual contracts are supported")
            if normalized["quote_coin"] != "USDT" or normalized["settle_coin"] != "USDT":
                raise ValueError("Only USDT-settled linear perpetuals are supported")
        elif normalized["quote_coin"] != "USDT":
            raise ValueError("Only non-margin USDT spot instruments are supported")
        encoded = _json(normalized)
        with self.store.connection() as db:
            db.execute("INSERT INTO bybit_instruments(mode,category,symbol,updated_at_ms,value) VALUES (?,?,?,?,?) "
                       "ON CONFLICT(mode,category,symbol) DO UPDATE SET updated_at_ms=excluded.updated_at_ms,value=excluded.value",
                       (self.metadata_mode, category, symbol, self._now_ms(), encoded))
        return normalized

    def refresh_metadata(self, category, symbol):
        """Force a fresh exchange metadata query and replace the durable cache."""
        with self.store.connection() as db:
            db.execute("DELETE FROM bybit_instruments WHERE mode=? AND category=? AND symbol=?",
                       (self.metadata_mode, category, symbol))
        return self.metadata(category, symbol)

    def normalize_quantity(self, category, symbol, quantity, *, price=None, side="Buy", market=False):
        """Round size down to the venue step and reject below-minimum orders."""
        info = self.metadata(category, symbol)
        qty = _number(quantity, "quantity", positive=True)
        step = _number(info["qty_step"], "quantity step", positive=True)
        units = (qty / step).to_integral_value(rounding=ROUND_DOWN)
        normalized = units * step
        if normalized <= 0:
            raise ValueError("Order quantity rounds below the exchange quantity step")
        minimum = _number(info["min_order_qty"], "minimum order quantity", positive=True)
        if market and info.get("max_market_order_qty"):
            maximum_key = "max_market_order_qty"
        elif not market and info.get("max_limit_order_qty"):
            maximum_key = "max_limit_order_qty"
        else:
            maximum_key = "max_order_qty"
        maximum = _number(info.get(maximum_key), "maximum order quantity", optional=True)
        if normalized < minimum:
            raise ValueError("Order quantity is below Bybit minOrderQty")
        if maximum is not None and normalized > maximum:
            raise ValueError("Order quantity exceeds Bybit maximum order quantity")
        selected_price = _number(price, "order price", positive=True, optional=True)
        if selected_price is None:
            quote = self.tickers(category, symbol)[0]
            book_price = quote["ask"] if side == "Buy" else quote["bid"]
            selected_price = _number(book_price or quote["last_price"], "ticker reference price", positive=True)
        notional_min = _number(info.get("min_notional"), "minimum notional", positive=True, optional=True)
        if notional_min is not None and normalized * selected_price < notional_min:
            raise ValueError("Order notional is below Bybit minimum notional")
        return {"quantity": _decimal_string(normalized), "price": _decimal_string(selected_price),
                "notional": float(normalized * selected_price), "category": category, "symbol": symbol,
                "qty_step": info["qty_step"], "min_order_qty": info["min_order_qty"],
                "min_notional": info["min_notional"]}

    def _normalize_price(self, category, symbol, price, side):
        info = self.metadata(category, symbol)
        value = _number(price, "price", positive=True)
        tick = _number(info["tick_size"], "tick size", positive=True)
        rounding = ROUND_DOWN if side == "Buy" else ROUND_CEILING
        normalized = (value / tick).to_integral_value(rounding=rounding) * tick
        if normalized <= 0:
            raise ValueError("Price rounds to zero at the exchange tick size")
        return _decimal_string(normalized)

    def tickers(self, category, symbol=None):
        """Return normalised ticker quotes.  Omit symbol to retrieve a category."""
        if category not in SUPPORTED_CATEGORIES:
            raise ValueError("Only spot and linear tickers are supported")
        params = {"category": category, "symbol": symbol}
        if symbol:
            self._check_category_symbol(category, symbol)
        result = self._request("GET", "/v5/market/tickers", params=params)
        rows = []
        for item in result.get("list", []):
            if symbol and item.get("symbol") != symbol:
                continue
            rows.append({"venue": "bybit", "category": category, "symbol": item.get("symbol"),
                         "bid": _float(item.get("bid1Price"), "bid", positive=True, default=0.0),
                         "ask": _float(item.get("ask1Price"), "ask", positive=True, default=0.0),
                         "bid_size": _float(item.get("bid1Size"), "bid size", positive=True, default=0.0),
                         "ask_size": _float(item.get("ask1Size"), "ask size", positive=True, default=0.0),
                         "last_price": _float(item.get("lastPrice"), "last price", positive=True),
                         "mark_price": _float(item.get("markPrice"), "mark price", positive=True, default=0.0),
                         "index_price": _float(item.get("indexPrice"), "index price", positive=True, default=0.0),
                         "turnover_24h": _float(item.get("turnover24h"), "turnover", positive=True, default=0.0),
                         "volume_24h": _float(item.get("volume24h"), "volume", positive=True, default=0.0),
                         "timestamp_ms": self._effective_now_ms(), "raw": item})
        if symbol and not rows:
            raise ValueError(f"Bybit returned no ticker for {category}/{symbol}")
        return rows

    def klines(self, category, symbol, interval, start=None, end=None, limit=200):
        """Return ascending Bybit candles with explicit open/close timestamps."""
        self._check_category_symbol(category, symbol)
        interval = timeframe(interval)
        if interval not in INTERVAL_MS:
            raise ValueError("Bybit monthly candles are unsupported by this adapter")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Bybit kline limit must be an integer from 1 to 1000")
        for label, value in (("start", start), ("end", end)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{label} must be a nonnegative epoch millisecond")
        if start is not None and end is not None and start > end:
            raise ValueError("Kline start must not be after end")
        result = self._request("GET", "/v5/market/kline", params={"category": category, "symbol": symbol,
                               "interval": interval, "start": start, "end": end, "limit": limit})
        duration = INTERVAL_MS[interval]
        cutoff = self._effective_now_ms()
        candles = []
        for row in result.get("list", []):
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                raise BybitError("Malformed Bybit kline row")
            opened = int(row[0])
            candle = {"start_ms": opened, "end_ms": opened + duration,
                      "open": _float(row[1], "candle open", positive=True),
                      "high": _float(row[2], "candle high", positive=True),
                      "low": _float(row[3], "candle low", positive=True),
                      "close": _float(row[4], "candle close", positive=True),
                      "volume": _float(row[5], "candle volume", default=0.0),
                      "turnover": _float(row[6], "candle turnover", default=0.0) if len(row) > 6 else 0.0,
                      "closed": opened + duration <= cutoff,
                      "venue": "bybit", "category": category, "symbol": symbol, "interval": interval}
            if start is not None and opened < start:
                continue
            if end is not None and opened > end:
                continue
            candles.append(candle)
        candles.sort(key=lambda row: row["start_ms"])
        return candles

    def _insert_intent(self, order_link_id, category, symbol, intent):
        now = self._now_ms()
        with self.store.connection() as db:
            existing = db.execute("SELECT * FROM bybit_orders WHERE mode=? AND order_link_id=?",
                                  (self.mode, order_link_id)).fetchone()
            if existing:
                old_intent = json.loads(existing["intent"])
                old_stable = {key: value for key, value in old_intent.items() if key not in {"referencePrice", "execution_price_cap"}}
                new_stable = {key: value for key, value in intent.items() if key not in {"referencePrice", "execution_price_cap"}}
                if old_stable != new_stable:
                    raise ValueError("orderLinkId already identifies a different durable order intent")
                return dict(existing), False
            db.execute("INSERT INTO bybit_orders(mode,order_link_id,category,symbol,status,intent,filled_qty,fee_total,realized_pnl,stop_loss,created_at_ms,updated_at_ms) "
                       "VALUES (?,?,?,?,'prepared',?,'0','0','0',?,?,?)",
                       (self.mode, order_link_id, category, symbol, _json(intent), intent.get("stopLoss"), now, now))
            row = db.execute("SELECT * FROM bybit_orders WHERE mode=? AND order_link_id=?",
                             (self.mode, order_link_id)).fetchone()
        return dict(row), True

    @staticmethod
    def _order_view(row):
        if row is None:
            return None
        result = dict(row)
        intent = json.loads(result.pop("intent"))
        result["intent"] = intent
        result["protection_verified"] = bool(result["protection_verified"])
        result["filled_quantity"] = float(result.pop("filled_qty"))
        result["category"] = intent["category"]
        result["venue"] = "bybit"
        result["symbol"] = intent["symbol"]
        result["side"] = intent["side"]
        result["order_type"] = intent["orderType"]
        result['wire_order_type'] = 'Limit' if intent.get('execution_price_cap') else intent['orderType']
        result['execution_price_cap'] = intent.get('execution_price_cap')
        result["quantity"] = float(_number(intent["qty"], "order quantity"))
        reference_price = intent.get("referencePrice", intent.get("price"))
        result["reference_price"] = float(_number(reference_price, "order reference price", positive=True)) if reference_price else None
        selected_price = intent.get("price") or reference_price
        result["price"] = float(_number(selected_price, "order price", positive=True)) if selected_price else None
        result['external'] = intent.get('external', False)
        result["reduce_only"] = intent.get("reduceOnly", False) is True
        stop_value = intent.get("stopLoss", intent.get("triggerPrice"))
        result["stop_loss"] = float(_number(stop_value, "order stop loss", positive=True)) if stop_value else None
        result["order_filter"] = intent.get("orderFilter", "Order")
        result["spot_protection"] = intent.get("spot_protection") is True
        if intent.get("parent_order_link_id"):
            result["parent_order_link_id"] = intent["parent_order_link_id"]
        for key in ("strategy_id", "snapshot_id", "rationale", "expires_at_ms",
                    "unprotected_dust_quantity", "unprotected_dust_notional"):
            if key in intent:
                result[key] = intent[key]
        for source, target in (("fee_total", "fees"), ("realized_pnl", "realized_pnl"),
                               ("average_price", "average_price")):
            value = result.get(source)
            result[target] = float(value) if value not in (None, "") else None
            if source != target:
                result.pop(source, None)
        breakdown = json.loads(result.pop('fee_breakdown', None) or '{}')
        if not breakdown and result.get('fees'):
            breakdown = {'unclassified': result['fees']}
        result['fees_by_currency'] = {key: float(value) for key, value in breakdown.items()}
        result['fee_currency'] = next(iter(breakdown)) if len(breakdown) == 1 else None
        if len(breakdown) > 1:
            result['fees'] = None  # Adding BTC fees to USDT fees would fabricate a unit.
        return result

    def _order_row(self, order_link_id):
        with self.store.connection() as db:
            return db.execute("SELECT * FROM bybit_orders WHERE mode=? AND order_link_id=?",
                              (self.mode, order_link_id)).fetchone()

    def local_order(self, order_link_id):
        """Look up one locally journaled order without reconciling the venue."""
        if not isinstance(order_link_id, str) or not order_link_id:
            return None
        return self._order_view(self._order_row(order_link_id))

    def accounting_revision(self):
        """Return durable per-mode order and execution revisions for safe cache keys.

        SQLite triggers advance these counters for supported writes and direct
        durable row changes. Timestamp-only order refreshes do not invalidate
        accounting reconstruction; late executions and ownership/fill updates do.
        """
        with self.store.connection() as db:
            row = db.execute('SELECT orders,executions FROM bybit_ledger_revision WHERE mode=?',
                             (self.mode,)).fetchone()
        return {'orders': int(row['orders']), 'executions': int(row['executions'])} if row else {
            'orders': 0, 'executions': 0}

    def transaction_revision(self):
        """Return the durable transaction-ledger revision for this adapter mode."""
        with self.store.connection() as db:
            row = db.execute('SELECT revision FROM bybit_transaction_revision WHERE mode=?',
                             (self.mode,)).fetchone()
        return int(row['revision']) if row else 0

    def orders(self, *, status=None, category=None, symbol=None, limit=500):
        """Inspect the durable order ledger, including unknown outcomes."""
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError("Order ledger limit must be from 1 to 5000")
        clauses, values = ["mode=?"], [self.mode]
        for field, value in (("status", status), ("category", category), ("symbol", symbol)):
            if value is not None:
                clauses.append(field + "=?")
                values.append(value)
        query = "SELECT * FROM bybit_orders" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at_ms DESC LIMIT ?"
        values.append(limit)
        with self.store.connection() as db:
            rows = db.execute(query, values).fetchall()
        return [self._order_view(row) for row in rows]

    def active_orders(self, *, category=None, symbol=None):
        """Read every unresolved local order using the status/instrument index.

        This complete internal view is intentionally separate from the bounded
        ``orders`` inspection API used by the UI.
        """
        statuses = sorted(ACTIVE_ORDERS)
        clauses = ['mode=?', 'status IN (' + ','.join('?' for _ in statuses) + ')']
        values = [self.mode, *statuses]
        for field, value in (('category', category), ('symbol', symbol)):
            if value is not None:
                clauses.append(field + '=?')
                values.append(value)
        query = ('SELECT * FROM bybit_orders WHERE ' + ' AND '.join(clauses) +
                 ' ORDER BY created_at_ms,order_link_id')
        with self.store.connection() as db:
            rows = db.execute(query, values).fetchall()
        return [self._order_view(row) for row in rows]

    def recent_order_count(self, *, since_ms):
        """Count the complete local order window independently of display history."""
        if type(since_ms) is not int or since_ms < 0:
            raise ValueError('Order rate boundary must be a nonnegative millisecond timestamp')
        with self.store.connection() as db:
            return db.execute('SELECT COUNT(*) FROM bybit_orders WHERE mode=? AND created_at_ms>=?',
                              (self.mode, since_ms)).fetchone()[0]

    @staticmethod
    def _ledger_batch_size(value):
        if type(value) is not int or not 1 <= value <= 5000:
            raise ValueError('Ledger page size must be from 1 to 5000')
        return value

    @staticmethod
    def _ledger_cursor(value, label):
        if value is None:
            return None
        if (not isinstance(value, (tuple, list)) or len(value) != 2 or
                type(value[0]) is not int or value[0] < 0 or
                not isinstance(value[1], str) or not value[1]):
            raise ValueError(f'{label} cursor must be a (timestamp_ms, id) pair')
        return value[0], value[1]

    def _iter_ledger_rows(self, table, time_column, id_column, clauses, values,
                          *, batch_size, after, decode):
        """Keyset-page rows at a fixed insertion watermark; release DB locks before yields."""
        batch_size = self._ledger_batch_size(batch_size)
        cursor = self._ledger_cursor(after, f'{table} ledger')
        with self.store.connection() as db:
            # rowid is globally monotonic for these append-oriented ledgers;
            # taking the table high-water mark uses SQLite's rowid index.
            watermark = db.execute(f'SELECT COALESCE(MAX(rowid),0) FROM {table}').fetchone()[0]
        while watermark:
            page_clauses = [*clauses, 'rowid<=?']
            page_values = [*values, watermark]
            if cursor is not None:
                page_clauses.append(f'({time_column},{id_column})>(?,?)')
                page_values.extend(cursor)
            query = (f'SELECT * FROM {table} WHERE ' +
                     ' AND '.join(page_clauses) + f' ORDER BY {time_column},{id_column} LIMIT ?')
            with self.store.connection() as db:
                rows = db.execute(query, [*page_values, batch_size]).fetchall()
            if not rows:
                return
            for row in rows:
                cursor = (int(row[time_column]), str(row[id_column]))
                yield decode(row)
            if len(rows) < batch_size:
                return

    def iter_orders(self, *, batch_size=1000, category=None, symbol=None, after=None):
        """Iterate rows present at start, in creation order; mutable fields may refresh per page."""
        batch_size = self._ledger_batch_size(batch_size)
        self._ledger_cursor(after, 'bybit_orders ledger')
        clauses, values = ['mode=?'], [self.mode]
        for field, value in (('category', category), ('symbol', symbol)):
            if value is not None:
                clauses.append(field + '=?')
                values.append(value)
        return self._iter_ledger_rows('bybit_orders', 'created_at_ms', 'order_link_id',
            clauses, values, batch_size=batch_size, after=after, decode=self._order_view)

    def submit_order(self, intent=None, **kwargs):
        """Persist an orderLinkId intent, then issue exactly one create request.

        ``intent`` is a mapping, or fields can be passed as keywords.  Common
        fields: category, symbol, side, quantity/qty, order_type/orderType,
        price, order_link_id/orderLinkId, reduce_only/reduceOnly, stop_loss.
        Repeated link IDs return the saved outcome and are never resubmitted.
        """
        values = dict(intent or {})
        values.update(kwargs)
        category, symbol = values.get("category"), values.get("symbol")
        self._check_category_symbol(category, symbol)
        self._check_instrument_allowlist(category, symbol)
        self._guard_environment()
        if self.mode != "paper" and self.config.get("enabled") is not True:
            raise BybitError("Bybit execution is disabled by the adapter configuration")
        self._verify_account_identity(category=category, write=True)
        # Trading status and lot filters can change after a previous cache
        # read; refresh exact instrument metadata before sizing a new order.
        self.refresh_metadata(category, symbol)
        side = values.get("side")
        if side not in {"Buy", "Sell"}:
            raise ValueError("Order side must be Buy or Sell")
        order_type = values.get("order_type", values.get("orderType", "Market"))
        if order_type not in {"Market", "Limit"}:
            raise ValueError("Only Market and Limit orders are supported")
        reduce_only = values.get("reduce_only", values.get("reduceOnly", False))
        if type(reduce_only) is not bool:
            raise ValueError("reduce_only must be an explicit boolean")
        if category == "spot" and side == "Sell" and not reduce_only:
            raise ValueError("Spot sell must be marked reduce_only; short spot exposure is unsupported")
        if category == "spot" and reduce_only and side != "Sell":
            raise ValueError("Spot reduce_only orders must sell the owned base asset")
        if category == "linear" and side == "Sell" and not reduce_only:
            raise ValueError("Linear short exposure is unsupported by this adapter")
        supplied_stop = values.get("stop_loss", values.get("stopLoss"))
        if category == "spot" and supplied_stop is not None:
            raise ValueError("Spot stop-loss attachment is outside the supported adapter protocol")
        if reduce_only and supplied_stop is not None:
            raise ValueError("Reduce-only orders cannot attach a new stop-loss")
        qty_value = values.get("quantity", values.get("qty"))
        raw_price = values.get("price", values.get("limit_price"))
        price = None
        if order_type == "Limit":
            if raw_price is None:
                raise ValueError("Limit order requires a price")
            price = self._normalize_price(category, symbol, raw_price, side)
        else:
            if raw_price is not None:
                raise ValueError("Market orders must not supply a price")
        market_unit = values.get("market_unit", "baseCoin")
        if category == "spot" and order_type == "Market" and market_unit != "baseCoin":
            raise ValueError("Spot market orders use baseCoin quantity in the supported protocol")
        sizing = self.normalize_quantity(category, symbol, qty_value, price=price, side=side, market=order_type == "Market")
        price = sizing["price"] if order_type == "Limit" else None
        stop_loss = supplied_stop
        if category == "linear" and not reduce_only:
            if stop_loss is None:
                raise ValueError("Linear opening orders require an exchange-side stop_loss")
            stop_loss = self._normalize_price(category, symbol, stop_loss, "Sell")
            reference = _number(sizing["price"], "reference price", positive=True)
            if side == "Buy" and _number(stop_loss, "stop loss", positive=True) >= reference:
                raise ValueError("Long linear stop_loss must be below current reference price")
        elif stop_loss is not None:
            stop_loss = self._normalize_price(category, symbol, stop_loss, side)
        if category == "linear":
            self._assert_linear_one_way(symbol)
        order_link_id = values.get("order_link_id", values.get("orderLinkId")) or ("prism-" + uuid.uuid4().hex[:26])
        if not isinstance(order_link_id, str) or not ORDER_LINK_PATTERN.fullmatch(order_link_id):
            raise ValueError("orderLinkId must be 1–36 letters, digits, underscores, or hyphens")
        time_in_force = values.get("time_in_force", values.get("timeInForce", "IOC" if order_type == "Market" else "GTC"))
        if time_in_force not in {"GTC", "IOC", "FOK", "PostOnly"}:
            raise ValueError("Unsupported Bybit timeInForce")
        if order_type == "Market" and time_in_force != "IOC":
            raise ValueError("Bybit market orders use IOC timeInForce")
        sl_trigger_by = values.get("sl_trigger_by", "MarkPrice")
        if stop_loss is not None and sl_trigger_by not in {"MarkPrice", "IndexPrice", "LastPrice"}:
            raise ValueError("Unsupported stop-loss trigger price")
        # Intent is an allowlisted structure: caller credential-like fields and
        # arbitrary objects cannot enter the durable journal.
        clean_intent = {"category": category, "symbol": symbol, "side": side,
                        "orderType": order_type, "qty": sizing["quantity"],
                        "referencePrice": sizing["price"],
                        "price": price, "reduceOnly": reduce_only,
                        "timeInForce": time_in_force, "stopLoss": stop_loss}
        if order_type == 'Market':
            # Translate a market intent into an absolute-price bounded IOC.
            # The exchange cannot fill beyond this host-checked quote envelope
            # even if the book moves after the last host snapshot.
            bps = _number(self.live_config.get('max_slippage_bps', 20), 'slippage limit', positive=True)
            reference = _number(sizing['price'], 'reference price', positive=True)
            cap = reference * (Decimal('1') + (bps if side == 'Buy' else -bps) / Decimal('10000'))
            decision = self.store.snapshot(values['snapshot_id']) if values.get('snapshot_id') else None
            if decision:
                measured = decision['value'].get('quote', {}).get('ask' if side == 'Buy' else 'bid')
                if measured is not None:
                    decision_cap = _number(measured, 'decision quote', positive=True) * (
                        Decimal('1') + (bps if side == 'Buy' else -bps) / Decimal('10000'))
                    cap = min(cap, decision_cap) if side == 'Buy' else max(cap, decision_cap)
            clean_intent['execution_price_cap'] = self._normalize_price(category, symbol, cap, side)
        for key, limit in (("strategy_id", 256), ("snapshot_id", 256), ("rationale", 2000)):
            value = values.get(key)
            if value is not None:
                if not isinstance(value, str) or not value.strip() or len(value) > limit:
                    raise ValueError(f"Invalid order {key}")
                clean_intent[key] = value
        expires_at = values.get("expires_at_ms")
        if expires_at is not None:
            if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= 0:
                raise ValueError("Invalid order expires_at_ms")
            clean_intent["expires_at_ms"] = expires_at
        clean_intent = {key: value for key, value in clean_intent.items() if value is not None}
        _, inserted = self._insert_intent(order_link_id, category, symbol, clean_intent)
        if not inserted:
            return self.query_order(order_link_id, reconcile=False)
        payload = {"category": category, "symbol": symbol, "side": side,
                   "orderType": order_type, "qty": sizing["quantity"],
                   "timeInForce": time_in_force, "orderLinkId": order_link_id}
        if order_type == 'Market':
            payload.update(orderType='Limit', price=clean_intent['execution_price_cap'], timeInForce='IOC')
        if price is not None:
            payload["price"] = price
        if reduce_only and category == "linear":
            payload["reduceOnly"] = True
        if category == "spot":
            payload["isLeverage"] = 0
            if order_type == "Market" and payload['orderType'] == 'Market':
                payload["marketUnit"] = market_unit
        else:
            payload["positionIdx"] = 0
            if stop_loss is not None:
                payload["stopLoss"] = stop_loss
                payload["slTriggerBy"] = sl_trigger_by
                payload["tpslMode"] = "Full"
                payload["slOrderType"] = "Market"
        now = self._now_ms()
        if expires_at is not None and now >= expires_at:
            with self.store.connection() as db:
                db.execute("UPDATE bybit_orders SET status='rejected',exchange_status='LocalIntentExpired',updated_at_ms=? "
                           'WHERE mode=? AND order_link_id=?', (now, self.mode, order_link_id))
            raise BybitError('Order intent expired while exchange metadata/account checks were running')
        with self.store.connection() as db:
            db.execute("UPDATE bybit_orders SET status='submitting',last_mutation='create',updated_at_ms=? WHERE mode=? AND order_link_id=?",
                       (now, self.mode, order_link_id))
        try:
            result = self._request("POST", "/v5/order/create", payload=payload, private=self.mode != "paper")
        except BybitAPIError as exc:
            with self.store.connection() as db:
                db.execute("UPDATE bybit_orders SET status='rejected',exchange_status=?,updated_at_ms=? WHERE mode=? AND order_link_id=? "
                           "AND status NOT IN ('filled','cancelled')",
                           (str(exc.code), self._now_ms(), self.mode, order_link_id))
            raise
        except Exception as exc:
            self._mutation_unknown(order_link_id)
            raise OrderOutcomeUnknown(order_link_id) from exc
        result_id = result.get("orderId")
        with self.store.connection() as db:
            db.execute("UPDATE bybit_orders SET order_id=COALESCE(?,order_id),"
                       "status=CASE WHEN status IN ('filled','cancelled','rejected') THEN status ELSE 'acknowledged' END,"
                       "exchange_status=CASE WHEN status IN ('filled','cancelled','rejected') THEN exchange_status ELSE 'Created' END,"
                       "updated_at_ms=? WHERE mode=? AND order_link_id=?",
                       (result_id, self._now_ms(), self.mode, order_link_id))
        return self._order_view(self._order_row(order_link_id))

    def amend_order(self, order_link_id, *, price=None, quantity=None, order_id=None):
        """Amend one tracked order. A timeout is recorded as unknown, never retried."""
        self._guard_environment()
        row = self._order_row(order_link_id)
        if row is None:
            raise ValueError("Cannot amend an order that is not in the durable ledger")
        intent = json.loads(row["intent"])
        self._verify_account_identity(category=row['category'], write=True)
        if intent.get("orderType") != "Limit":
            raise ValueError("Only tracked limit orders can be amended")
        self.refresh_metadata(row["category"], row["symbol"])
        payload = {"category": row["category"], "symbol": row["symbol"], "orderLinkId": order_link_id}
        if order_id or row["order_id"]:
            payload["orderId"] = order_id or row["order_id"]
        if price is not None:
            payload["price"] = self._normalize_price(row["category"], row["symbol"], price, intent["side"])
        if quantity is not None:
            payload["qty"] = self.normalize_quantity(row["category"], row["symbol"], quantity,
                                                       price=price or intent.get("price"), side=intent["side"])["quantity"]
            if _number(payload["qty"], "amended quantity", positive=True) < _number(row["filled_qty"], "filled quantity"):
                raise ValueError("Amended order quantity cannot be below the already filled quantity")
        if price is None and quantity is None:
            raise ValueError("Amend requires price or quantity")
        next_intent = dict(intent)
        next_intent.update({key: value for key, value in payload.items() if key in {"price", "qty"}})
        if "price" in payload:
            next_intent["referencePrice"] = payload["price"]
        self._mutation_start(order_link_id, "amend", intent=next_intent)
        try:
            result = self._request("POST", "/v5/order/amend", payload=payload, private=self.mode != "paper")
        except BybitAPIError:
            self._restore_mutation(order_link_id, intent, row["status"], "amend_rejected")
            raise
        except Exception:
            self._mutation_unknown(order_link_id)
            raise OrderOutcomeUnknown(order_link_id, "Amend outcome is unknown; reconcile before changing this order again")
        with self.store.connection() as db:
            db.execute("UPDATE bybit_orders SET intent=?,last_mutation='amend_acknowledged',status='acknowledged',updated_at_ms=? WHERE mode=? AND order_link_id=?",
                       (_json(next_intent), self._now_ms(), self.mode, order_link_id))
        return self.query_order(order_link_id, reconcile=False) or {"order_link_id": order_link_id, "status": "acknowledged", "result": result}

    def cancel_order(self, order_link_id, *, order_id=None):
        """Request cancellation; a cancel acknowledgement is not terminal state."""
        self._guard_environment()
        row = self._order_row(order_link_id)
        if row is None:
            raise ValueError("Cannot cancel an order that is not in the durable ledger")
        if row["status"] in {"filled", "cancelled", "rejected"}:
            return self._order_view(row)
        self._verify_account_identity(category=row['category'], write=True)
        payload = {"category": row["category"], "symbol": row["symbol"], "orderLinkId": order_link_id}
        if order_id or row["order_id"]:
            payload["orderId"] = order_id or row["order_id"]
        self._mutation_start(order_link_id, "cancel")
        try:
            result = self._request("POST", "/v5/order/cancel", payload=payload, private=self.mode != "paper")
        except BybitAPIError:
            self._restore_mutation(order_link_id, json.loads(row["intent"]), row["status"], "cancel_rejected")
            raise
        except Exception:
            self._mutation_unknown(order_link_id)
            raise OrderOutcomeUnknown(order_link_id, "Cancel outcome is unknown; query the order before further action")
        with self.store.connection() as db:
            latest = db.execute("SELECT status FROM bybit_orders WHERE mode=? AND order_link_id=?",
                                (self.mode, order_link_id)).fetchone()
            if latest and latest["status"] not in {"filled", "cancelled", "rejected"}:
                db.execute("UPDATE bybit_orders SET status='cancel_pending',last_mutation='cancel_acknowledged',updated_at_ms=? WHERE mode=? AND order_link_id=?",
                           (self._now_ms(), self.mode, order_link_id))
        latest = self._order_row(order_link_id)
        if latest and latest["status"] in {"filled", "cancelled", "rejected"}:
            return self._order_view(latest)
        return {"order_link_id": order_link_id, "status": "cancel_pending", "acknowledgement": result,
                "filled_quantity": float(latest["filled_qty"] if latest else row["filled_qty"])}

    def _mutation_start(self, link_id, action, *, intent=None):
        with self.store.connection() as db:
            if intent is None:
                status = "cancel_pending" if action == "cancel" else "unknown"
                db.execute("UPDATE bybit_orders SET status=?,last_mutation=?,updated_at_ms=? WHERE mode=? AND order_link_id=?",
                           (status, action + "_pending", self._now_ms(), self.mode, link_id))
            else:
                db.execute("UPDATE bybit_orders SET intent=?,status='unknown',last_mutation=?,updated_at_ms=? WHERE mode=? AND order_link_id=?",
                           (_json(intent), action + "_pending", self._now_ms(), self.mode, link_id))

    def _restore_mutation(self, link_id, intent, status, marker):
        with self.store.connection() as db:
            current = db.execute("SELECT status FROM bybit_orders WHERE mode=? AND order_link_id=?",
                                 (self.mode, link_id)).fetchone()
            if current and current["status"] not in {"filled", "cancelled", "rejected"}:
                db.execute("UPDATE bybit_orders SET intent=?,status=?,last_mutation=?,updated_at_ms=? WHERE mode=? AND order_link_id=?",
                           (_json(intent), status, marker, self._now_ms(), self.mode, link_id))

    def _mutation_unknown(self, link_id):
        with self.store.connection() as db:
            db.execute("UPDATE bybit_orders SET status='unknown',updated_at_ms=? WHERE mode=? AND order_link_id=? "
                       "AND status NOT IN ('filled','cancelled','rejected')", (self._now_ms(), self.mode, link_id))

    def query_order(self, order_link_id=None, *, order_id=None, category=None, symbol=None, reconcile=True):
        """Query an order by the durable client link identity or exchange ID."""
        row = None
        if order_link_id:
            row = self._order_row(order_link_id)
        elif order_id:
            with self.store.connection() as db:
                row = db.execute("SELECT * FROM bybit_orders WHERE mode=? AND order_id=?",
                                 (self.mode, order_id)).fetchone()
        if row is None and not (category and symbol and (order_link_id or order_id)):
            return None
        if row is not None:
            category, symbol = row["category"], row["symbol"]
        params = {"category": category, "symbol": symbol}
        if row is not None:
            row_intent = json.loads(row["intent"])
            if row_intent.get("orderFilter") in {"StopOrder", "tpslOrder"}:
                params["orderFilter"] = row_intent["orderFilter"]
        if order_id or (row and row["order_id"]):
            params["orderId"] = order_id or row["order_id"]
        elif order_link_id:
            params["orderLinkId"] = order_link_id
        else:
            return self._order_view(row)
        result = self._request("GET", "/v5/order/realtime", params=params, private=self.mode != "paper")
        found = next((item for item in result.get("list", []) if
                      (not order_link_id or item.get("orderLinkId") == order_link_id) and
                      (not order_id or item.get("orderId") == order_id)), None)
        if not found and row is not None and row["status"] in {"unknown", "submitting", "cancel_pending", "acknowledged", "open", "partially_filled"}:
            history = self._request("GET", "/v5/order/history", params=params, private=self.mode != "paper")
            found = next((item for item in history.get("list", []) if
                          (not order_link_id or item.get("orderLinkId") == order_link_id) and
                          (not order_id or item.get("orderId") == order_id)), None)
        if found:
            self._apply_exchange_order(found)
        if reconcile and row is not None:
            self._reconcile_executions_for_order(row["category"], order_link_id or row["order_link_id"])
        final_link = order_link_id or (row["order_link_id"] if row else None)
        final = self._order_row(final_link) if final_link else None
        return self._order_view(final) if final else None

    def _apply_exchange_order(self, item, *, link_override=None):
        link_id, order_id = link_override or item.get("orderLinkId"), item.get("orderId")
        if not link_id and order_id:
            with self.store.connection() as db:
                found = db.execute("SELECT order_link_id FROM bybit_orders WHERE mode=? AND order_id=?",
                                   (self.mode, order_id)).fetchone()
            link_id = found[0] if found else None
        if not link_id and item.get("parentOrderLinkId"):
            parent_link = item.get("parentOrderLinkId")
            parent = self._order_row(parent_link)
            if parent and parent["category"] == item.get("category") and parent["symbol"] == item.get("symbol"):
                with self.store.connection() as db:
                    linked = db.execute("SELECT order_link_id,intent FROM bybit_orders WHERE mode=? AND category='spot' AND symbol=? "
                        "AND status NOT IN ('filled','cancelled','rejected')", (self.mode, item.get("symbol"))).fetchall()
                matching_protection = []
                for candidate in linked:
                    candidate_intent = json.loads(candidate["intent"])
                    if (candidate_intent.get("spot_protection") is True and
                            candidate_intent.get("parent_order_link_id") == parent_link and
                            candidate_intent.get("qty") == item.get("qty") and
                            candidate_intent.get("triggerPrice") == item.get("triggerPrice")):
                        matching_protection.append(candidate["order_link_id"])
                if len(matching_protection) == 1:
                    link_id = matching_protection[0]
                else:
                # Some attached conditional children are reported by their
                # parent client ID. Keep a distinct durable child identity but
                # inherit ownership only from that exact, locally recorded
                # parent. Never let the child overwrite the parent order row.
                    child_tag = hashlib.sha256(str(order_id or item.get("triggerPrice", "")).encode()).hexdigest()[:20]
                    link_id = "parent-stop-" + child_tag
        if not link_id and order_id:
            link_id = 'external-' + hashlib.sha256(order_id.encode()).hexdigest()[:24]
        if not link_id:
            return None
        current = self._order_row(link_id)
        if current is None:
            # Account-wide exposure includes orders created outside Prism.
            # Import them as unowned; an execution agent cannot commandeer them.
            if item.get('category') not in SUPPORTED_CATEGORIES or not item.get('symbol') or not item.get('qty'):
                return None
            reference = next((item.get(k) for k in ('price', 'lastPriceOnCreated', 'avgPrice', 'triggerPrice')
                              if item.get(k) and _number(item[k], k) > 0), None)
            parent_link = item.get('parentOrderLinkId')
            parent = self._order_row(parent_link) if parent_link else None
            parent_intent = json.loads(parent["intent"]) if parent else {}
            effective_filter = item.get("orderFilter") or (
                "StopOrder" if item.get("category") == "spot" and item.get("orderStatus") in {"Untriggered", "Triggered"}
                and item.get("triggerPrice") else "Order")
            is_spot_stop = (item.get('category') == 'spot' and item.get('side') == 'Sell' and
                            effective_filter in {'StopOrder', 'tpslOrder'})
            inherited_owner = bool(parent and parent_intent.get('strategy_id') and not parent_intent.get('external'))
            intent = {'category': item['category'], 'symbol': item['symbol'], 'side': item.get('side', 'Buy'),
                'orderType': item.get('orderType', 'Limit'), 'qty': item['qty'],
                'referencePrice': reference, 'price': reference, 'reduceOnly': item.get('reduceOnly') is True,
                'external': not inherited_owner, 'parent_order_link_id': parent_link,
                'orderFilter': effective_filter,
                'spot_protection': is_spot_stop and inherited_owner}
            if is_spot_stop and inherited_owner:
                intent['reduceOnly'] = True
            if inherited_owner:
                intent['strategy_id'] = parent_intent['strategy_id']
                if parent_intent.get('snapshot_id'):
                    intent['snapshot_id'] = parent_intent['snapshot_id']
                intent['rationale'] = 'Exchange conditional child linked to a locally attributed parent'
            elif not parent_link:
                intent.pop('parent_order_link_id', None)
            self._insert_intent(link_id, item['category'], item['symbol'], intent)
            current = self._order_row(link_id)
        exchange_status = item.get("orderStatus", "")
        mapped = {"Created": "acknowledged", "New": "open", "PartiallyFilled": "partially_filled",
                  "Filled": "filled", "Cancelled": "cancelled", "PartiallyFilledCanceled": "cancelled",
                  "Rejected": "rejected", "Deactivated": "cancelled", "Untriggered": "open",
                  "Triggered": "open"}.get(exchange_status)
        old_status = current["status"]
        filled = _number(item.get("cumExecQty", current["filled_qty"] or "0"), "cumulative fill quantity")
        prior_filled = _number(current["filled_qty"] or "0", "cumulative fill quantity")
        filled = max(filled, prior_filled)
        if mapped == "filled" or (_number(current["intent"] and json.loads(current["intent"]).get("qty", "0"), "order quantity") > 0 and
                                  filled >= _number(json.loads(current["intent"]).get("qty", "0"), "order quantity")):
            mapped = "filled"
        elif old_status in {"cancelled", "rejected", "filled"}:
            mapped = old_status
        elif mapped is None:
            mapped = old_status
        avg = item.get("avgPrice") or current["average_price"]
        now = self._now_ms()
        with self.store.connection() as db:
            db.execute("UPDATE bybit_orders SET order_id=COALESCE(?,order_id),status=?,exchange_status=?,filled_qty=?,average_price=?,updated_at_ms=? WHERE mode=? AND order_link_id=?",
                       (order_id, mapped, exchange_status, _decimal_string(filled), avg, now, self.mode, link_id))
        return self._order_view(self._order_row(link_id))

    def _apply_execution(self, item):
        exec_id = item.get("execId")
        if not isinstance(exec_id, str) or not exec_id:
            raise BybitError("Bybit execution has no execId")
        category = item.get("category")
        symbol = item.get("symbol")
        if category not in SUPPORTED_CATEGORIES or not isinstance(symbol, str):
            raise BybitError("Unsupported execution category or symbol")
        exec_type = item.get("execType", "Trade")
        financial_only = exec_type in {"Funding", "Settle", "Settlement"}
        qty = _number(item.get("execQty", "0" if financial_only else None), "execution quantity",
                      positive=not financial_only)
        price = _number(item.get("execPrice", "0" if financial_only else None), "execution price",
                        positive=not financial_only)
        if qty < 0 or price < 0:
            raise BybitError("Bybit execution size and price cannot be negative")
        fee = _number(item.get("execFee", "0"), "execution fee")
        pnl = _number(item.get("execPnl", "0"), "execution realized P&L")
        executed_at = int(item.get("execTime") or item.get("execTimeMs") or self._now_ms())
        link_id = item.get("orderLinkId")
        if not link_id and item.get("orderId"):
            with self.store.connection() as db:
                row = db.execute("SELECT order_link_id FROM bybit_orders WHERE mode=? AND order_id=?",
                                 (self.mode, item["orderId"])).fetchone()
            link_id = row[0] if row else None
        raw = _json(item)
        with self.store.connection() as db:
            cursor = db.execute("INSERT OR IGNORE INTO bybit_executions(mode,exec_id,order_link_id,order_id,category,symbol,qty,price,fee,fee_currency,realized_pnl,exec_time_ms,value) "
                                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (self.mode, exec_id, link_id, item.get("orderId"), category, symbol, _decimal_string(qty),
                                  _decimal_string(price), _decimal_string(fee), item.get("feeCurrency"),
                                  _decimal_string(pnl), executed_at, raw))
            if cursor.rowcount == 0:
                # execId is Bybit's idempotency key; duplicate delivery is a no-op.
                return False
            if link_id and not financial_only:
                order = db.execute("SELECT * FROM bybit_orders WHERE mode=? AND order_link_id=?",
                                   (self.mode, link_id)).fetchone()
                if order:
                    # Order notifications carry cumulative quantities and can
                    # arrive before the corresponding execution notifications.
                    # Sum the deduplicated execution ledger, never add a fill to
                    # an already reported cumulative order quantity.
                    fills = db.execute('SELECT qty,price,fee,fee_currency,realized_pnl,value FROM bybit_executions '
                                       'WHERE mode=? AND order_link_id=?', (self.mode, link_id)).fetchall()
                    fills = [r for r in fills if json.loads(r['value']).get('execType', 'Trade') not in {'Funding', 'Settle', 'Settlement'}]
                    known_qty = sum((_number(r['qty'], 'executed quantity') for r in fills), Decimal('0'))
                    known_value = sum((_number(r['qty'], 'executed quantity') * _number(r['price'], 'executed price') for r in fills), Decimal('0'))
                    reported_qty = _number(order['filled_qty'] or '0', 'reported cumulative quantity')
                    total_qty = max(known_qty, reported_qty)
                    average = known_value / known_qty if known_qty >= reported_qty else _number(order['average_price'], 'reported average price', optional=True)
                    total_fee = sum((_number(r['fee'], 'execution fee') for r in fills), Decimal('0'))
                    fee_breakdown = {}
                    for fill in fills:
                        currency = fill['fee_currency'] or 'unclassified'
                        fee_breakdown[currency] = fee_breakdown.get(currency, Decimal('0')) + _number(fill['fee'], 'execution fee')
                    total_pnl = sum((_number(r['realized_pnl'], 'execution P&L') for r in fills), Decimal('0'))
                    intent = json.loads(order["intent"])
                    target = _number(intent["qty"], "order quantity")
                    if total_qty >= target:
                        status = "filled"
                    elif order["status"] in {"cancelled", "rejected", "filled"}:
                        status = order["status"]
                    else:
                        status = "partially_filled"
                    db.execute("UPDATE bybit_orders SET status=?,filled_qty=?,average_price=?,fee_total=?,realized_pnl=?,order_id=COALESCE(?,order_id),updated_at_ms=? WHERE mode=? AND order_link_id=?",
                               (status, _decimal_string(total_qty), _decimal_string(average), _decimal_string(total_fee),
                                _decimal_string(total_pnl), item.get("orderId"), self._now_ms(), self.mode, link_id))
                    db.execute('UPDATE bybit_orders SET fee_breakdown=? WHERE mode=? AND order_link_id=?',
                        (_json({key: _decimal_string(value) for key, value in fee_breakdown.items()}), self.mode, link_id))
        return True

    def executions(self, *, order_link_id=None, limit=1000):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("Execution ledger limit must be from 1 to 10000")
        with self.store.connection() as db:
            if order_link_id:
                rows = db.execute("SELECT value FROM bybit_executions WHERE mode=? AND order_link_id=? ORDER BY exec_time_ms LIMIT ?",
                                  (self.mode, order_link_id, limit)).fetchall()
            else:
                rows = db.execute("SELECT value FROM bybit_executions WHERE mode=? ORDER BY exec_time_ms LIMIT ?",
                                  (self.mode, limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def iter_executions(self, *, batch_size=1000, category=None, symbol=None, after=None):
        """Iterate the complete local execution snapshot by (time, execId)."""
        batch_size = self._ledger_batch_size(batch_size)
        self._ledger_cursor(after, 'bybit_executions ledger')
        clauses, values = ['mode=?'], [self.mode]
        for field, value in (('category', category), ('symbol', symbol)):
            if value is not None:
                clauses.append(field + '=?')
                values.append(value)
        return self._iter_ledger_rows('bybit_executions', 'exec_time_ms', 'exec_id',
            clauses, values, batch_size=batch_size, after=after,
            decode=lambda row: json.loads(row['value']))

    def _apply_transaction(self, item):
        ident = item.get("id") or item.get("transactionId")
        if not isinstance(ident, str) or not ident:
            raise BybitError("Bybit transaction log row has no durable id")
        trans_ms = int(item.get("transactionTime", 0))
        funding = _number(item.get("funding", "0"), "funding")
        fee = _number(item.get("fee", "0"), "transaction fee")
        cash_flow = _number(item.get("cashFlow", "0"), "transaction cash flow")
        change = _number(item.get("change", "0"), "transaction change")
        with self.store.connection() as db:
            db.execute("INSERT OR IGNORE INTO bybit_transactions(mode,transaction_id,category,symbol,currency,transaction_time_ms,funding,fee,cash_flow,change_amount,value) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (self.mode, ident, item.get("category"), item.get("symbol"), item.get("currency"), trans_ms,
                        _decimal_string(funding), _decimal_string(fee), _decimal_string(cash_flow),
                        _decimal_string(change), _json(item)))
        return {"transaction_id": ident, "funding": float(funding), "fee": float(fee),
                "cash_flow": float(cash_flow), "change": float(change), "timestamp_ms": trans_ms}

    def transactions(self, *, since_ms=None, limit=5000):
        if type(limit) is not int or not 1 <= limit <= 20000:
            raise ValueError("Transaction ledger limit must be from 1 to 20000")
        with self.store.connection() as db:
            if since_ms is None:
                rows = db.execute("SELECT value FROM bybit_transactions WHERE mode=? ORDER BY transaction_time_ms LIMIT ?",
                                  (self.mode, limit)).fetchall()
            else:
                rows = db.execute("SELECT value FROM bybit_transactions WHERE mode=? AND transaction_time_ms>=? ORDER BY transaction_time_ms LIMIT ?",
                                  (self.mode, since_ms, limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def iter_transactions(self, *, batch_size=1000, since_ms=None, after=None):
        """Iterate the complete local transaction snapshot by (time, transactionId)."""
        batch_size = self._ledger_batch_size(batch_size)
        self._ledger_cursor(after, 'bybit_transactions ledger')
        clauses, values = ['mode=?'], [self.mode]
        if since_ms is not None:
            if type(since_ms) is not int or since_ms < 0:
                raise ValueError('Transaction lower bound must be nonnegative milliseconds')
            clauses.append('transaction_time_ms>=?')
            values.append(since_ms)
        return self._iter_ledger_rows('bybit_transactions', 'transaction_time_ms', 'transaction_id',
            clauses, values, batch_size=batch_size, after=after,
            decode=lambda row: json.loads(row['value']))

    def _pages(self, path, params, *, private=False, list_key="list", limit=200):
        cursor = None
        pages = 0
        while True:
            page_params = dict(params)
            page_params["limit"] = limit
            if cursor:
                page_params["cursor"] = cursor
            result = self._request("GET", path, params=page_params, private=private)
            yield result
            cursor = result.get("nextPageCursor")
            pages += 1
            if not cursor:
                break
            if pages >= MAX_CURSOR_PAGES:
                raise BybitError("Bybit pagination exceeded the safety bound")

    def _reconcile_executions_for_order(self, category, link_id):
        if self.mode == "paper" and not self._transport_injected:
            return 0
        count = 0
        for page in self._pages("/v5/execution/list", {"category": category, "orderLinkId": link_id}, private=True, limit=100):
            for item in page.get("list", []):
                count += bool(self._apply_execution(item))
        return count

    def _reconcile_trades_and_orders(self):
        if self.mode == "paper" and not self._transport_injected:
            return {"orders": 0, "executions": 0, "transactions": 0}
        order_count = execution_count = transaction_count = 0
        for category in ("spot", "linear"):
            for page in self._pages("/v5/order/realtime", {"category": category, "openOnly": 0}, private=True, limit=50):
                for item in page.get("list", []):
                    order_count += bool(self._apply_exchange_order(item))
        for row in self.active_orders():
            if row["status"] in RECONCILIATION_ORDER_STATUSES | {"open", "partially_filled"}:
                self.query_order(row["order_link_id"], reconcile=False)
        # Account-wide execution recovery catches cancellation/fill races and
        # foreign activity without querying every historical order forever.
        execution_end = self._effective_now_ms()
        last_execution = self._meta_get('execution_reconcile_ms')
        execution_start = max(0, int(last_execution) - 60_000) if last_execution else max(0, execution_end - SEVEN_DAYS_MS)
        while execution_start < execution_end:
            window_end = min(execution_end, execution_start + SEVEN_DAYS_MS)
            for category in ('spot', 'linear'):
                for page in self._pages('/v5/execution/list', {'category': category,
                        'startTime': execution_start, 'endTime': window_end}, private=True, limit=100):
                    for item in page.get('list', []):
                        execution_count += bool(self._apply_execution(item))
            execution_start = window_end
        self._meta_set('execution_reconcile_ms', execution_end)
        last_ms = self._meta_get("transaction_reconcile_ms")
        start_ms = max(0, int(last_ms) - 60_000) if last_ms else max(0, self._effective_now_ms() - SEVEN_DAYS_MS)
        end_ms = self._effective_now_ms()
        cursor_start = start_ms
        while cursor_start < end_ms:
            cursor_end = min(end_ms, cursor_start + SEVEN_DAYS_MS)
            for page in self._pages("/v5/account/transaction-log", {"accountType": "UNIFIED",
                    "startTime": cursor_start, "endTime": cursor_end}, private=True, limit=50):
                for item in page.get("list", []):
                    self._apply_transaction(item)
                    transaction_count += 1
            cursor_start = cursor_end
        if end_ms >= start_ms:
            self._meta_set("transaction_reconcile_ms", end_ms)
        self._meta_set("last_reconcile_ms", end_ms)
        return {"orders": order_count, "executions": execution_count, "transactions": transaction_count}

    def _assert_linear_one_way(self, symbol):
        if self.config.get("position_mode", "one_way") != "one_way":
            raise BybitError("Only configured one-way linear position mode is supported")
        result = self._request("GET", "/v5/position/list",
                               params={"category": "linear", "symbol": symbol},
                               private=self.mode != "paper")
        rows = result.get("list", [])
        if not rows:
            raise BybitError("Bybit did not provide a verifiable linear position mode for this symbol")
        indices = {int(row.get("positionIdx", -1)) for row in rows}
        if indices != {0}:
            raise BybitError("Only verified one-way linear position mode is supported")
        return True

    def set_leverage(self, category, symbol, leverage):
        """Set exact linear leverage only while the instrument is reconciled flat.

        The exchange acknowledgment is never treated as proof: the resulting
        one-way flat position row is read back and checked before success.
        """
        if category != 'linear':
            raise ValueError('Leverage changes are supported only for linear contracts')
        self._check_category_symbol(category, symbol)
        self._check_instrument_allowlist(category, symbol)
        if self.config.get('enabled') is not True or self.live_config.get('enabled') is not True:
            raise BybitError('Leverage changes require explicitly enabled Bybit and live execution settings')
        self._guard_environment()
        self._verify_account_identity(category='linear', write=True, position=True)

        info = self.refresh_metadata('linear', symbol)
        if len(str(leverage)) > 64:
            raise ValueError('Leverage precision is outside the supported bound')
        requested = _number(leverage, 'leverage')
        if requested.as_tuple().exponent < -12:
            raise ValueError('Leverage precision is outside the supported bound')
        maximum = _number(info.get('max_leverage'), 'venue maximum leverage', positive=True)
        minimum = _number(info.get('min_leverage', '1'), 'venue minimum leverage', positive=True)
        step_value = info.get('leverage_step')
        step = _number(step_value, 'venue leverage step', positive=True) if step_value not in (None, '') else None
        if requested < max(Decimal('1'), minimum) or requested > maximum:
            raise ValueError('Leverage is outside the exact instrument metadata limits')
        if step is not None and (requested - minimum) % step != 0:
            raise ValueError('Leverage does not match the venue leverage step')

        # Reconcile existing exchange state first. This recovers ambiguous local
        # orders and includes orders created outside Prism in the direct query.
        self._reconcile_trades_and_orders()
        active_local = {'unknown', 'submitting', 'cancel_pending', 'acknowledged', 'open', 'partially_filled'}
        if any(row['status'] in active_local for row in self.active_orders(category='linear', symbol=symbol)):
            raise BybitError('Unresolved local linear orders prevent leverage changes')

        # No orderFilter includes active and conditional futures orders, while
        # avoiding Spot-only filter values on a linear request. Any nonempty or
        # incomplete open-only result prevents a leverage mutation.
        result = self._request('GET', '/v5/order/realtime', params={
            'category': 'linear', 'symbol': symbol, 'openOnly': 0, 'limit': 50,
        }, private=self.mode != 'paper')
        if not isinstance(result.get('list'), list) or result['list'] or result.get('nextPageCursor'):
            raise BybitError('Exchange open orders or incomplete order coverage prevent leverage changes')

        self._assert_linear_one_way(symbol)
        before = self._request('GET', '/v5/position/list',
            params={'category': 'linear', 'symbol': symbol}, private=self.mode != 'paper')
        rows = [row for row in before.get('list', []) if isinstance(row, dict)]
        if not rows or any(row.get('symbol') != symbol or int(row.get('positionIdx', -1)) != 0 for row in rows):
            raise BybitError('Exact one-way linear position state could not be verified')
        if any(_number(row.get('size', '0'), 'linear position size') != 0 for row in rows):
            raise BybitError('Close the exact linear position before changing leverage')
        flat = next((row for row in rows if int(row.get('positionIdx', -1)) == 0), None)
        if flat is None or flat.get('leverage') in (None, ''):
            raise BybitError('Current linear leverage could not be read from the exchange')
        current = _number(flat['leverage'], 'current linear leverage', positive=True)
        if current == requested:
            return {'status': 'verified', 'leverage': float(requested), 'venue_max_leverage': float(maximum),
                    'symbol': symbol, 'category': 'linear', 'verification': {'source': 'position_list',
                    'position_idx': 0, 'size': 0, 'changed': False}}

        payload = {'category': 'linear', 'symbol': symbol,
                   'buyLeverage': _decimal_string(requested), 'sellLeverage': _decimal_string(requested)}
        try:
            self._request('POST', '/v5/position/set-leverage', payload=payload, private=self.mode != 'paper')
        except BybitAPIError:
            raise BybitError('Bybit rejected the leverage update; no retry was attempted') from None
        except Exception:
            # Never blindly retry a mutation whose transport outcome is unclear.
            raise BybitError('Leverage update outcome is uncertain; read back state before retrying') from None

        try:
            after = self._request('GET', '/v5/position/list',
                params={'category': 'linear', 'symbol': symbol}, private=self.mode != 'paper')
        except Exception:
            raise BybitError('Leverage update was sent but exchange readback failed; state is unverified') from None
        verified_rows = [row for row in after.get('list', []) if isinstance(row, dict)]
        actual = next((row for row in verified_rows if int(row.get('positionIdx', -1)) == 0), None)
        verified = (len(verified_rows) > 0 and all(row.get('symbol') == symbol and int(row.get('positionIdx', -1)) == 0 for row in verified_rows)
                    and all(_number(row.get('size', '0'), 'linear position size') == 0 for row in verified_rows)
                    and actual is not None and actual.get('leverage') not in (None, '')
                    and _number(actual['leverage'], 'verified linear leverage', positive=True) == requested)
        if not verified:
            raise BybitError('Leverage mutation was not verified by the exact flat position readback')
        return {'status': 'verified', 'leverage': float(requested), 'venue_max_leverage': float(maximum),
                'symbol': symbol, 'category': 'linear', 'verification': {'source': 'position_list',
                'position_idx': 0, 'size': 0, 'changed': True}}

    def reconcile_order(self, order_link_id):
        """Reconcile one durable order by the same link ID after timeout/restart."""
        row = self._order_row(order_link_id)
        if row is None:
            raise ValueError("Unknown durable orderLinkId")
        return self.query_order(order_link_id, reconcile=True)

    def reconcile(self):
        """Reconcile open/history orders, executions, and funding/cash transactions."""
        self._guard_environment()
        result = self._reconcile_trades_and_orders()
        account = self._account_snapshot(reconcile=False)
        result["account"] = account
        result["reconciliation_required"] = any(order["status"] in RECONCILIATION_ORDER_STATUSES
                                                  for order in self.active_orders())
        return result

    def _account_snapshot(self, *, reconcile):
        self._verify_account_identity()
        if reconcile:
            self._reconcile_trades_and_orders()
        self._guard_environment()
        account_result = self._request("GET", "/v5/account/info", private=self.mode != "paper")
        wallet_result = self._request("GET", "/v5/account/wallet-balance",
                                      params={"accountType": "UNIFIED"}, private=self.mode != "paper")
        account_list = account_result.get("list")
        account_info = account_list[0] if account_list else account_result
        if account_info.get('unifiedMarginStatus') not in {3, 4, 5, 6}:
            raise BybitError('Only a verified Unified trading account is supported')
        if account_info.get('marginMode') not in {'REGULAR_MARGIN', 'ISOLATED_MARGIN'}:
            raise BybitError('Unsupported or unverified account margin mode')
        account_type = self.config.get("account_type", "UNIFIED")
        if account_list and account_list[0].get("accountType") and account_list[0]["accountType"] != account_type:
            raise BybitError("Exchange account type does not match configured account_type")
        wallet_list = wallet_result.get("list", [])
        if not wallet_list:
            raise BybitError("Bybit Unified wallet balance is empty")
        wallet = wallet_list[0]
        currencies = []
        for coin in wallet.get("coin", []):
            value = {"currency": coin.get("coin"),
                     "wallet_balance": _float(coin.get("walletBalance"), "wallet balance", default=0),
                     "equity": _float(coin.get("equity"), "wallet equity", default=0),
                     "available_to_withdraw": _float(coin.get("availableToWithdraw"), "available balance", default=0),
                     "spot_borrow": _float(coin.get("spotBorrow"), "spot borrow", default=0),
                     "raw": coin}
            currencies.append(value)
        positions = []
        for page in self._pages("/v5/position/list", {"category": "linear", "settleCoin": "USDT"},
                                private=self.mode != "paper", limit=200):
            for position in page.get("list", []):
                symbol = position.get("symbol")
                if not symbol:
                    continue
                size = _float(position.get("size"), "position size", default=0)
                pos_idx = int(position.get("positionIdx", 0))
                normalized = {"category": "linear", "symbol": symbol, "position_idx": pos_idx,
                              "side": position.get("side"), "quantity": size,
                              "entry_price": _float(position.get("avgPrice"), "average entry price", default=0),
                              "mark_price": _float(position.get("markPrice"), "mark price", default=0),
                              "unrealized_pnl": _float(position.get("unrealisedPnl"), "unrealized P&L", default=0),
                              "leverage": _float(position.get("leverage"), "leverage", positive=True, default=0),
                              "stop_loss": _float(position.get("stopLoss"), "stop loss", positive=True, default=0),
                              "liq_price": _float(position.get("liqPrice"), "liquidation price", positive=True, default=0),
                              "raw": position}
                positions.append(normalized)
        configured_linear = sorted({row["symbol"] for row in self.config.get("allowed_instruments", [])
                                    if row.get("category") == "linear"})
        mode_indices = {}
        leverage_by_instrument = {}
        observed_leverages = [row["leverage"] for row in positions if row["quantity"] > 0 and row["leverage"] > 0]
        for position in positions:
            if position["quantity"] > 0:
                mode_indices.setdefault(position["symbol"], set()).add(position["position_idx"])
        for symbol in configured_linear:
            mode_result = self._request("GET", "/v5/position/list",
                                        params={"category": "linear", "symbol": symbol},
                                        private=self.mode != "paper")
            mode_rows = mode_result.get("list")
            if isinstance(mode_rows, list):
                exact_rows = [row for row in mode_rows if isinstance(row, dict) and row.get("symbol") == symbol]
                returned_rows = [row for row in mode_rows if isinstance(row, dict)]
                indices = {int(row.get("positionIdx", -1)) for row in returned_rows}
                if len(returned_rows) != len(mode_rows) or len(exact_rows) != len(mode_rows):
                    indices.add(-1)
                mode_indices.setdefault(symbol, set()).update(indices)
                leverage_values = []
                for row in exact_rows:
                    try:
                        leverage_values.append(float(_number(row.get("leverage"), "configured leverage", positive=True)))
                    except (ValueError, TypeError, OverflowError):
                        leverage_values = []
                        break
                if (exact_rows and len(exact_rows) == len(mode_rows) and
                        mode_indices.get(symbol) == {0} and leverage_values and
                        all(value == leverage_values[0] for value in leverage_values)):
                    leverage_by_instrument[f"linear:{symbol}"] = leverage_values[0]
                    observed_leverages.extend(leverage_values)
        now = self._now_ms()
        with self.store.connection() as db:
            db.execute("DELETE FROM bybit_positions WHERE mode=?", (self.mode,))
            for position in positions:
                db.execute("INSERT INTO bybit_positions(mode,category,symbol,position_idx,captured_at_ms,value) VALUES (?,?,?,?,?,?)",
                           (self.mode, position["category"], position["symbol"], position["position_idx"], now, _json(position)))
            db.execute("DELETE FROM bybit_wallets WHERE mode=?", (self.mode,))
            for currency in currencies:
                db.execute("INSERT INTO bybit_wallets(mode,currency,captured_at_ms,value) VALUES (?,?,?,?)",
                           (self.mode, currency["currency"], now, _json(currency)))
        position_rows = [row for row in positions if row["quantity"] > 0]
        if any(index in {1, 2} for indices in mode_indices.values() for index in indices):
            position_mode = "hedge"
        elif mode_indices and ((configured_linear and all(mode_indices.get(symbol) == {0} for symbol in configured_linear))
                               or (not configured_linear and all(indices == {0} for indices in mode_indices.values()))):
            position_mode = "one_way"
        else:
            # V5 has no read-only mode endpoint when there are no positions.
            # Do not turn the operator's configured preference into a claim of
            # exchange verification in the empty-position case.
            position_mode = "unverified"
        usdt = next((coin for coin in currencies if coin["currency"] == "USDT"), {})
        total_equity = _float(wallet.get("totalEquity"), "account equity", default=0)
        total_wallet = _float(wallet.get("totalWalletBalance"), "wallet total", default=0)
        available = _float(wallet.get("totalAvailableBalance"), "available balance", default=0)
        account = {"venue": "bybit", "mode": self.mode, "account_type": wallet.get("accountType", account_type),
                   "account_id": self.config.get("account_id") or (self._credentials or {}).get("account_id"),
                   "updated_at_ms": now, "equity": total_equity, "wallet_balance": total_wallet,
                   "available_balance": available, "unrealized_pnl": _float(wallet.get("totalPerpUPL"), "perpetual P&L", default=0),
                   "usdt_wallet_balance": usdt.get("wallet_balance", 0), "usdt_available_balance": usdt.get("available_to_withdraw", 0),
                   "positions": positions, "currencies": currencies, "position_mode": position_mode,
                   "leverage": max(observed_leverages) if observed_leverages else (1.0 if not configured_linear else None),
                   "leverage_by_instrument": leverage_by_instrument,
                   "protection_supported": bool(configured_linear) and position_mode == "one_way",
                   "protection_verified": bool(position_rows) and all(row["stop_loss"] > 0 for row in position_rows),
                   "reconciliation_required": any(row["status"] in RECONCILIATION_ORDER_STATUSES
                                                  for row in self.active_orders()),
                   "risk_halted": False, "raw_account": account_info, "raw_wallet": wallet}
        with self.store.connection() as db:
            db.execute("INSERT INTO bybit_account_snapshots(mode,captured_at_ms,value) VALUES (?,?,?)",
                       (self.mode, now, _json(account)))
            db.execute("DELETE FROM bybit_account_snapshots WHERE mode=? AND id NOT IN "
                       "(SELECT id FROM bybit_account_snapshots WHERE mode=? ORDER BY id DESC LIMIT 100)",
                       (self.mode, self.mode))
        return account

    def account_snapshot(self):
        """Return refreshed Unified wallet and linear position state."""
        return self._account_snapshot(reconcile=True)

    def verify_exchange_protection(self, category, symbol, expected_stop_loss, *, position_idx=0):
        """Read the exchange position and verify the actual stop-loss value."""
        if category != "linear":
            raise ValueError("Exchange position stop-loss verification is for linear contracts")
        self._check_category_symbol(category, symbol)
        expected = _number(expected_stop_loss, "expected stop loss", positive=True)
        result = self._request("GET", "/v5/position/list", params={"category": category, "symbol": symbol},
                               private=self.mode != "paper")
        rows = result.get("list", [])
        match = next((row for row in rows if int(row.get("positionIdx", -1)) == position_idx and
                      _number(row.get("size", "0"), "position size") > 0), None)
        if not match:
            return {"verified": False, "reason": "position_not_open", "category": category, "symbol": symbol}
        actual = _number(match.get("stopLoss", "0"), "exchange stop loss", positive=True, optional=True)
        verified = actual is not None and actual == expected
        if verified:
            with self.store.connection() as db:
                db.execute("UPDATE bybit_orders SET protection_verified=1,updated_at_ms=? WHERE mode=? AND category=? AND symbol=? AND stop_loss=?",
                           (self._now_ms(), self.mode, category, symbol, _decimal_string(expected)))
        return {"verified": verified, "expected_stop_loss": float(expected),
                "actual_stop_loss": float(actual) if actual is not None else None,
                "position_idx": position_idx, "category": category, "symbol": symbol}

    def normalize_spot_protection(self, symbol, quantity, stop_loss):
        """Normalize a cash Spot stop against live metadata and the measured quote."""
        self._check_category_symbol("spot", symbol)
        info = self.metadata("spot", symbol)
        if info.get("quote_coin") != "USDT" or not info.get("base_coin"):
            raise ValueError("Cash Spot protection requires an exact base/USDT instrument")
        quote = self.tickers("spot", symbol)[0]
        reference = _number(quote.get("last_price") or quote.get("bid"), "spot trigger reference", positive=True)
        normalized_stop = self._normalize_price("spot", symbol, stop_loss, "Sell")
        if _number(normalized_stop, "spot stop loss", positive=True) >= reference:
            raise ValueError("Spot sell stop must be below the current measured market price")
        sizing = self.normalize_quantity("spot", symbol, quantity, price=normalized_stop, side="Sell", market=True)
        requested = _number(quantity, "protection quantity", positive=True)
        dust = requested - _number(sizing['quantity'], 'normalized protection quantity', positive=True)
        return {"quantity": sizing["quantity"], "stop_loss": normalized_stop,
                "reference_price": _decimal_string(reference), "base_coin": info["base_coin"],
                "quote_coin": info["quote_coin"], "notional": sizing["notional"],
                "unprotected_dust_quantity": float(dust), "unprotected_dust_notional": float(dust * reference)}

    def set_exchange_stop_loss(self, category, symbol, stop_loss, *, position_idx=0, trigger_by="MarkPrice"):
        """Set linear position protection and return query verification."""
        if category != "linear":
            raise ValueError("Exchange stop loss is supported for linear contracts only")
        self._check_category_symbol(category, symbol)
        self._check_instrument_allowlist(category, symbol)
        self.refresh_metadata(category, symbol)
        self._assert_linear_one_way(symbol)
        value = self._normalize_price(category, symbol, stop_loss, "Sell")
        if trigger_by not in {"MarkPrice", "IndexPrice", "LastPrice"}:
            raise ValueError("Unsupported stop-loss trigger price")
        self._verify_account_identity(category=category, write=True, position=True)
        payload = {"category": category, "symbol": symbol, "tpslMode": "Full", "positionIdx": position_idx,
                   "stopLoss": value, "slTriggerBy": trigger_by}
        self._request("POST", "/v5/position/trading-stop", payload=payload, private=self.mode != "paper")
        return self.verify_exchange_protection(category, symbol, value, position_idx=position_idx)

    def _spot_conditional_rows(self, symbol, *, order_link_id=None, order_id=None, parent_order_link_id=None,
                               include_history=True):
        """Read actual Spot conditional state; local acknowledgements are not proof."""
        self._check_category_symbol("spot", symbol)
        params = {"category": "spot", "symbol": symbol, "orderFilter": "StopOrder", "limit": 50}
        if order_link_id:
            params["orderLinkId"] = order_link_id
        if order_id:
            params["orderId"] = order_id
        result = self._request("GET", "/v5/order/realtime", params=params, private=self.mode != "paper")
        rows = [item for item in result.get("list", []) if isinstance(item, dict)]
        matches = [item for item in rows if
                   (not order_link_id and not order_id) or
                   (order_link_id and item.get("orderLinkId") == order_link_id) or
                   (order_id and item.get("orderId") == order_id)]
        if not matches and parent_order_link_id and not order_link_id and not order_id:
            matches = [item for item in rows if item.get("parentOrderLinkId") == parent_order_link_id]
        if matches or not include_history:
            return matches
        history_params = dict(params)
        result = self._request("GET", "/v5/order/history", params=history_params, private=self.mode != "paper")
        rows = [item for item in result.get("list", []) if isinstance(item, dict)]
        matches = [item for item in rows if
                   (not order_link_id and not order_id) or
                   (order_link_id and item.get("orderLinkId") == order_link_id) or
                   (order_id and item.get("orderId") == order_id)]
        if not matches and parent_order_link_id and not order_link_id and not order_id:
            matches = [item for item in rows if item.get("parentOrderLinkId") == parent_order_link_id]
        return matches

    def _spot_order_rows(self, symbol, *, order_link_id=None, order_id=None, include_history=True):
        """Read ordinary Spot orders, including a conditional after activation."""
        params = {"category": "spot", "symbol": symbol, "orderFilter": "Order", "limit": 50}
        if order_link_id:
            params["orderLinkId"] = order_link_id
        if order_id:
            params["orderId"] = order_id
        result = self._request("GET", "/v5/order/realtime", params=params, private=self.mode != "paper")
        rows = [item for item in result.get("list", []) if isinstance(item, dict)]
        matches = [item for item in rows if
                   (not order_link_id and not order_id) or
                   (order_link_id and item.get("orderLinkId") == order_link_id) or
                   (order_id and item.get("orderId") == order_id)]
        if matches or not include_history:
            return matches
        result = self._request("GET", "/v5/order/history", params=params, private=self.mode != "paper")
        rows = [item for item in result.get("list", []) if isinstance(item, dict)]
        return [item for item in rows if
                (not order_link_id and not order_id) or
                (order_link_id and item.get("orderLinkId") == order_link_id) or
                (order_id and item.get("orderId") == order_id)]

    @staticmethod
    def _spot_order_status(item):
        return {"Created": "acknowledged", "New": "open", "PartiallyFilled": "partially_filled",
                "Untriggered": "open", "Triggered": "open", "Filled": "filled",
                "Cancelled": "cancelled", "PartiallyFilledCanceled": "cancelled",
                "Deactivated": "cancelled", "Rejected": "rejected"}.get(item.get("orderStatus"), "unknown")

    def verify_spot_exchange_protection(self, order_link_id):
        """Verify a cash Spot sell stop from Bybit's actual open conditional order."""
        row = self._order_row(order_link_id)
        if row is None:
            raise ValueError("Cannot verify a Spot protection order without its durable intent")
        intent = json.loads(row["intent"])
        if row["category"] != "spot" or intent.get("spot_protection") is not True:
            raise ValueError("Order is not a durable cash Spot protection intent")
        rows = self._spot_conditional_rows(row["symbol"], order_link_id=order_link_id)
        if not rows and intent.get("parent_order_link_id"):
            rows = self._spot_conditional_rows(row["symbol"], parent_order_link_id=intent["parent_order_link_id"])
        actual = rows[0] if len(rows) == 1 else None
        if actual:
            self._apply_exchange_order(actual, link_override=order_link_id)
            status = self._spot_order_status(actual)
            actual_qty = _number(actual.get("qty"), "conditional order quantity", positive=True)
            actual_stop = _number(actual.get("triggerPrice"), "conditional trigger price", positive=True, optional=True)
            wanted_qty = _number(intent.get("qty"), "protection quantity", positive=True)
            wanted_stop = _number(intent.get("triggerPrice"), "protection stop", positive=True)
            verified = (status == "open" and actual.get("side") == "Sell" and
                        actual.get("orderFilter", "StopOrder") == "StopOrder" and
                        actual.get("marketUnit") == "baseCoin" and
                        actual_qty == wanted_qty and actual_stop == wanted_stop and
                        actual.get("isLeverage", "0") in (0, "0", None, ""))
            return {"verified": verified, "order_link_id": order_link_id, "status": status,
                    "actual_quantity": float(actual_qty),
                    "actual_stop_loss": float(actual_stop) if actual_stop is not None else None,
                    "expected_quantity": float(wanted_qty), "expected_stop_loss": float(wanted_stop),
                    "acknowledgement_is_not_verification": True}
        return {"verified": False, "order_link_id": order_link_id,
                "status": "unknown" if len(rows) > 1 else "not_observed",
                "reason": "No unique open conditional order matched the durable client identity",
                "acknowledgement_is_not_verification": True}

    def set_spot_exchange_protection(self, symbol, quantity, stop_loss, *, order_link_id,
                                     strategy_id=None, snapshot_id=None, rationale=None,
                                     parent_order_link_id=None):
        """Create one durable cash Spot sell StopOrder and verify venue state.

        Spot ``StopOrder`` conditionals do not reserve base assets on Bybit until
        triggered. Prism therefore records them as a local reducing reservation
        and must confirm cancellation or reconcile a fill before any ordinary
        sell for the same attributed inventory.
        """
        category = "spot"
        self._check_category_symbol(category, symbol)
        self._check_instrument_allowlist(category, symbol)
        self._guard_environment()
        if self.mode != "paper" and self.config.get("enabled") is not True:
            raise BybitError("Bybit execution is disabled by the adapter configuration")
        if not isinstance(order_link_id, str) or not ORDER_LINK_PATTERN.fullmatch(order_link_id):
            raise ValueError("orderLinkId must be 1–36 letters, digits, underscores, or hyphens")
        if parent_order_link_id is not None and (not isinstance(parent_order_link_id, str) or
                                                  not ORDER_LINK_PATTERN.fullmatch(parent_order_link_id)):
            raise ValueError("parent_order_link_id must be a valid Bybit client order identity")
        self._verify_account_identity(category=category, write=True)
        info = self.refresh_metadata(category, symbol)
        if info.get("quote_coin") != "USDT" or not info.get("base_coin"):
            raise ValueError("Cash Spot protection requires an exact base/USDT instrument")
        normalized = self.normalize_spot_protection(symbol, quantity, stop_loss)
        normalized_stop = normalized["stop_loss"]
        reference = _number(normalized["reference_price"], "spot trigger reference", positive=True)
        sizing = {"quantity": normalized["quantity"]}

        # Verify a Unified cash wallet and reserve only unborrowed, unlocked
        # base inventory. A conditional StopOrder itself does not lock it.
        account_result = self._request("GET", "/v5/account/info", private=self.mode != "paper")
        account_rows = account_result.get("list", [])
        account = account_rows[0] if account_rows else account_result
        if account.get("unifiedMarginStatus") not in {3, 4, 5, 6}:
            raise BybitError("Cash Spot protection requires a verified Unified account")
        wallet = self._request("GET", "/v5/account/wallet-balance", params={"accountType": "UNIFIED"},
                               private=self.mode != "paper")
        wallet_rows = wallet.get("list", [])
        if not wallet_rows or wallet_rows[0].get("accountType") != "UNIFIED":
            raise BybitError("Cash Spot protection requires a Unified wallet balance")
        base_row = next((coin for coin in wallet_rows[0].get("coin", [])
                         if coin.get("coin") == info["base_coin"]), None)
        if not base_row:
            raise ValueError("No measured cash base-coin balance is available for this Spot protection")
        base_balance = _number(base_row.get("walletBalance", "0"), "base wallet balance")
        borrowed = _number(base_row.get("spotBorrow", "0"), "base borrowed quantity")
        locked = _number(base_row.get("locked", base_row.get("lockedAmount", "0")), "base locked quantity")
        available = max(Decimal("0"), base_balance - borrowed - locked)
        protected_qty = _number(sizing["quantity"], "protection quantity", positive=True)
        if borrowed > 0 or protected_qty > available:
            raise ValueError("Spot protection quantity exceeds unborrowed, unlocked cash base inventory")
        reserved = Decimal("0")
        with self.store.connection() as db:
            active = db.execute("SELECT order_link_id,filled_qty,intent FROM bybit_orders WHERE mode=? "
                "AND category='spot' AND symbol=? AND status NOT IN ('filled','cancelled','rejected')",
                (self.mode, symbol)).fetchall()
        for existing in active:
            if existing["order_link_id"] == order_link_id:
                continue
            existing_intent = json.loads(existing["intent"])
            if existing_intent.get("side") != "Sell":
                continue
            if existing_intent.get("external") is True:
                raise ValueError("Foreign Spot sell order conflicts with reserved base inventory")
            if existing_intent.get("reduceOnly") is True or existing_intent.get("spot_protection") is True:
                order_qty = _number(existing_intent.get("qty"), "reserved Spot sell quantity", positive=True)
                filled_qty = _number(existing["filled_qty"], "reserved Spot sell fill quantity")
                reserved += max(Decimal("0"), order_qty - filled_qty)
        if protected_qty + reserved > available:
            raise ValueError("Spot protection exceeds unreserved cash base inventory")

        clean_intent = {"category": "spot", "symbol": symbol, "side": "Sell", "orderType": "Market",
            "qty": sizing["quantity"], "referencePrice": _decimal_string(reference), "reduceOnly": True,
            "timeInForce": "IOC", "stopLoss": normalized_stop, "triggerPrice": normalized_stop,
            "orderFilter": "StopOrder", "marketUnit": "baseCoin", "isLeverage": 0,
            "spot_protection": True, 'unprotected_dust_quantity': normalized['unprotected_dust_quantity'],
            'unprotected_dust_notional': normalized['unprotected_dust_notional']}
        for key, value, limit in (("strategy_id", strategy_id, 256), ("snapshot_id", snapshot_id, 256),
                                  ("rationale", rationale, 2000)):
            if value is not None:
                if not isinstance(value, str) or not value.strip() or len(value) > limit:
                    raise ValueError(f"Invalid Spot protection {key}")
                clean_intent[key] = value
        if parent_order_link_id:
            clean_intent["parent_order_link_id"] = parent_order_link_id
        _, inserted = self._insert_intent(order_link_id, category, symbol, clean_intent)
        if not inserted:
            return {**(self._order_view(self._order_row(order_link_id)) or {}),
                    "verification": self.verify_spot_exchange_protection(order_link_id)}

        payload = {"category": "spot", "symbol": symbol, "side": "Sell", "orderType": "Market",
            "qty": sizing["quantity"], "marketUnit": "baseCoin", "timeInForce": "IOC",
            "orderLinkId": order_link_id, "isLeverage": 0, "triggerPrice": normalized_stop,
            "orderFilter": "StopOrder"}
        now = self._now_ms()
        with self.store.connection() as db:
            db.execute("UPDATE bybit_orders SET status='submitting',last_mutation='create_spot_protection',updated_at_ms=? "
                       "WHERE mode=? AND order_link_id=?", (now, self.mode, order_link_id))
        try:
            result = self._request("POST", "/v5/order/create", payload=payload, private=self.mode != "paper")
        except BybitAPIError as exc:
            with self.store.connection() as db:
                db.execute("UPDATE bybit_orders SET status='rejected',exchange_status=?,updated_at_ms=? "
                           "WHERE mode=? AND order_link_id=?", (str(exc.code), self._now_ms(), self.mode, order_link_id))
            raise
        except Exception as exc:
            self._mutation_unknown(order_link_id)
            raise OrderOutcomeUnknown(order_link_id, "Spot protection create outcome is unknown; reconcile this identity") from exc
        with self.store.connection() as db:
            db.execute("UPDATE bybit_orders SET order_id=COALESCE(?,order_id),status='acknowledged',"
                       "exchange_status='Created',updated_at_ms=? WHERE mode=? AND order_link_id=? "
                       "AND status NOT IN ('filled','cancelled','rejected')",
                       (result.get("orderId"), self._now_ms(), self.mode, order_link_id))
        verification = self.verify_spot_exchange_protection(order_link_id)
        view = self._order_view(self._order_row(order_link_id))
        return {**view, "protection_verified": verification.get("verified") is True,
                "verification": verification, "acknowledgement_is_not_verification": True}

    def cancel_spot_exchange_protection(self, order_link_id):
        """Cancel a cash Spot conditional and confirm terminal venue state."""
        self._guard_environment()
        row = self._order_row(order_link_id)
        if row is None:
            raise ValueError("Cannot cancel an untracked Spot protection order")
        intent = json.loads(row["intent"])
        if row["category"] != "spot" or intent.get("spot_protection") is not True:
            raise ValueError("Order is not a durable cash Spot protection intent")
        self._verify_account_identity(category="spot", write=True)
        observed = self._spot_conditional_rows(row["symbol"], order_link_id=order_link_id,
                                               order_id=row["order_id"])
        if not observed and intent.get("parent_order_link_id"):
            observed = self._spot_conditional_rows(row["symbol"], parent_order_link_id=intent["parent_order_link_id"])
        order_filter = "StopOrder"
        if observed:
            item = observed[0]
            self._apply_exchange_order(item, link_override=order_link_id)
            status = self._spot_order_status(item)
            if status in {"filled", "cancelled", "rejected"}:
                self._reconcile_executions_for_order("spot", order_link_id)
                return {"order_link_id": order_link_id, "status": status,
                        "filled_quantity": self._order_view(self._order_row(order_link_id))["filled_quantity"],
                        "cancellation_verified": status != "filled"}
            if item.get("orderStatus") == "Triggered":
                activated = self._spot_order_rows(row["symbol"], order_link_id=order_link_id,
                                                  order_id=row["order_id"], include_history=False)
                if activated:
                    item = activated[0]
                    self._apply_exchange_order(item, link_override=order_link_id)
                    status = self._spot_order_status(item)
                    order_filter = "Order"
                    if status in {"filled", "cancelled", "rejected"}:
                        self._reconcile_executions_for_order("spot", order_link_id)
                        latest = self._order_row(order_link_id)
                        return {"order_link_id": order_link_id, "status": latest["status"],
                                "filled_quantity": self._order_view(latest)["filled_quantity"],
                                "cancellation_verified": latest["status"] == "cancelled"}
        else:
            activated = self._spot_order_rows(row["symbol"], order_link_id=order_link_id, order_id=row["order_id"])
            if activated:
                item = activated[0]
                self._apply_exchange_order(item, link_override=order_link_id)
                status = self._spot_order_status(item)
                self._reconcile_executions_for_order("spot", order_link_id)
                if status in {"filled", "cancelled", "rejected"}:
                    latest = self._order_row(order_link_id)
                    return {"order_link_id": order_link_id, "status": latest["status"],
                            "filled_quantity": self._order_view(latest)["filled_quantity"],
                            "cancellation_verified": latest["status"] == "cancelled"}
                order_filter = "Order"
            elif row["status"] in {"filled", "cancelled", "rejected"}:
                self._reconcile_executions_for_order("spot", order_link_id)
                final = self._order_row(order_link_id)
                return {"order_link_id": order_link_id, "status": final["status"],
                        "filled_quantity": self._order_view(final)["filled_quantity"],
                        "cancellation_verified": final["status"] == "cancelled"}

        payload = {"category": "spot", "symbol": row["symbol"], "orderLinkId": order_link_id,
                   "orderFilter": order_filter}
        if row["order_id"]:
            payload["orderId"] = row["order_id"]
        with self.store.connection() as db:
            db.execute("UPDATE bybit_orders SET status='cancel_pending',last_mutation='cancel_spot_protection',updated_at_ms=? "
                       "WHERE mode=? AND order_link_id=? AND status NOT IN ('filled','cancelled','rejected')",
                       (self._now_ms(), self.mode, order_link_id))
        try:
            self._request("POST", "/v5/order/cancel", payload=payload, private=self.mode != "paper")
        except Exception:
            # A timeout or a terminal-state race is resolved from actual order
            # and execution state below. Never submit an ordinary sell here.
            pass
        try:
            rows = self._spot_conditional_rows(row["symbol"], order_link_id=order_link_id,
                                                order_id=row["order_id"])
            if not rows and intent.get("parent_order_link_id"):
                rows = self._spot_conditional_rows(row["symbol"], parent_order_link_id=intent["parent_order_link_id"])
            if rows:
                self._apply_exchange_order(rows[0], link_override=order_link_id)
                status = self._spot_order_status(rows[0])
                if status in {"filled", "cancelled", "rejected"} or rows[0].get("orderStatus") != "Triggered":
                    self._reconcile_executions_for_order("spot", order_link_id)
                    latest = self._order_row(order_link_id)
                    status = latest["status"]
                    return {"order_link_id": order_link_id, "status": status,
                            "filled_quantity": self._order_view(latest)["filled_quantity"],
                            "cancellation_verified": status == "cancelled"}
            rows = self._spot_order_rows(row["symbol"], order_link_id=order_link_id, order_id=row["order_id"])
            if rows:
                self._apply_exchange_order(rows[0], link_override=order_link_id)
                self._reconcile_executions_for_order("spot", order_link_id)
                latest = self._order_row(order_link_id)
                status = latest["status"]
                return {"order_link_id": order_link_id, "status": status,
                        "filled_quantity": self._order_view(latest)["filled_quantity"],
                        "cancellation_verified": status == "cancelled"}
            history_params = {"category": "spot", "symbol": row["symbol"], "orderFilter": "StopOrder",
                              "orderLinkId": order_link_id}
            if row["order_id"]:
                history_params["orderId"] = row["order_id"]
            history = self._request("GET", "/v5/order/history", params=history_params, private=self.mode != "paper")
            found = next((item for item in history.get("list", []) if
                          item.get("orderLinkId") == order_link_id or
                          (row["order_id"] and item.get("orderId") == row["order_id"])), None)
            if found:
                self._apply_exchange_order(found, link_override=order_link_id)
                self._reconcile_executions_for_order("spot", order_link_id)
                latest = self._order_row(order_link_id)
                status = latest["status"]
                return {"order_link_id": order_link_id, "status": status,
                        "filled_quantity": self._order_view(latest)["filled_quantity"],
                        "cancellation_verified": status == "cancelled"}
        except Exception:
            pass
        return {"order_link_id": order_link_id, "status": "unknown", "filled_quantity": float(row["filled_qty"]),
                "cancellation_verified": False}

    def _stream_auth(self, credentials):
        expires = self._now_ms() + (self._server_offset_ms or 0) + 10_000
        signature = hmac.new(credentials["api_secret"].encode("utf-8"),
                             f"GET/realtime{expires}".encode("utf-8"), hashlib.sha256).hexdigest()
        return {"op": "auth", "args": [credentials["api_key"], expires, signature]}

    def _handle_private_message(self, message):
        topic = message.get("topic", "") if isinstance(message, dict) else ""
        data = message.get("data", []) if isinstance(message, dict) else []
        if not isinstance(data, list):
            data = [data]
        changed = []
        if topic.startswith("order"):
            changed.extend(filter(None, (self._apply_exchange_order(row) for row in data if isinstance(row, dict))))
        elif topic.startswith("execution"):
            for row in data:
                if isinstance(row, dict):
                    if self._apply_execution(row):
                        changed.append({"exec_id": row.get("execId"), "order_link_id": row.get("orderLinkId")})
        elif topic.startswith("position"):
            # REST supplies complete account truth. Material private changes
            # preempt its periodic refresh without treating mark-price ticks
            # as fresh strategy decisions.
            material = [{k: r.get(k) for k in ('category', 'symbol', 'size', 'side', 'positionIdx', 'leverage', 'stopLoss', 'takeProfit')}
                        for r in data if isinstance(r, dict)]
            if self._meta_get('private_position_signature') != material:
                self._meta_set('private_position_signature', material)
                changed.extend(material)
        elif topic.startswith("wallet"):
            material = [{'accountType': r.get('accountType'), 'coins': [
                {k: coin.get(k) for k in ('coin', 'walletBalance', 'spotBorrow', 'borrowAmount', 'locked')}
                for coin in r.get('coin', [])]} for r in data if isinstance(r, dict)]
            if self._meta_get('private_wallet_signature') != material:
                self._meta_set('private_wallet_signature', material)
                changed.extend(material)
        return changed

    def _expect_ack(self, websocket, operation, *, stop_event=None):
        deadline = time.monotonic() + self.request_timeout
        while time.monotonic() <= deadline:
            if stop_event is not None and stop_event.is_set():
                return False
            message = websocket.recv_json(timeout=min(2.0, self.request_timeout))
            if not isinstance(message, dict):
                continue
            if message.get("op") == operation:
                if message.get("success") is not True:
                    raise BybitError(f"Bybit WebSocket {operation} failed: {message.get('ret_msg', message)}")
                return True
            self._handle_private_message(message)
        raise TimeoutError(f"Timed out waiting for Bybit WebSocket {operation} acknowledgement")

    def run_private_stream(self, *, on_event=None, stop_event=None, max_reconnects=None,
                           initial_backoff=1.0, maximum_backoff=30.0):
        """Run authenticated private subscriptions with heartbeat and recovery.

        Reconnects re-authenticate and restore order, execution, position and
        wallet topics, then reconcile durable orders/fills/funding and refresh
        the account snapshot before processing further messages.
        """
        self._guard_environment()
        if self.mode == "paper":
            if not self._websocket_factory_injected:
                raise BybitError("Paper private streams require an explicitly injected offline WebSocket fixture")
            # Paper mode may exercise the exact runner with an explicitly
            # supplied fake socket and fixture credentials are never loaded.
            credentials = {"api_key": "paper", "api_secret": "paper", "account_id": "paper"}
        else:
            credentials = self._get_credentials()
            if (self._server_offset_ms is None or self._clock_sync_at_ms is None or
                    self._now_ms() - self._clock_sync_at_ms > 300_000):
                self.synchronize_clock()
        topics = ["order", "execution", "position", "wallet"]
        reconnects = 0
        connected_once = False
        backoff = float(initial_backoff)
        if backoff <= 0 or maximum_backoff < backoff:
            raise ValueError("Invalid WebSocket reconnect backoff")
        while stop_event is None or not stop_event.is_set():
            websocket = None
            try:
                websocket = self.websocket_factory(self.private_ws_url)
                websocket.send_json(self._stream_auth(credentials))
                if not self._expect_ack(websocket, "auth", stop_event=stop_event):
                    break
                websocket.send_json({"op": "subscribe", "args": topics})
                if not self._expect_ack(websocket, "subscribe", stop_event=stop_event):
                    break
                self._reconcile_trades_and_orders()
                self._account_snapshot(reconcile=False)
                connected_once = True
                backoff = float(initial_backoff)
                last_ping = _clock_value(self.clock)
                last_received = time.monotonic()
                while stop_event is None or not stop_event.is_set():
                    now = _clock_value(self.clock)
                    if now - last_ping >= 20:
                        websocket.send_json({"op": "ping"})
                        last_ping = now
                    try:
                        message = websocket.recv_json(timeout=1.0)
                    except (socket.timeout, TimeoutError):
                        if time.monotonic() - last_received > 45:
                            raise ConnectionError('Private stream heartbeat expired')
                        continue
                    last_received = time.monotonic()
                    if not isinstance(message, dict):
                        continue
                    if message.get("op") == "ping":
                        websocket.send_json({"op": "pong"})
                        continue
                    if message.get("op") == "pong":
                        continue
                    changed = self._handle_private_message(message)
                    if on_event is not None:
                        on_event(message, changed)
                if stop_event is not None and stop_event.is_set():
                    break
            except (OSError, ConnectionError, TimeoutError, BybitError) as exc:
                if stop_event is not None and stop_event.is_set():
                    break
                reconnects += 1
                if max_reconnects is not None and reconnects > max_reconnects:
                    return {"connected": connected_once, "reconnects": reconnects - 1,
                            "stopped": False, "last_error": type(exc).__name__}
            finally:
                if websocket is not None:
                    try:
                        websocket.close()
                    except Exception:
                        pass
            if stop_event is not None and stop_event.is_set():
                break
            if max_reconnects is not None and reconnects > max_reconnects:
                break
            self.sleeper(backoff)
            backoff = min(maximum_backoff, backoff * 2)
        return {"connected": connected_once, "reconnects": reconnects, "stopped": True}


def lot_value(row, *names):
    for name in names:
        value = row.get(name) if isinstance(row, dict) else None
        if value not in (None, ""):
            return str(value)
    return None


__all__ = ["BybitAdapter", "BybitError", "BybitAPIError", "OrderOutcomeUnknown",
           "REST_URLS", "PRIVATE_WS_URLS"]
