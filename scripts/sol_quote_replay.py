#!/usr/bin/env python3
"""Replay explicit synthetic SOLUSDT events against the local paper exchange."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from prism_runtime.bybit import BybitAdapter, BybitError
from prism_runtime.paper_exchange import PaperExchange
from prism_runtime.store import Store


class FixtureInstrument:
    """Synthetic instrument metadata only; no network fallback exists."""
    def request(self, method, url, *, headers=None, body=None, timeout=10):
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
        if (method != 'GET' or parsed.path != '/v5/market/instruments-info'
                or params.get('category') != ['linear'] or params.get('symbol') != ['SOLUSDT']):
            raise ValueError('Offline fixture refuses all other market requests')
        return {'retCode': 0, 'retMsg': 'OK', 'result': {'list': [{
            'symbol': 'SOLUSDT', 'status': 'Trading', 'baseCoin': 'SOL',
            'quoteCoin': 'USDT', 'settleCoin': 'USDT', 'contractType': 'LinearPerpetual',
            'priceFilter': {'tickSize': '0.01'},
            'lotSizeFilter': {'qtyStep': '0.01', 'minOrderQty': '0.01',
                              'maxOrderQty': '100000', 'maxMktOrderQty': '100000',
                              'minNotionalValue': '5'},
            'leverageFilter': {'maxLeverage': '1'},
        }]}}


def _finite(value, *, positive=False, nonnegative=False):
    try:
        return (type(value) in (int, float) and math.isfinite(value)
                and (not positive or value > 0) and (not nonnegative or value >= 0))
    except OverflowError:
        return False


def _validate(document):
    if not isinstance(document, dict) or set(document) != {'initial_equity', 'fee_bps', 'events'}:
        raise ValueError('Supply initial_equity, fee_bps and events')
    if not _finite(document['initial_equity'], positive=True):
        raise ValueError('Initial equity must be finite and positive')
    if not _finite(document['fee_bps'], nonnegative=True) or document['fee_bps'] >= 10000:
        raise ValueError('Fee must be nonnegative and less than 10000 bps')
    events = document['events']
    if not isinstance(events, list) or not 1 <= len(events) <= 5000:
        raise ValueError('Supply 1–5000 ordered synthetic events')
    previous, quote_seen, order_ids, funding_ids = 0, False, set(), set()
    for event in events:
        if (not isinstance(event, dict) or type(event.get('timestamp_ms')) is not int
                or event['timestamp_ms'] <= previous or event['timestamp_ms'] > 10_000_000_000_000):
            raise ValueError('Events require strictly increasing positive millisecond timestamps')
        previous = event['timestamp_ms']
        kind = event.get('type')
        if kind == 'quote':
            if set(event) != {'type', 'timestamp_ms', 'bid', 'ask', 'bid_size', 'ask_size'}:
                raise ValueError('Quote must specify bid, ask and both sizes')
            if (not _finite(event['bid'], positive=True) or not _finite(event['ask'], positive=True)
                    or event['bid'] > event['ask'] or not _finite(event['bid_size'], nonnegative=True)
                    or not _finite(event['ask_size'], nonnegative=True)):
                raise ValueError('Synthetic quote is malformed')
            quote_seen = True
        elif kind == 'order':
            required = {'type', 'timestamp_ms', 'id', 'side', 'quantity', 'limit_price'}
            permitted = required | {'stop_loss', 'order_type'}
            if not required <= set(event) or set(event) - permitted or not quote_seen:
                raise ValueError('Order requires explicit fields and a prior quote')
            order_type = event.get('order_type', 'Limit')
            if (not isinstance(event['id'], str) or not event['id'] or event['id'] in order_ids
                    or event['side'] not in {'Buy', 'Sell'} or not _finite(event['quantity'], positive=True)
                    or order_type not in {'Limit', 'Market'}
                    or (order_type == 'Limit' and not _finite(event['limit_price'], positive=True))
                    or (order_type == 'Market' and event['limit_price'] is not None)):
                raise ValueError('Synthetic order is malformed or duplicated')
            if event['side'] == 'Buy' and not _finite(event.get('stop_loss'), positive=True):
                raise ValueError('Paper opening orders require an explicit stop')
            if event['side'] == 'Sell' and 'stop_loss' in event:
                raise ValueError('Reduction cannot attach a new stop')
            order_ids.add(event['id'])
        elif kind == 'funding':
            amount_fields = {'type', 'timestamp_ms', 'id', 'amount'}
            rate_fields = {'type', 'timestamp_ms', 'id', 'rate', 'mark_price'}
            if (set(event) not in (amount_fields, rate_fields)
                    or not isinstance(event['id'], str) or not event['id'] or event['id'] in funding_ids
                    or (set(event) == amount_fields and not _finite(event['amount']))
                    or (set(event) == rate_fields and (not _finite(event['rate'])
                        or not _finite(event['mark_price'], positive=True)))):
                raise ValueError('Funding must be an explicit, unique signed USDT settlement')
            funding_ids.add(event['id'])
        elif kind == 'target':
            required = {'type', 'timestamp_ms', 'id', 'target', 'quantity',
                        'stop_fraction', 'max_quote_age_ms'}
            if (set(event) != required or not quote_seen or not isinstance(event['id'], str)
                    or not event['id'] or event['id'] in order_ids or type(event['target']) is not int
                    or event['target'] not in (0, 1) or not _finite(event['quantity'], positive=True)
                    or not _finite(event['stop_fraction'], positive=True) or event['stop_fraction'] >= 1
                    or type(event['max_quote_age_ms']) is not int or event['max_quote_age_ms'] <= 0):
                raise ValueError('Target requires a unique identity, binary target, size, stop and quote age')
            order_ids.add(event['id'])
        else:
            raise ValueError('Unknown synthetic event type')
    return events


def replay(document):
    """Use a new temporary paper journal; never contact or start a service."""
    events = _validate(document)
    with tempfile.TemporaryDirectory(prefix='prism-sol-quote-replay-') as directory:
        now = [events[0]['timestamp_ms'] / 1000]
        store = Store(Path(directory) / 'paper.sqlite3', clock=lambda: now[0])
        paper = PaperExchange(store, public_transport=FixtureInstrument(), clock=lambda: now[0],
                              initial_equity=document['initial_equity'], fee_bps=document['fee_bps'])
        config = store.config()
        config['bybit']['mode'] = 'paper'
        config['bybit']['allowed_instruments'] = [{'category': 'linear', 'symbol': 'SOLUSDT'}]
        def no_credentials(_):
            raise AssertionError('Synthetic paper replay cannot read credentials')
        adapter = BybitAdapter(store, config, transport=paper, clock=lambda: now[0],
                               credential_loader=no_credentials)
        outcomes, target_outcomes, marks, last_quote = [], [], [], None
        for event in events:
            now[0] = event['timestamp_ms'] / 1000
            if event['type'] == 'quote':
                quote = {key: event[key] for key in ('timestamp_ms', 'bid', 'ask', 'bid_size', 'ask_size')}
                quote['book_timestamp_ms'] = event['timestamp_ms']
                for message in paper.advance([{'category': 'linear', 'symbol': 'SOLUSDT', 'quote': quote}]):
                    adapter._handle_private_message(message)
                last_quote = event['timestamp_ms']
            elif event['type'] == 'order':
                order = {'category': 'linear', 'symbol': 'SOLUSDT', 'order_link_id': event['id'],
                         'side': event['side'], 'quantity': event['quantity'], 'order_type': event.get('order_type', 'Limit'),
                         'price': event['limit_price'], 'reduce_only': event['side'] == 'Sell'}
                if event['side'] == 'Buy':
                    order['stop_loss'] = event['stop_loss']
                receipt = adapter.submit_order(order)
                outcomes.append({'id': event['id'], 'timestamp_ms': event['timestamp_ms'],
                                 'status': receipt['status']})
            elif event['type'] == 'target':
                state = paper._load()
                position = state['positions'].get('linear:SOLUSDT', {})
                held = position.get('quantity', 0.)
                quote = state['quotes'].get('linear:SOLUSDT')
                active = [order for order in state['orders'].values()
                          if order['category'] == 'linear' and order['symbol'] == 'SOLUSDT'
                          and order['orderStatus'] in {'New', 'PartiallyFilled', 'Untriggered'}]
                desired_quantity = event['quantity'] if event['target'] else 0.
                delta = desired_quantity - held
                desired_side = 'Buy' if delta > 1e-12 else 'Sell' if delta < -1e-12 else None
                for order in active:
                    if order['side'] != desired_side:
                        adapter.cancel_order(order['orderLinkId'])
                active = [order for order in active if order['side'] == desired_side]
                needed = abs(delta)
                outcome = {'id': event['id'], 'timestamp_ms': event['timestamp_ms'],
                           'target': event['target'], 'held_before': held}
                if needed <= 1e-12:
                    outcome['status'] = 'at_target'
                elif active:
                    outcome['status'] = 'pending_order'
                elif (not quote or type(quote.get('timestamp_ms')) is not int
                      or not 0 <= event['timestamp_ms'] - quote['timestamp_ms'] <= event['max_quote_age_ms']):
                    outcome['status'] = 'quote_unavailable_or_stale'
                else:
                    order = {'category': 'linear', 'symbol': 'SOLUSDT', 'order_link_id': event['id'],
                             'side': desired_side, 'quantity': needed, 'order_type': 'Market',
                             'price': None, 'reduce_only': desired_side == 'Sell'}
                    if desired_side == 'Buy':
                        order['stop_loss'] = quote['bid'] * (1 - event['stop_fraction'])
                    try:
                        receipt = adapter.submit_order(order)
                        outcome.update(status=receipt['status'], order_quantity=needed)
                    except (ValueError, BybitError) as error:
                        # Dust and instrument constraints are recorded, not
                        # invented away or silently rounded into a fill.
                        outcome.update(status='blocked', reason=str(error))
                target_outcomes.append(outcome)
            else:
                position = paper._load()['positions'].get('linear:SOLUSDT', {})
                if 'amount' in event:
                    amount = event['amount']
                else:
                    # Positive signed rates charge longs; settlement marks are
                    # explicit fixture inputs, never inferred from future data.
                    amount = -position.get('quantity', 0.) * event['mark_price'] * event['rate']
                if amount and position.get('quantity', 0) <= 0:
                    raise ValueError('Nonzero funding requires a simulated open position')
                provenance = {'source': 'synthetic_fixture', 'source_verified': False,
                              'basis': 'declared_amount' if 'amount' in event else 'declared_rate_and_mark'}
                if 'rate' in event:
                    provenance.update(rate=event['rate'], mark_price=event['mark_price'],
                                      position_quantity=position.get('quantity', 0.))
                paper.apply_funding('linear', 'SOLUSDT', amount, event_id=event['id'], provenance=provenance,
                                    source_timestamp_ms=event['timestamp_ms'])
            if event['type'] in {'quote', 'funding'}:
                equity = float(paper._wallet(paper._load())['totalEquity'])
                if not math.isfinite(equity) or equity <= 0:
                    raise ValueError('Paper account is insolvent; liquidation is not modeled')
                marks.append({'event_timestamp_ms': event['timestamp_ms'],
                              'mark_timestamp_ms': last_quote, 'equity_usdt': equity})
        adapter.reconcile()
        account = adapter.account_snapshot()
        fills = list(adapter.iter_executions(category='linear', symbol='SOLUSDT'))
        funding = list(adapter.iter_transactions())
        orders = list(adapter.iter_orders(category='linear', symbol='SOLUSDT'))
        return {'scope': 'synthetic SOLUSDT linear paper replay; no performance or edge claim',
                'initial_equity': document['initial_equity'], 'fee_bps': document['fee_bps'],
                'order_receipts': outcomes, 'orders': orders, 'fills': fills, 'funding_events': funding,
                'target_decisions': target_outcomes,
                'equity_marks': marks,
                'fees_usdt': sum(float(row['execFee']) for row in fills),
                'funding_usdt': sum(float(row['funding']) for row in funding),
                'wallet_usdt': account['wallet_balance'], 'equity_usdt': account['equity'],
                'unrealized_pnl_usdt': account['unrealized_pnl'],
                'positions': account['positions'], 'last_quote_timestamp_ms': last_quote,
                'mark_timestamp_ms': last_quote,
                'latest_ledger_event_timestamp_ms': events[-1]['timestamp_ms'],
                'limitations': ['Synthetic inputs and paper accounting, not actual venue execution',
                    'Top-of-book depth only; no queue position, hidden liquidity or market impact',
                    'No maintenance margin or liquidation model',
                    'Funding is supplied as a signed amount or a rate with an explicit settlement mark',
                    'Simulated stops and account constraints follow PaperExchange',
                    'Open positions are marked at the last supplied bid without a hypothetical exit']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('fixture', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(replay(json.loads(args.fixture.read_text())), indent=2, allow_nan=False))
    except (ValueError, OSError, json.JSONDecodeError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
