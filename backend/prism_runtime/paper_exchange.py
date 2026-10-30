"""Persistent paper exchange implementing the same V5 wire lifecycle.

Only public market requests may leave this transport, through an explicitly
supplied public transport. Account/order routes are always local. A create
acknowledgement never fills an order: a later measured quote must match it.
This conservative top-of-book simulator does not model queue priority.
The v1 document remains the canonical accounting checkpoint. A separate
revisioned compact head carries the newest quote state between accounting
writes; runtime reads must use this adapter's _load/request paths so that
quotes are overlaid only while the head matches the canonical revision.
"""
from __future__ import annotations

import copy
import json
import math
import threading
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlsplit

from .domain import identity


class PaperExchange:
    def __init__(self, store, *, public_transport=None, clock=None, initial_equity=10000., fee_bps=6.):
        if not math.isfinite(initial_equity) or initial_equity <= 0 or not 0 <= fee_bps <= 100:
            raise ValueError('Invalid paper capital or fee assumption')
        self.store, self.public_transport = store, public_transport
        self.clock = clock or store._clock
        self.initial_equity, self.fee = initial_equity, fee_bps / 10000
        self._lock = threading.RLock()
        # A distinct document never reads, changes or impersonates a real account.
        self._key = 'bybit_paper_exchange_v1'
        self._head_key = 'bybit_paper_exchange_head_v1'
        self._revision_key = 'bybit_paper_exchange_revision_v1'
        self._ensure_head_storage()

    def _ensure_head_storage(self):
        """Install canonical-document invalidation and initialize its compact head."""
        with self.store.connection() as db:
            db.execute('INSERT OR IGNORE INTO cascade_meta(key,value) VALUES (?,?)',
                       (self._revision_key, '0'))
            # All normal Store writes and direct fixture SQL writes to the
            # canonical document pass through this revision trigger. The head
            # is only trusted while it names the same canonical revision.
            db.executescript(f'''
                CREATE TRIGGER IF NOT EXISTS bybit_paper_revision_insert
                AFTER INSERT ON cascade_meta WHEN NEW.key='{self._key}' BEGIN
                    UPDATE cascade_meta SET value=CAST(value AS INTEGER)+1 WHERE key='{self._revision_key}';
                END;
                CREATE TRIGGER IF NOT EXISTS bybit_paper_revision_update
                AFTER UPDATE OF value ON cascade_meta
                WHEN OLD.key='{self._key}' AND NEW.key='{self._key}' AND OLD.value IS NOT NEW.value BEGIN
                    UPDATE cascade_meta SET value=CAST(value AS INTEGER)+1 WHERE key='{self._revision_key}';
                END;
                CREATE TRIGGER IF NOT EXISTS bybit_paper_revision_delete
                AFTER DELETE ON cascade_meta WHEN OLD.key='{self._key}' BEGIN
                    UPDATE cascade_meta SET value=CAST(value AS INTEGER)+1 WHERE key='{self._revision_key}';
                END;
            ''')
            row = db.execute('SELECT 1 FROM cascade_meta WHERE key=?', (self._key,)).fetchone()
        if row is None:
            self._save({'cash': self.initial_equity, 'initial_equity': self.initial_equity,
                'orders': {}, 'executions': [], 'transactions': [], 'positions': {},
                'quotes': {}, 'quote_ids': {}, 'leverages': {}, 'counter': 0})

    @staticmethod
    def _head_for_state(state, revision):
        positions = state.get('positions')
        orders = state.get('orders')
        flat = isinstance(positions, dict) and isinstance(orders, dict)
        if flat:
            for position in positions.values():
                quantity = position.get('quantity') if isinstance(position, dict) else None
                if (type(quantity) not in (int, float) or not math.isfinite(quantity) or
                        quantity != 0):
                    flat = False
                    break
        active_count = None
        if isinstance(orders, dict):
            terminal = {'Filled', 'Cancelled', 'Rejected', 'Deactivated', 'PartiallyFilledCanceled'}
            active_count = sum(1 for order in orders.values()
                               if not isinstance(order, dict) or order.get('orderStatus') not in terminal)
        else:
            flat = False
        return {'revision': revision, 'flat': flat, 'active_order_count': active_count,
                'quotes': copy.deepcopy(state.get('quotes', {})),
                'quote_ids': copy.deepcopy(state.get('quote_ids', {}))}

    @staticmethod
    def _encode(value):
        return json.dumps(value, allow_nan=False, separators=(',', ':'))

    def _write_head(self, db, state, revision):
        head = self._head_for_state(state, revision)
        db.execute('INSERT INTO cascade_meta(key,value) VALUES (?,?) ON CONFLICT(key) '
                   'DO UPDATE SET value=excluded.value', (self._head_key, self._encode(head)))
        return head

    def _read_valid_head(self, db):
        rows = db.execute('SELECT key,value FROM cascade_meta WHERE key IN (?,?)',
                          (self._head_key, self._revision_key)).fetchall()
        values = {row['key']: row['value'] for row in rows}
        try:
            revision = int(values[self._revision_key])
            head = json.loads(values[self._head_key])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(head, dict):
            return None
        if head.get('revision') != revision:
            return None
        return head

    def _load(self):
        with self.store.connection() as db:
            # A missing head is rebuilt in this transaction. Freeze canonical
            # state and its revision together even with another Store writer.
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value FROM cascade_meta WHERE key=?', (self._key,)).fetchone()
            revision_row = db.execute('SELECT value FROM cascade_meta WHERE key=?',
                                      (self._revision_key,)).fetchone()
            if row is None:
                raise ValueError('Paper exchange canonical state is missing')
            state = json.loads(row['value'])
            revision = int(revision_row['value']) if revision_row else 0
            head = self._read_valid_head(db)
            if head is None:
                # Direct canonical edits invalidate the compact quote and
                # flatness cache. Rebuild it from the just-read state.
                head = self._write_head(db, state, revision)
        # Latest measured quotes live in the compact head between canonical
        # accounting writes. _load is the compatibility view that overlays
        # them; direct raw v1 document reads remain a checkpoint representation.
        state['quotes'] = copy.deepcopy(head.get('quotes', {}))
        state['quote_ids'] = copy.deepcopy(head.get('quote_ids', {}))
        state.setdefault('leverages', {})
        return state

    def _save(self, state):
        with self.store.connection() as db:
            db.execute('INSERT INTO cascade_meta(key,value) VALUES (?,?) ON CONFLICT(key) '
                       'DO UPDATE SET value=excluded.value', (self._key, self._encode(state)))
            row = db.execute('SELECT value FROM cascade_meta WHERE key=?',
                             (self._revision_key,)).fetchone()
            revision = int(row['value']) if row else 0
            self._write_head(db, state, revision)

    def _fast_head(self, *, require_flat=False):
        with self.store.connection() as db:
            head = self._read_valid_head(db)
        if (head is None or type(head.get('active_order_count')) is not int or
                head.get('active_order_count') != 0 or (require_flat and head.get('flat') is not True)):
            return None
        quotes, quote_ids = head.get('quotes'), head.get('quote_ids')
        if not isinstance(quotes, dict) or not isinstance(quote_ids, dict):
            return None
        return head

    @staticmethod
    def _accepted_quote(snapshot, now, state):
        category, symbol = snapshot['category'], snapshot['symbol']
        key, quote = category + ':' + symbol, snapshot.get('quote', {})
        if (not all(type(quote.get(k)) in (int, float) and math.isfinite(quote[k]) and quote[k] > 0
                    for k in ('bid', 'ask', 'timestamp_ms')) or quote['bid'] > quote['ask']
                or not 0 <= now - quote['timestamp_ms'] <= 10000):
            return None
        if (any(quote.get(field) is not None and
                (type(quote[field]) not in (int, float) or not math.isfinite(quote[field]) or quote[field] < 0)
                for field in ('bid_size', 'ask_size')) or
                any(quote.get(field) is not None and
                    (type(quote[field]) not in (int, float) or not math.isfinite(quote[field]) or
                     not 0 < quote[field] <= now)
                for field in ('book_timestamp_ms', 'ticker_timestamp_ms'))):
            return None
        digest = identity([quote['timestamp_ms'], quote])
        previous = state.get('quotes', {}).get(key, {})
        if (state.get('quote_ids', {}).get(key) == digest or
                quote['timestamp_ms'] <= previous.get('timestamp_ms', 0) or
                quote.get('book_timestamp_ms') is not None and
                quote['book_timestamp_ms'] <= (previous.get('book_timestamp_ms') or 0)):
            return None
        return key, digest, copy.deepcopy(quote)

    def _advance_flat(self, snapshots):
        """Persist quote-only head updates without parsing closed order history."""
        with self.store.connection() as db:
            # Do not label an old flat head with a newer canonical revision if
            # another connection inserts inventory or an order during a quote.
            db.execute('BEGIN IMMEDIATE')
            head = self._read_valid_head(db)
            if (head is None or head.get('flat') is not True or
                    type(head.get('active_order_count')) is not int or head.get('active_order_count') != 0 or
                    not isinstance(head.get('quotes'), dict) or not isinstance(head.get('quote_ids'), dict)):
                return None
            now = round(self.clock() * 1000)
            changed = False
            state = {'quotes': head['quotes'], 'quote_ids': head['quote_ids']}
            for snapshot in snapshots:
                accepted = self._accepted_quote(snapshot, now, state)
                if accepted is None:
                    continue
                key, digest, quote = accepted
                state['quote_ids'][key], state['quotes'][key] = digest, quote
                changed = True
            if changed:
                revision_row = db.execute('SELECT value FROM cascade_meta WHERE key=?',
                                          (self._revision_key,)).fetchone()
                head.update(revision=int(revision_row['value']), quotes=state['quotes'], quote_ids=state['quote_ids'])
                db.execute('INSERT INTO cascade_meta(key,value) VALUES (?,?) ON CONFLICT(key) '
                           'DO UPDATE SET value=excluded.value', (self._head_key, self._encode(head)))
            return []

    @staticmethod
    def _ok(result=None, code=0, message='OK'):
        return {'retCode': code, 'retMsg': message, 'result': result or {}}

    def request(self, method, url, *, headers=None, body=None, timeout=10):
        path = urlsplit(url).path
        params = dict(parse_qsl(urlsplit(url).query))
        args = json.loads(body) if body else params
        if path.startswith('/v5/market/'):
            if path == '/v5/market/tickers' and method == 'GET':
                head = self._fast_head()
                if head is None:
                    state = self._load()
                    quotes = state['quotes']
                else:
                    quotes = head['quotes']
                quote = quotes.get(args.get('category', '') + ':' + args.get('symbol', ''))
                if quote:
                    return self._ok({'list': [{'symbol': args['symbol'], 'bid1Price': str(quote['bid']),
                        'ask1Price': str(quote['ask']), 'lastPrice': str((quote['bid'] + quote['ask']) / 2),
                        'bid1Size': str(quote.get('bid_size', 0)), 'ask1Size': str(quote.get('ask_size', 0)),
                        'turnover24h': str(quote.get('turnover_24h', 0))}]})
            if method != 'GET' or not self.public_transport:
                raise ValueError('Paper public market data requires an explicit read-only transport')
            return self.public_transport.request(method, url, headers=headers, body=body, timeout=timeout)
        with self._lock:
            state = self._load()
            if path == '/v5/order/create' and method == 'POST':
                link = args['orderLinkId']
                if link in state['orders']:
                    return self._ok(code=10014, message='Duplicate paper order identity')
                if args.get('category') not in {'spot', 'linear'} or args.get('side') not in {'Buy', 'Sell'}:
                    return self._ok(code=10001, message='Unsupported paper product')
                qty = float(args['qty'])
                if not math.isfinite(qty) or qty <= 0:
                    return self._ok(code=10001, message='Invalid paper size')
                conditional = args.get('category') == 'spot' and args.get('orderFilter') in {'StopOrder', 'tpslOrder'}
                if conditional and (args.get('side') != 'Sell' or args.get('isLeverage', 0) not in (0, '0') or
                                    args.get('marketUnit') != 'baseCoin' or not args.get('triggerPrice')):
                    return self._ok(code=10001, message='Paper Spot conditional must be a cash base-coin sell stop')
                order = {**args, 'orderId': 'paper-' + identity(link)[:24],
                    'orderStatus': 'Untriggered' if conditional else 'New',
                    'cumExecQty': '0', 'cumExecValue': '0', 'avgPrice': '0',
                    'leavesQty': str(qty), 'createdTime': str(round(self.clock() * 1000)),
                    'updatedTime': str(round(self.clock() * 1000)),
                    'paperEligibleAfterTime': str(round(self.clock() * 1000))}
                state['orders'][link] = order
                self._save(state)
                return self._ok({'orderId': order['orderId'], 'orderLinkId': link})
            if path in {'/v5/order/cancel', '/v5/order/amend'} and method == 'POST':
                order = state['orders'].get(args.get('orderLinkId'))
                if not order or order['orderStatus'] not in {'New', 'PartiallyFilled', 'Untriggered'}:
                    return self._ok(code=110001, message='Paper order no longer amendable/cancellable')
                if path.endswith('/cancel'):
                    order['orderStatus'] = 'Cancelled'
                else:
                    qty = float(args.get('qty', order['qty']))
                    if qty <= float(order['cumExecQty']) or not math.isfinite(qty):
                        return self._ok(code=10001, message='Amend quantity must exceed filled quantity')
                    order['qty'] = str(qty)
                    if args.get('price') is not None:
                        order['price'] = args['price']
                    order['leavesQty'] = str(qty - float(order['cumExecQty']))
                    # A changed price or size must not use a market observation
                    # that predates the amendment, including after restart.
                    order['paperEligibleAfterTime'] = str(round(self.clock() * 1000))
                order['updatedTime'] = str(round(self.clock() * 1000))
                self._save(state)
                return self._ok({'orderId': order['orderId'], 'orderLinkId': order['orderLinkId']})
            if path in {'/v5/order/realtime', '/v5/order/history'}:
                rows = list(state['orders'].values())
                rows = self._filter(rows, args, ('category', 'symbol', 'orderLinkId', 'orderId'))
                if args.get('orderFilter'):
                    rows = [r for r in rows if r.get('orderFilter', 'Order') == args['orderFilter']]
                if args.get('openOnly') == '0' and not args.get('orderLinkId') and path.endswith('realtime'):
                    rows = [r for r in rows if r['orderStatus'] in {'New', 'PartiallyFilled', 'Untriggered'}]
                return self._page(rows, args)
            if path == '/v5/execution/list':
                return self._page(self._filter(state['executions'], args, ('category', 'symbol', 'orderLinkId', 'orderId')), args)
            if path == '/v5/account/transaction-log':
                return self._page(self._filter(state['transactions'], args, ('category', 'symbol')), args)
            if path == '/v5/account/info':
                return self._ok({'unifiedMarginStatus': 3, 'marginMode': 'REGULAR_MARGIN'})
            if path == '/v5/account/wallet-balance':
                return self._ok({'list': [self._wallet(state)]})
            if path == '/v5/position/symbol-info':
                leverage = state['leverages'].get('linear:' + args['symbol'], '1')
                return self._ok({'list': [{'symbol': args['symbol'], 'positionIdx': 0,
                                          'leverage': str(leverage), 'side': 'Buy'}]})
            if path == '/v5/position/list':
                rows = [self._position(state, p) for p in state['positions'].values() if p['category'] == 'linear']
                if args.get('symbol') and not any(x['symbol'] == args['symbol'] for x in rows):
                    rows.append({'symbol': args['symbol'], 'side': '', 'positionIdx': 0, 'size': '0',
                                 'avgPrice': '0', 'markPrice': '0', 'unrealisedPnl': '0',
                                 'leverage': str(state['leverages'].get('linear:' + args['symbol'], '1')),
                                 'stopLoss': '', 'liqPrice': ''})
                return self._page(self._filter(rows, args, ('symbol',)), args)
            if path == '/v5/position/set-leverage' and method == 'POST':
                if args.get('category') != 'linear' or not isinstance(args.get('symbol'), str):
                    return self._ok(code=10001, message='Paper leverage is linear-only')
                try:
                    if max(len(str(args.get('buyLeverage', ''))), len(str(args.get('sellLeverage', '')))) > 64:
                        return self._ok(code=10001, message='Paper leverage precision is outside the supported bound')
                    buy, sell = Decimal(str(args['buyLeverage'])), Decimal(str(args['sellLeverage']))
                except (KeyError, InvalidOperation, ValueError):
                    return self._ok(code=10001, message='Invalid paper leverage')
                if (not buy.is_finite() or not sell.is_finite() or buy < 1 or buy != sell
                        or buy.as_tuple().exponent < -12 or sell.as_tuple().exponent < -12):
                    return self._ok(code=10001, message='Paper leverage must be finite, paired, and at least 1x')
                symbol = args['symbol']
                position = state['positions'].get('linear:' + symbol)
                if position and position.get('quantity', 0) > 0:
                    return self._ok(code=110043, message='Close paper position before leverage change')
                active = {'Created', 'New', 'PartiallyFilled', 'Untriggered', 'Triggered'}
                if any(row.get('category') == 'linear' and row.get('symbol') == symbol
                       and row.get('orderStatus') in active for row in state['orders'].values()):
                    return self._ok(code=110043, message='Cancel paper orders before leverage change')
                state['leverages']['linear:' + symbol] = format(buy.normalize(), 'f')
                self._save(state)
                return self._ok({'buyLeverage': str(state['leverages']['linear:' + symbol]),
                                 'sellLeverage': str(state['leverages']['linear:' + symbol])})
            if path == '/v5/position/trading-stop' and method == 'POST':
                position = state['positions'].get('linear:' + args['symbol'])
                if not position or not position['quantity']:
                    return self._ok(code=110001, message='No paper position')
                position['stop_loss'] = float(args['stopLoss'])
                self._save(state)
                return self._ok()
            raise ValueError('Unsupported local paper exchange route: ' + path)

    @staticmethod
    def _filter(rows, args, fields):
        return [copy.deepcopy(r) for r in rows if all(not args.get(k) or str(r.get(k)) == str(args[k]) for k in fields)
                and (not args.get('startTime') or int(r.get('execTime', r.get('updatedTime', r.get('transactionTime', 0)))) >= int(args['startTime']))]

    def _page(self, rows, args):
        offset, limit = int(args.get('cursor', 0)), min(1000, max(1, int(args.get('limit', 200))))
        return self._ok({'list': rows[offset:offset + limit],
                         'nextPageCursor': str(offset + limit) if len(rows) > offset + limit else ''})

    @staticmethod
    def _position(state, position):
        key = position['category'] + ':' + position['symbol']
        quote = state['quotes'].get(key, {})
        mark = quote.get('bid', position['entry_price'])
        return {'symbol': position['symbol'], 'side': 'Buy', 'positionIdx': 0,
            'size': str(position['quantity']), 'avgPrice': str(position['entry_price']),
            'markPrice': str(mark), 'unrealisedPnl': str(position['quantity'] * (mark - position['entry_price'])),
            'leverage': str(state.get('leverages', {}).get(key, '1')),
            'stopLoss': str(position.get('stop_loss', 0)), 'liqPrice': ''}

    def _wallet(self, state):
        spot_value, linear_margin, upl, coins = 0., 0., 0., []
        for key, position in state['positions'].items():
            if position['quantity'] <= 0:
                continue
            quote = state['quotes'].get(key, {})
            mark = quote.get('bid', position['entry_price'])
            value = position['quantity'] * mark
            if position['category'] == 'spot':
                spot_value += value
                with self.store.connection() as db:
                    row = db.execute('SELECT value FROM bybit_instruments WHERE mode=? AND category=? AND symbol=?',
                                     ('paper', 'spot', position['symbol'])).fetchone()
                if not row:
                    raise ValueError('Paper spot accounting requires exact instrument metadata')
                base = json.loads(row[0])['base_coin']
                coins.append({'coin': base, 'walletBalance': str(position['quantity']),
                              'equity': str(position['quantity']), 'usdValue': str(value), 'spotBorrow': '0'})
            else:
                leverage = float(state.get('leverages', {}).get(key, '1'))
                linear_margin += position['quantity'] * position['entry_price'] / leverage
                upl += position['quantity'] * (mark - position['entry_price'])
        equity = state['cash'] + spot_value + upl
        # Losses reduce collateral immediately. This conservative simulator
        # does not reuse unrealized gains as cash or model portfolio margin.
        available = max(0., state['cash'] + min(0., upl) - linear_margin)
        return {'accountType': 'UNIFIED', 'totalEquity': str(equity),
            'totalWalletBalance': str(state['cash'] + spot_value),
            'totalAvailableBalance': str(available), 'totalPerpUPL': str(upl),
            'coin': [{'coin': 'USDT', 'walletBalance': str(state['cash']), 'equity': str(state['cash'] + upl),
                      'availableToWithdraw': str(available), 'spotBorrow': '0'}, *coins]}

    def advance(self, snapshots):
        """Match against a later fresh quote once; return actual simulated events.

        Total matched volume for all orders shares the observed top-of-book size.
        Market orders use IOC semantics. No fill is manufactured on missing data.
        """
        snapshots = tuple(snapshots)
        messages = []
        with self._lock:
            fast_result = self._advance_flat(snapshots)
            if fast_result is not None:
                return fast_result
            state = self._load()
            changed = False
            now = round(self.clock() * 1000)
            for snapshot in snapshots:
                category, symbol = snapshot['category'], snapshot['symbol']
                # The same validation and high-water semantics are used by the
                # flat fast path and the full matching path.
                accepted = self._accepted_quote(snapshot, now, state)
                if accepted is None:
                    continue
                key, digest, quote = accepted
                state['quote_ids'][key], state['quotes'][key] = digest, copy.deepcopy(quote)
                changed = True
                depth = {side: max(0., float(quote.get(side + '_size') or 0)) for side in ('bid', 'ask')}
                position = state['positions'].get(key)
                if category == 'linear' and position and position['quantity'] > 0 and position.get('stop_loss', 0) >= quote['bid']:
                    link = 'paper-stop-' + identity([key, digest, position['quantity']])[:24]
                    state['orders'][link] = {'orderLinkId': link, 'orderId': link, 'category': category, 'symbol': symbol,
                        'side': 'Sell', 'orderType': 'Market', 'qty': str(position['quantity']), 'reduceOnly': True,
                        'orderStatus': 'New', 'cumExecQty': '0', 'cumExecValue': '0', 'avgPrice': '0',
                        'leavesQty': str(position['quantity']), 'createdTime': str(quote['timestamp_ms'] - 1)}
                for order in list(state['orders'].values()):
                    if (order['category'] != category or order['symbol'] != symbol or
                            order['orderStatus'] not in {'New', 'PartiallyFilled', 'Untriggered'} or
                            quote['timestamp_ms'] <= int(order.get('paperEligibleAfterTime', order['createdTime']))):
                        continue
                    if order.get('orderFilter') in {'StopOrder', 'tpslOrder'} and order['orderStatus'] == 'Untriggered':
                        stop = float(order.get('triggerPrice') or 0)
                        triggered = (order.get('side') == 'Sell' and quote['bid'] <= stop)
                        if not triggered:
                            continue
                        # The simulated exchange observes this quote after the
                        # create time, triggers the conditional, then submits
                        # its market IOC against the same measured book.
                        order['orderStatus'] = 'New'
                        order['triggeredTime'] = str(quote['timestamp_ms'])
                        order['orderType'] = 'Market'
                        order['timeInForce'] = 'IOC'
                    book = 'ask' if order['side'] == 'Buy' else 'bid'
                    price = quote[book]
                    limit = float(order.get('price') or price)
                    if order['orderType'] != 'Market' and ((book == 'ask' and price > limit) or (book == 'bid' and price < limit)):
                        if order.get('timeInForce') in {'IOC', 'FOK'}:
                            order['orderStatus'] = 'Cancelled'
                            messages.append({'topic': 'order', 'data': [copy.deepcopy(order)]})
                        continue
                    quantity = min(float(order['qty']) - float(order['cumExecQty']), depth[book])
                    position = state['positions'].get(key)
                    if book == 'bid':
                        quantity = min(quantity, (position or {}).get('quantity', 0.))
                    else:
                        available = float(self._wallet(state)['totalAvailableBalance'])
                        leverage = float(state.get('leverages', {}).get(key, '1')) if category == 'linear' else 1.
                        quantity = min(quantity, available / (price / leverage + price * self.fee))
                    if (order.get('timeInForce') == 'FOK' and
                            quantity < float(order['qty']) - float(order['cumExecQty'])):
                        order.update(orderStatus='Cancelled', updatedTime=str(now))
                        messages.append({'topic': 'order', 'data': [copy.deepcopy(order)]})
                        continue
                    if quantity > 1e-12:
                        execution = self._fill(state, order, quantity, price, now)
                        messages.append({'topic': 'execution', 'data': [execution]})
                        depth[book] -= quantity
                    if (order['orderType'] == 'Market' or order.get('timeInForce') == 'IOC') and order['orderStatus'] != 'Filled':
                        order['orderStatus'] = 'PartiallyFilledCanceled' if float(order['cumExecQty']) else 'Cancelled'
                    order['updatedTime'] = str(now)
                    messages.append({'topic': 'order', 'data': [copy.deepcopy(order)]})
            if changed:
                self._save(state)
        return messages

    def _fill(self, state, order, quantity, price, now):
        key = order['category'] + ':' + order['symbol']
        position = state['positions'].setdefault(key, {'category': order['category'], 'symbol': order['symbol'],
            'quantity': 0., 'entry_price': 0., 'stop_loss': 0.})
        fee, realized = quantity * price * self.fee, 0.
        if order['side'] == 'Buy':
            if position['quantity'] <= 1e-12:
                position['stop_loss'] = 0.
            position['entry_price'] = (position['quantity'] * position['entry_price'] + quantity * price) / (position['quantity'] + quantity)
            position['quantity'] += quantity
            if order.get('stopLoss'):
                position['stop_loss'] = float(order['stopLoss'])
            state['cash'] -= fee + (quantity * price if order['category'] == 'spot' else 0.)
        else:
            realized = quantity * (price - position['entry_price'])
            position['quantity'] = max(0., position['quantity'] - quantity)
            state['cash'] += (quantity * price if order['category'] == 'spot' else realized) - fee
        filled = float(order['cumExecQty']) + quantity
        value = float(order['cumExecValue']) + quantity * price
        order.update(cumExecQty=str(filled), cumExecValue=str(value), avgPrice=str(value / filled),
                     leavesQty=str(max(0., float(order['qty']) - filled)),
                     orderStatus='Filled' if filled >= float(order['qty']) - 1e-12 else 'PartiallyFilled')
        state['counter'] += 1
        execution = {'execId': 'paper-exec-' + str(state['counter']), 'execType': 'Trade',
            'category': order['category'], 'symbol': order['symbol'], 'orderId': order['orderId'],
            'orderLinkId': order['orderLinkId'], 'side': order['side'], 'execQty': str(quantity),
            'execPrice': str(price), 'execFee': str(fee), 'feeCurrency': 'USDT',
            'execPnl': str(realized), 'execTime': str(now), 'seq': state['counter']}
        state['executions'].append(execution)
        return copy.deepcopy(execution)

    def apply_funding(self, category, symbol, amount, *, event_id):
        """Apply a measured/specified paper funding settlement once, with provenance."""
        if category != 'linear' or not math.isfinite(amount):
            raise ValueError('Funding requires a finite linear settlement')
        with self._lock:
            state = self._load()
            if any(x['id'] == event_id for x in state['transactions']):
                return
            state['transactions'].append({'id': event_id, 'category': category, 'symbol': symbol, 'currency': 'USDT',
                'transactionTime': str(round(self.clock() * 1000)), 'type': 'SETTLEMENT',
                'funding': str(amount), 'fee': '0', 'cashFlow': '0', 'change': str(amount)})
            state['cash'] += amount
            self._save(state)
