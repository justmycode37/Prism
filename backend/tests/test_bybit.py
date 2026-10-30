import base64
import hashlib
import hmac
import json
from pathlib import Path
import re
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

from prism_runtime.bybit import BybitAdapter, BybitError, OrderOutcomeUnknown, REST_URLS, PRIVATE_WS_URLS
from prism_runtime.store import Store
from prism_runtime.websocket_transport import WebSocketTransport


NOW = 1_800_000_000_000


def api(result=None, *, code=0, message="OK"):
    return {"retCode": code, "retMsg": message, "result": result or {}}


def instrument(category="spot", symbol="BTCUSDT"):
    return {"symbol": symbol, "status": "Trading", "baseCoin": "BTC", "quoteCoin": "USDT",
            "settleCoin": "USDT" if category == "linear" else "", "contractType": "LinearPerpetual" if category == "linear" else "",
            "priceFilter": {"tickSize": "0.1"},
            "lotSizeFilter": {"qtyStep": "0.01", "minOrderQty": "0.01", "maxOrderQty": "100",
                              "maxMktOrderQty": "50", "minNotionalValue": "5"},
            "leverageFilter": {"maxLeverage": "10"}}


class FixtureTransport:
    def __init__(self):
        self.calls = []
        self.fail_create = False
        self.order_rows = []
        self.exec_rows = []
        self.transaction_rows = []
        self.position_rows = []
        self.position_mode_rows = [{"symbol": "BTCUSDT", "positionIdx": 0, "leverage": "1", "side": ""}]
        self.instrument_status = "Trading"

    def request(self, method, url, *, headers=None, body=None, timeout=10):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "body": body.decode() if isinstance(body, bytes) else body})
        path = urlsplit(url).path
        params = parse_qs(urlsplit(url).query)
        payload = json.loads(body) if body else {}
        if path == "/v5/market/time":
            return api({"timeNano": str(NOW * 1_000_000)})
        if path == '/v5/user/query-api':
            return api({'userID': 'fixture-account', 'readOnly': 0,
                        'permissions': {'Spot': ['SpotTrade'], 'ContractTrade': ['Order', 'Position']}})
        if path == "/v5/market/instruments-info":
            category = params.get("category", ["spot"])[0]
            symbol = params.get("symbol", ["BTCUSDT"])[0]
            row = instrument(category, symbol)
            row["status"] = self.instrument_status
            return api({"list": [row]})
        if path == "/v5/market/tickers":
            symbol = params.get("symbol", ["BTCUSDT"])[0]
            return api({"list": [{"symbol": symbol, "bid1Price": "99.9", "ask1Price": "100.1",
                                   "bid1Size": "10", "ask1Size": "12", "lastPrice": "100",
                                   "markPrice": "100", "indexPrice": "100", "turnover24h": "100000",
                                   "volume24h": "1000"}]})
        if path == "/v5/market/kline":
            return api({"list": [[str(NOW - 120_000), "99", "101", "98", "100", "5", "500"],
                                  [str(NOW - 60_000), "100", "102", "99", "101", "6", "606"]]})
        if path == "/v5/order/create":
            if self.fail_create:
                raise TimeoutError("fixture timeout after send")
            return api({"orderId": "OID-1", "orderLinkId": payload["orderLinkId"]})
        if path == "/v5/order/cancel":
            return api({"orderId": payload.get("orderId", "OID-1"), "orderLinkId": payload["orderLinkId"]})
        if path == "/v5/order/amend":
            return api({"orderId": payload.get("orderId", "OID-1"), "orderLinkId": payload["orderLinkId"]})
        if path == "/v5/order/realtime":
            link = params.get("orderLinkId", [None])[0]
            order_id = params.get("orderId", [None])[0]
            rows = [row for row in self.order_rows if (not link or row.get("orderLinkId") == link)
                    and (not order_id or row.get("orderId") == order_id)]
            return api({"list": rows})
        if path == "/v5/order/history":
            return api({"list": self.order_rows})
        if path == "/v5/execution/list":
            link = params.get("orderLinkId", [None])[0]
            return api({"list": [row for row in self.exec_rows if not link or row.get("orderLinkId") == link]})
        if path == "/v5/account/transaction-log":
            return api({"list": self.transaction_rows})
        if path == "/v5/account/info":
            return api({"unifiedMarginStatus": 3, "marginMode": "REGULAR_MARGIN"})
        if path == "/v5/account/wallet-balance":
            return api({"list": [{"accountType": "UNIFIED", "totalEquity": "1000", "totalWalletBalance": "1000",
                                   "totalAvailableBalance": "900", "totalPerpUPL": "0",
                                   "coin": [{"coin": "USDT", "walletBalance": "1000", "equity": "1000",
                                             "availableToWithdraw": "900", "spotBorrow": "0"}]}]})
        if path == "/v5/position/list":
            position_symbol = params.get("symbol", [None])[0]
            if position_symbol:
                rows = [row for row in self.position_rows if row.get("symbol") == position_symbol]
                if not rows:
                    rows = [{**row, "size": "0", "side": "", "stopLoss": "0"}
                            for row in self.position_mode_rows if row.get("symbol") == position_symbol]
                return api({"list": rows})
            return api({"list": self.position_rows})
        if path == "/v5/position/trading-stop":
            return api({"category": payload.get("category")})
        raise AssertionError(f"Unexpected fixture request: {method} {path}")


