"""Persistent paper exchange implementing the same V5 wire lifecycle.

Only public market requests may leave this transport, through an explicitly
supplied public transport. Account/order routes are always local. A create
acknowledgement never fills an order: a later measured quote must match it.
This conservative top-of-book simulator does not model queue priority.
"""
from __future__ import annotations

import copy
import json
import math
import threading
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
        if store.get_document(self._key) is None:
            store.set_document(self._key, {'cash': initial_equity, 'initial_equity': initial_equity,
                'orders': {}, 'executions': [], 'transactions': [], 'positions': {},
                'quotes': {}, 'quote_ids': {}, 'counter': 0})

    def _load(self):
        return self.store.get_document(self._key)

    def _save(self, state):
        self.store.set_document(self._key, state)

    @staticmethod
    def _ok(result=None, code=0, message='OK'):
        return {'retCode': code, 'retMsg': message, 'result': result or {}}

    def request(self, method, url, *, headers=None, body=None, timeout=10):
        path = urlsplit(url).path
        params = dict(parse_qsl(urlsplit(url).query))
        args = json.loads(body) if body else params
        if path.startswith('/v5/market/'):
            if path == '/v5/market/tickers' and method == 'GET':
                state = self._load()
                quote = state['quotes'].get(args.get('category', '') + ':' + args.get('symbol', ''))
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
                    'updatedTime': str(round(self.clock() * 1000))}
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
                return self._ok({'list': [{'symbol': args['symbol'], 'positionIdx': 0,
                                          'leverage': '1', 'side': 'Buy'}]})
            if path == '/v5/position/list':
                rows = [self._position(state, p) for p in state['positions'].values() if p['category'] == 'linear']
                if args.get('symbol') and not any(x['symbol'] == args['symbol'] for x in rows):
                    rows.append({'symbol': args['symbol'], 'side': '', 'positionIdx': 0, 'size': '0',
                                 'avgPrice': '0', 'markPrice': '0', 'unrealisedPnl': '0',
                                 'leverage': '1', 'stopLoss': '', 'liqPrice': ''})
                return self._page(self._filter(rows, args, ('symbol',)), args)
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
            'leverage': '1', 'stopLoss': str(position.get('stop_loss', 0)), 'liqPrice': ''}

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
                linear_margin += position['quantity'] * position['entry_price']
                upl += position['quantity'] * (mark - position['entry_price'])
        equity = state['cash'] + spot_value + upl
        return {'accountType': 'UNIFIED', 'totalEquity': str(equity),
            'totalWalletBalance': str(state['cash'] + spot_value),
            'totalAvailableBalance': str(max(0., state['cash'] - linear_margin)), 'totalPerpUPL': str(upl),
            'coin': [{'coin': 'USDT', 'walletBalance': str(state['cash']), 'equity': str(state['cash'] + upl),
                      'availableToWithdraw': str(max(0., state['cash'] - linear_margin)), 'spotBorrow': '0'}, *coins]}

    def advance(self, snapshots):
        """Match against a later fresh quote once; return actual simulated events.

        Total matched volume for all orders shares the observed top-of-book size.
        Market orders use IOC semantics. No fill is manufactured on missing data.
        """
        messages = []
        with self._lock:
            state = self._load()
            now = round(self.clock() * 1000)
            for snapshot in snapshots:
                category, symbol = snapshot['category'], snapshot['symbol']
                key, quote = category + ':' + symbol, snapshot.get('quote', {})
                if (not all(type(quote.get(k)) in (int, float) and math.isfinite(quote[k]) and quote[k] > 0
                            for k in ('bid', 'ask', 'timestamp_ms')) or quote['bid'] > quote['ask']
                        or not 0 <= now - quote['timestamp_ms'] <= 10000):
                    continue
                digest = identity([quote['timestamp_ms'], quote])
                if state['quote_ids'].get(key) == digest:
                    continue
                state['quote_ids'][key], state['quotes'][key] = digest, copy.deepcopy(quote)
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
                            quote['timestamp_ms'] <= int(order['createdTime'])):
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
                        quantity = min(quantity, available / (price * (1 + self.fee)))
                    if quantity > 1e-12:
                        execution = self._fill(state, order, quantity, price, now)
                        messages.append({'topic': 'execution', 'data': [execution]})
                        depth[book] -= quantity
                    if (order['orderType'] == 'Market' or order.get('timeInForce') == 'IOC') and order['orderStatus'] != 'Filled':
                        order['orderStatus'] = 'PartiallyFilledCanceled' if float(order['cumExecQty']) else 'Cancelled'
                    order['updatedTime'] = str(now)
                    messages.append({'topic': 'order', 'data': [copy.deepcopy(order)]})
            self._save(state)
        return messages

    def _fill(self, state, order, quantity, price, now):
        key = order['category'] + ':' + order['symbol']
        position = state['positions'].setdefault(key, {'category': order['category'], 'symbol': order['symbol'],
            'quantity': 0., 'entry_price': 0., 'stop_loss': 0.})
        fee, realized = quantity * price * self.fee, 0.
        if order['side'] == 'Buy':
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
            'execPnl': str(realized), 'execTime': str(now)}
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