class FakeWebSocket:
    def __init__(self, messages, *, after_messages=None):
        self.messages = list(messages)
        self.after_messages = after_messages
        self.sent = []
        self.closed = False

    def send_json(self, value):
        self.sent.append(value)

    def recv_json(self, *, timeout=None):
        if self.messages:
            return self.messages.pop(0)
        if self.after_messages:
            return self.after_messages()
        raise TimeoutError("fixture receive timeout")

    def close(self):
        self.closed = True


class FakeSocket:
    def __init__(self):
        self.incoming = bytearray()
        self.sent_frames = []
        self.timeout = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def gettimeout(self):
        return self.timeout

    def sendall(self, value):
        if value.startswith(b"GET "):
            key = re.search(br"Sec-WebSocket-Key: ([^\r]+)", value).group(1)
            accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            document = json.dumps({"topic": "fixture", "data": [1]}).encode()
            self.incoming.extend(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                                 b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept +
                                 b"\r\n\r\n" + b"\x89\x04ping" + bytes((0x81, len(document))) + document)
        else:
            self.sent_frames.append(value)

    def recv(self, count):
        if not self.incoming:
            raise socket.timeout()
        size = min(count, len(self.incoming))
        result = bytes(self.incoming[:size])
        del self.incoming[:size]
        return result

    def close(self):
        self.closed = True


class NoopTLS:
    def wrap_socket(self, raw, server_hostname=None):
        return raw


class BybitAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path, clock=lambda: NOW / 1000)
        self.transport = FixtureTransport()
        self.config = {"bybit": {"mode": "paper", "enabled": False, "mainnet_enabled": False,
                                  "account_id": "fixture-account", "account_type": "UNIFIED",
                                  "position_mode": "one_way", "allowed_categories": ["spot", "linear"],
                                  "allowed_instruments": [{"category": "spot", "symbol": "BTCUSDT"},
                                                          {"category": "linear", "symbol": "BTCUSDT"}]},
                       "live_execution": {"require_exchange_protection": True}}
        self.adapter = BybitAdapter(self.store, self.config, transport=self.transport,
                                    credential_loader=lambda mode: self.fail("paper read credentials"),
                                    clock=lambda: NOW / 1000)
        self.cache_metadata("spot")
        self.cache_metadata("linear")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def cache_metadata(self, category, symbol="BTCUSDT"):
        value = instrument(category, symbol)
        meta = {"venue": "bybit", "category": category, "symbol": symbol, "status": "Trading",
                "base_coin": "BTC", "quote_coin": "USDT", "settle_coin": "USDT" if category == "linear" else "",
                "contract_type": "LinearPerpetual" if category == "linear" else "", "tick_size": "0.1",
                "qty_step": "0.01", "min_order_qty": "0.01", "max_order_qty": "100",
                "max_market_order_qty": "50", "min_notional": "5", "max_limit_order_qty": "100",
                "max_leverage": "10", "funding_interval_minutes": "480", "raw": value}
        with self.store.connection() as db:
            db.execute("INSERT OR REPLACE INTO bybit_instruments VALUES (?,?,?,?,?)",
                       ("paper", category, symbol, NOW, json.dumps(meta, separators=(",", ":"))))

    def create_spot(self, link="prism-order-1", *, order_type="Limit", **overrides):
        intent = {"category": "spot", "symbol": "BTCUSDT", "side": "Buy", "quantity": "0.50",
                  "order_type": order_type, "order_link_id": link}
        if order_type == "Limit":
            intent["price"] = "100.05"
        intent.update(overrides)
        return self.adapter.submit_order(intent)

    def test_constructor_is_lazy_and_mode_urls_are_distinct(self):
        transport = FixtureTransport()
        adapter = BybitAdapter(self.store, self.config, transport=transport,
                               credential_loader=lambda mode: self.fail("credentials are lazy"),
                               clock=lambda: NOW / 1000)
        self.assertEqual(transport.calls, [])
        self.assertEqual(len(set(REST_URLS.values())), 3)
        self.assertEqual(len(set(PRIVATE_WS_URLS.values())), 3)
        self.assertEqual(adapter.mode, "paper")

    def test_order_and_instrument_ledgers_are_scoped_to_exchange_mode(self):
        paper_order = self.create_spot(link="shared-mode-link")
        self.assertEqual(paper_order["mode"], "paper")
        self.adapter._apply_transaction({"id": "MODE-TX", "category": "linear", "symbol": "BTCUSDT",
                                          "currency": "USDT", "transactionTime": str(NOW), "funding": "0.1",
                                          "fee": "0", "cashFlow": "0", "change": "0.1"})
        config = json.loads(json.dumps(self.config))
        config["bybit"].update(mode="testnet", enabled=True)
        testnet = BybitAdapter(self.store, config, transport=self.transport,
                               credential_loader=lambda mode: {"api_key": "dummy-key", "api_secret": "dummy-secret",
                                                               "account_id": "fixture-account"},
                               clock=lambda: NOW / 1000)
        testnet_order = testnet.submit_order({"category": "spot", "symbol": "BTCUSDT", "side": "Buy",
                                              "quantity": "0.50", "order_type": "Limit", "price": "100.05",
                                              "order_link_id": "shared-mode-link"})
        self.assertEqual(testnet_order["mode"], "testnet")
        self.assertEqual(len(self.adapter.orders()), 1)
        self.assertEqual(len(testnet.orders()), 1)
        self.assertEqual(len(self.adapter.transactions()), 1)
        self.assertEqual(testnet.transactions(), [])
        with self.store.connection() as db:
            modes = {row[0] for row in db.execute("SELECT mode FROM bybit_instruments WHERE category='spot' AND symbol='BTCUSDT'")}
        self.assertEqual(modes, {"paper", "testnet"})

    def test_stdlib_rfc6455_transport_upgrades_masks_client_and_answers_ping(self):
        raw_socket = FakeSocket()
        with patch("prism_runtime.websocket_transport.socket.create_connection", return_value=raw_socket):
            websocket = WebSocketTransport("wss://fixture.example/v5/private", ssl_context=NoopTLS())
            websocket.send_json({"op": "subscribe", "args": ["order"]})
            self.assertEqual(websocket.recv_json(timeout=1), {"topic": "fixture", "data": [1]})
            self.assertTrue(raw_socket.sent_frames[0][1] & 0x80)  # Client data frame is masked.
            self.assertEqual(raw_socket.sent_frames[1][0] & 0x0F, 0xA)  # Server ping got a pong.
            websocket.close()
            self.assertTrue(raw_socket.closed)

    def test_signed_request_matches_exact_bybit_v5_wire_string(self):
        config = json.loads(json.dumps(self.config))
        config["bybit"].update(mode="testnet", enabled=True)
        adapter = BybitAdapter(self.store, config, transport=self.transport,
                               credential_loader=lambda mode: {"api_key": "dummy-key", "api_secret": "dummy-secret",
                                                               "account_id": "fixture-account"},
                               clock=lambda: NOW / 1000)
        result = adapter.submit_order({"category": "spot", "symbol": "BTCUSDT", "side": "Buy", "quantity": "0.5",
                                       "order_type": "Limit", "price": "100.1", "order_link_id": "signed-wire-1"})
        self.assertEqual(result["status"], "acknowledged")
        create = next(call for call in self.transport.calls if urlsplit(call["url"]).path == "/v5/order/create")
        headers, body = create["headers"], create["body"]
        expected = hmac.new(b"dummy-secret", (headers["X-BAPI-TIMESTAMP"] + "dummy-key" +
                          headers["X-BAPI-RECV-WINDOW"] + body).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-BAPI-SIGN"], expected)
        self.assertEqual(headers["X-BAPI-API-KEY"], "dummy-key")
        self.assertEqual(parse_qs(urlsplit(create["url"]).query), {})
        self.assertEqual(json.loads(body)["isLeverage"], 0)
        self.assertEqual(result["filled_quantity"], 0.0)
        with self.store.connection() as db:
            stored = db.execute("SELECT intent FROM bybit_orders WHERE mode='testnet' AND order_link_id='signed-wire-1'").fetchone()[0]
        self.assertNotIn("dummy-key", stored)
        self.assertNotIn("dummy-secret", stored)

    def test_ack_is_not_a_fill_and_partial_fills_fees_are_exec_idempotent(self):
        order = self.create_spot(strategy_id="strategy-v1", snapshot_id="snapshot-1",
                                 rationale="fixture entry", expires_at_ms=NOW + 30_000)
        self.assertEqual(order["status"], "acknowledged")
        self.assertEqual(order["filled_quantity"], 0.0)
        self.assertEqual(order["quantity"], 0.5)
        self.assertEqual(order["strategy_id"], "strategy-v1")
        self.assertEqual(order["snapshot_id"], "snapshot-1")
        self.assertEqual(order["reduce_only"], False)
        first = {"category": "spot", "symbol": "BTCUSDT", "orderId": "OID-1", "orderLinkId": order["order_link_id"],
                 "execId": "E-1", "execQty": "0.20", "execPrice": "100", "execFee": "0.01", "feeCurrency": "USDT",
                 "execPnl": "0", "execTime": str(NOW)}
        second = dict(first, execId="E-2", execQty="0.30", execPrice="101", execFee="0.02", execPnl="0.15")
        self.adapter._handle_private_message({"topic": "execution", "data": [first]})
        self.adapter._handle_private_message({"topic": "execution", "data": [first]})
        self.adapter._handle_private_message({"topic": "execution", "data": [second]})
        current = self.adapter.orders()[0]
        self.assertEqual(current["status"], "filled")
        self.assertAlmostEqual(current["filled_quantity"], 0.5)
        self.assertAlmostEqual(current["fees"], 0.03)
        self.assertAlmostEqual(current["realized_pnl"], 0.15)
        self.assertEqual(len(self.adapter.executions(order_link_id=order["order_link_id"])), 2)

    def test_mixed_fee_currencies_are_not_added_and_survive_restart(self):
        order = self.create_spot()
        first = {"category": "spot", "symbol": "BTCUSDT", "orderId": "OID-1",
                 "orderLinkId": order["order_link_id"], "execId": "MIXED-1",
                 "execQty": "0.2", "execPrice": "100", "execFee": "0.0002",
                 "feeCurrency": "BTC", "execTime": str(NOW)}
        second = dict(first, execId="MIXED-2", execQty="0.3", execFee="0.03", feeCurrency="USDT")
        self.adapter._handle_private_message({"topic": "execution", "data": [first, second, first]})
        restarted = BybitAdapter(self.store, self.config, transport=self.transport, clock=lambda: NOW / 1000,
                                 credential_loader=lambda mode: self.fail('paper read credentials'))
        current = restarted.orders()[0]
        self.assertIsNone(current['fees'])
        self.assertIsNone(current['fee_currency'])
        self.assertEqual(current['fees_by_currency'], {'BTC': .0002, 'USDT': .03})
        self.assertAlmostEqual(current['filled_quantity'], .5)

    def test_cancel_acknowledgement_preserves_fill_race(self):
        order = self.create_spot()
        cancel = self.adapter.cancel_order(order["order_link_id"])
        self.assertEqual(cancel["status"], "cancel_pending")
        fill = {"category": "spot", "symbol": "BTCUSDT", "orderId": "OID-1", "orderLinkId": order["order_link_id"],
                "execId": "RACE-1", "execQty": "0.50", "execPrice": "100", "execFee": "0.01", "execTime": str(NOW)}
        self.adapter._handle_private_message({"topic": "execution", "data": [fill]})
        self.adapter._handle_private_message({"topic": "order", "data": [{"orderId": "OID-1", "orderLinkId": order["order_link_id"],
                                        "orderStatus": "Cancelled", "cumExecQty": "0.50"}]})
        self.assertEqual(self.adapter.orders()[0]["status"], "filled")
        self.assertEqual(self.adapter.orders()[0]["filled_quantity"], 0.5)

    def test_timeout_survives_restart_and_same_identity_is_only_reconciled(self):
        self.transport.fail_create = True
        with self.assertRaises(OrderOutcomeUnknown) as raised:
            self.create_spot(link="prism-ambiguous-1")
        self.assertEqual(raised.exception.order_link_id, "prism-ambiguous-1")
        self.assertEqual(self.adapter.orders()[0]["status"], "unknown")
        create_count = sum(urlsplit(call["url"]).path == "/v5/order/create" for call in self.transport.calls)
        self.store.close()
        restarted_store = Store(self.path, clock=lambda: NOW / 1000)
        transport = FixtureTransport()
        transport.order_rows = [{"category": "spot", "symbol": "BTCUSDT", "orderId": "OID-1",
                                 "orderLinkId": "prism-ambiguous-1", "orderStatus": "New", "cumExecQty": "0"}]
        restarted = BybitAdapter(restarted_store, self.config, transport=transport,
                                 clock=lambda: NOW / 1000)
        result = restarted.submit_order({"category": "spot", "symbol": "BTCUSDT", "side": "Buy", "quantity": "0.50",
                                         "order_type": "Limit", "price": "100.05", "order_link_id": "prism-ambiguous-1"})
        self.assertEqual(result["status"], "open")
        transport.exec_rows = [{"category": "spot", "symbol": "BTCUSDT", "orderId": "OID-1",
                                "orderLinkId": "prism-ambiguous-1", "execId": "RESTART-FILL-1",
                                "execQty": "0.20", "execPrice": "100", "execFee": "0.01",
                                "feeCurrency": "USDT", "execTime": str(NOW)}]
        reconciled = restarted.reconcile_order("prism-ambiguous-1")
        self.assertEqual(reconciled["status"], "partially_filled")
        self.assertAlmostEqual(reconciled["filled_quantity"], 0.2)
        self.assertEqual(sum(urlsplit(call["url"]).path == "/v5/order/create" for call in transport.calls), 0)
        self.assertEqual(create_count, 1)
        restarted_store.close()
        self.store = Store(self.path, clock=lambda: NOW / 1000)

    def test_disabled_mainnet_never_calls_even_an_injected_transport(self):
        config = {"bybit": {"mode": "mainnet", "enabled": True, "mainnet_enabled": False,
                            "account_id": "fixture-account", "allowed_categories": ["spot"],
                            "allowed_instruments": [{"category": "spot", "symbol": "BTCUSDT"}]}}
        transport = FixtureTransport()
        adapter = BybitAdapter(self.store, config, transport=transport,
                               credential_loader=lambda mode: self.fail("mainnet gate runs first"),
                               clock=lambda: NOW / 1000)
        with self.assertRaisesRegex(BybitError, "Mainnet is disabled"):
            adapter.tickers("spot", "BTCUSDT")
        with self.assertRaisesRegex(BybitError, "Mainnet is disabled"):
            adapter.account_snapshot()
        self.assertEqual(transport.calls, [])

    def test_step_rounding_minimum_notional_and_exact_instrument_mismatch(self):
        sized = self.adapter.normalize_quantity("spot", "BTCUSDT", "0.159", price="100")
        self.assertEqual(sized["quantity"], "0.15")
        with self.assertRaisesRegex(ValueError, "below the exchange quantity step"):
            self.adapter.normalize_quantity("spot", "BTCUSDT", "0.009", price="100")
        with self.assertRaisesRegex(ValueError, "minimum notional"):
            self.adapter.normalize_quantity("spot", "BTCUSDT", "0.02", price="100")
        with self.assertRaisesRegex(ValueError, "instrument allowlist"):
            self.adapter.submit_order({"category": "spot", "symbol": "ETHUSDT", "side": "Buy", "quantity": "0.1",
                                       "order_type": "Limit", "price": "100", "order_link_id": "wrong-instrument"})
        with self.assertRaisesRegex(ValueError, "Only Bybit spot and linear"):
            self.adapter.metadata("inverse", "BTCUSD")

    def test_market_ticker_and_kline_normalization(self):
        quote = self.adapter.tickers("spot", "BTCUSDT")[0]
        self.assertEqual(quote["bid"], 99.9)
        bars = self.adapter.klines("spot", "BTCUSDT", "1m", start=NOW - 120_000, end=NOW - 60_000, limit=10)
        self.assertEqual([row["start_ms"] for row in bars], [NOW - 120_000, NOW - 60_000])
        self.assertTrue(all(row["closed"] for row in bars))

    def test_amend_is_durable_and_pending_until_query_confirms(self):
        order = self.create_spot(link="amend-link-1")
        amended = self.adapter.amend_order(order["order_link_id"], price="100.15", quantity="0.55")
        self.assertEqual(amended["status"], "acknowledged")
        self.assertEqual(amended["intent"]["price"], "100.1")
        self.assertEqual(amended["intent"]["qty"], "0.55")
        call = next(call for call in self.transport.calls if urlsplit(call["url"]).path == "/v5/order/amend")
        self.assertEqual(json.loads(call["body"])["orderLinkId"], "amend-link-1")

    def test_nontrading_instrument_status_is_rejected(self):
        self.transport.instrument_status = "PendingOpen"
        with self.assertRaisesRegex(ValueError, "only Trading instruments"):
            self.adapter.metadata("linear", "ETHUSDT")

    def test_unified_account_snapshot_reports_wallet_mode_and_protection_capability(self):
        snapshot = self.adapter.account_snapshot()
        self.assertEqual(snapshot["account_type"], "UNIFIED")
        self.assertEqual(snapshot["equity"], 1000.0)
        self.assertEqual(snapshot["position_mode"], "one_way")
        self.assertEqual(snapshot["leverage"], 1.0)
        self.assertTrue(snapshot["protection_supported"])
        self.assertFalse(snapshot["protection_verified"])
        self.assertFalse(snapshot["reconciliation_required"])
        self.assertEqual(snapshot["usdt_wallet_balance"], 1000.0)

    def test_linear_stop_loss_is_sent_and_query_verified(self):
        order = self.adapter.submit_order({"category": "linear", "symbol": "BTCUSDT", "side": "Buy",
                                           "quantity": "0.50", "order_type": "Limit", "price": "100.1",
                                           "stop_loss": "90.0", "order_link_id": "linear-stop-1"})
        self.assertEqual(order["status"], "acknowledged")
        create = next(call for call in self.transport.calls if urlsplit(call["url"]).path == "/v5/order/create")
        payload = json.loads(create["body"])
        self.assertEqual(payload["positionIdx"], 0)
        self.assertEqual(payload["stopLoss"], "90")
        self.assertEqual(payload["slOrderType"], "Market")
        self.assertEqual(payload["tpslMode"], "Full")
        self.transport.position_rows = [{"symbol": "BTCUSDT", "positionIdx": 0, "side": "Buy", "size": "0.5",
                                         "avgPrice": "100", "markPrice": "101", "unrealisedPnl": "0.5",
                                         "leverage": "1", "stopLoss": "90", "liqPrice": "0"}]
        checked = self.adapter.set_exchange_stop_loss("linear", "BTCUSDT", "90.0")
        self.assertTrue(checked["verified"])
        self.assertEqual(checked["actual_stop_loss"], 90.0)

    def test_linear_hedge_mode_is_rejected_before_order_send(self):
        self.transport.position_mode_rows = [{"symbol": "BTCUSDT", "positionIdx": 1, "leverage": "1", "side": "Buy"}]
        before = len(self.transport.calls)
        with self.assertRaisesRegex(BybitError, "one-way"):
            self.adapter.submit_order({"category": "linear", "symbol": "BTCUSDT", "side": "Buy",
                                       "quantity": "0.50", "order_type": "Limit", "price": "100.1",
                                       "stop_loss": "90.0", "order_link_id": "linear-hedge-1"})
        self.assertFalse(any(urlsplit(call["url"]).path == "/v5/order/create" for call in self.transport.calls[before:]))

    def test_funding_transaction_is_durable_and_duplicate_safe(self):
        transaction = {"id": "TX-FUND-1", "category": "linear", "symbol": "BTCUSDT", "currency": "USDT",
                       "transactionTime": str(NOW), "type": "SETTLEMENT", "funding": "0.42", "fee": "0",
                       "cashFlow": "0", "change": "0.42", "cashBalance": "100.42"}
        self.adapter._apply_transaction(transaction)
        self.adapter._apply_transaction(transaction)
        rows = self.adapter.transactions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["funding"], "0.42")
        funding_execution = {"category": "linear", "symbol": "BTCUSDT", "execId": "FUND-EXEC-1",
                             "execType": "Funding", "execQty": "0", "execPrice": "0",
                             "execFee": "-0.42", "execPnl": "0", "execTime": str(NOW)}
        self.assertTrue(self.adapter._apply_execution(funding_execution))
        self.assertFalse(self.adapter._apply_execution(funding_execution))
        self.assertEqual(len(self.adapter.executions()), 1)

    def test_private_stream_reauthenticates_restores_topics_and_reconciles_after_reconnect(self):
        stop = threading.Event()
        sockets = []
        def make_socket(url):
            self.assertEqual(url, PRIVATE_WS_URLS["paper"])
            if not sockets:
                reads = {"count": 0}
                def drop_after_ping():
                    reads["count"] += 1
                    if reads["count"] == 1:
                        return {"op": "ping"}
                    raise ConnectionError("fixture drop")
                socket = FakeWebSocket([{"op": "auth", "success": True}, {"op": "subscribe", "success": True}],
                                      after_messages=drop_after_ping)
            else:
                def last_message():
                    stop.set()
                    return {"op": "subscribe", "success": True}
                socket = FakeWebSocket([{"op": "auth", "success": True}, {"op": "subscribe", "success": True}],
                                      after_messages=last_message)
            sockets.append(socket)
            return socket
        counts = {"reconcile": 0, "account": 0}
        self.adapter = BybitAdapter(self.store, self.config, transport=self.transport,
                                    websocket_factory=make_socket, clock=lambda: NOW / 1000,
                                    sleeper=lambda delay: None)
        original_reconcile = self.adapter._reconcile_trades_and_orders
        original_account = self.adapter._account_snapshot
        self.adapter._reconcile_trades_and_orders = lambda: counts.__setitem__("reconcile", counts["reconcile"] + 1) or {}
        self.adapter._account_snapshot = lambda **kwargs: counts.__setitem__("account", counts["account"] + 1) or {}
        result = self.adapter.run_private_stream(stop_event=stop, max_reconnects=1)
        self.adapter._reconcile_trades_and_orders = original_reconcile
        self.adapter._account_snapshot = original_account
        self.assertEqual(result["reconnects"], 1)
        self.assertEqual(len(sockets), 2)
        for socket in sockets:
            self.assertEqual(socket.sent[0]["op"], "auth")
            self.assertEqual(socket.sent[1], {"op": "subscribe", "args": ["order", "execution", "position", "wallet"]})
            self.assertTrue(socket.closed)
        self.assertEqual(sockets[0].sent[2], {"op": "pong"})
        self.assertEqual(counts, {"reconcile": 2, "account": 2})


if __name__ == "__main__":
    unittest.main()
