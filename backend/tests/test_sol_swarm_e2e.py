"""Offline end-to-end contract for the continuous SOL support-to-paper path."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from prism_runtime.agent_runtime import ResponsesAgent
from prism_runtime.bybit import BybitAdapter
from prism_runtime.continuous import ContinuousServices
from prism_runtime.domain import identity
from prism_runtime.live_execution import ExecutionController
from prism_runtime.paper_exchange import PaperExchange
from prism_runtime.provider import ModelProvider
from prism_runtime.research_memory import ResearchMemory
from prism_runtime.store import Store
from prism_runtime.orchestrator import Runtime
from test_bybit import FixtureTransport
from test_live_execution import NOW_SECONDS


class OfflineResponses:
    """Local Responses transport with Luna reports and one Sol tool proposal."""

    def __init__(self, clock):
        self.clock = clock
        self.requests = []
        self._lock = threading.Lock()
        self._execution_payload = None
        self._trader_decisions = 0
        self.review_decision = 'approve'
        self.review_error = False

    def __call__(self, body, key):
        if key != 'sol-swarm-offline-only':
            raise AssertionError('The integration fixture must never load a real provider credential')
        with self._lock:
            self.requests.append(copy.deepcopy(body))

        if body.get('model') == 'gpt-6-luna':
            format_name = body.get('text', {}).get('format', {}).get('name')
            if format_name == 'prism_trade_review':
                if self.review_error:
                    return {'status': 'failed', 'output': [],
                        'usage': {'input_tokens': 100, 'output_tokens': 0}}
                result = {'decision': self.review_decision,
                    'rationale': 'Fixture independent Luna review.'}
                return {'status': 'completed', 'output': [{'type': 'message', 'content': [
                    {'type': 'output_text', 'text': json.dumps(result)}]}],
                    'usage': {'input_tokens': 100, 'output_tokens': 20}}
            packet = json.loads(body['input'][1]['content'])
            report = {'summary': 'Fixture report for ' + packet['specialist'],
                'stance': 'neutral', 'observations': ['Measured fixture candle reviewed'],
                'risks': ['Paper fixture only'], 'invalidation': 'A newer candle replaces this report',
                'horizon_seconds': 60}
            return {'status': 'completed', 'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': json.dumps(report)}]}],
                'usage': {'input_tokens': 100, 'output_tokens': 20}}

        if body.get('model') != 'gpt-6.1-sol':
            raise AssertionError('Unexpected model route: ' + str(body.get('model')))
        outputs = body.get('input', [])
        if any(item.get('type') == 'function_call_output' for item in outputs):
            return {'status': 'completed', 'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': 'Paper intent requested from the host tools.'}]}],
                'usage': {'input_tokens': 120, 'output_tokens': 20}}

        self._trader_decisions += 1
        if self._trader_decisions > 1:
            return {'status': 'completed', 'output': [{'type': 'function_call',
                'name': 'hold', 'arguments': json.dumps({'strategy_id': self._execution_payload['strategy']['id'],
                    'rationale': 'Hold on the next offline minute.'}),
                'call_id': 'sol-fixture-hold'}], 'usage': {'input_tokens': 120, 'output_tokens': 20}}

        self._execution_payload = json.loads(body['input'][1]['content'])
        payload = self._execution_payload
        args = {'strategy_id': payload['strategy']['id'],
            'snapshot_id': payload['decision_snapshot']['snapshot_id'], 'side': 'Buy',
            'order_type': 'Market', 'quantity': 0.1, 'limit_price': None,
            'reduce_only': False, 'expires_at_ms': round(self.clock() * 1000) + 10_000,
            'rationale': 'Offline fixture proposal; controller revalidates all host gates.'}
        return {'status': 'completed', 'output': [{'type': 'function_call',
            'name': 'submit_order_intent', 'arguments': json.dumps(args), 'call_id': 'sol-fixture-order'}],
            'usage': {'input_tokens': 150, 'output_tokens': 30}}


class SolMarketFixture:
    """Host-snapshot fixture with the same lookup surface used by continuous lanes."""

    def __init__(self, store, clock, adapter):
        self.store, self.clock, self.adapter = store, clock, adapter
        self.on_event = None
        self.values = {}
        with store.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS md_snapshots (
                id TEXT PRIMARY KEY, strategy_id TEXT, signal_input_id TEXT,
                captured_at_ms INTEGER, invalidated INTEGER DEFAULT 0, reason TEXT)''')

    def register_strategy(self, strategy):
        return copy.deepcopy(strategy)

    def _get_adapter(self):
        return self.adapter

    def publish(self, strategy_id, registered_at_ms, *, captured_at_ms=None, bid=99.99,
                ask=100.01, ask_size=100.):
        captured_at_ms = captured_at_ms or round(self.clock() * 1000)
        start_ms = registered_at_ms + 1_000
        end_ms = start_ms + 60_000
        signal_input_id = identity(['offline-sol-closed-candle', start_ms, end_ms])
        value = {'strategy_id': strategy_id, 'snapshot_id': '', 'captured_at_ms': captured_at_ms,
            'venue': 'bybit', 'environment': 'testnet', 'category': 'linear', 'symbol': 'SOLUSDT',
            'primary_timeframe': '1', 'signal_mode': 'closed', 'ready': True, 'stale': False,
            'reason': 'Offline deterministic fixture', 'current_target': 1, 'signal': 1,
            'signal_input_id': signal_input_id, 'attention_deadline_ms': captured_at_ms + 60_000,
            'candles': [{'start_ms': start_ms, 'end_ms': end_ms, 'open': 100., 'high': 101.,
                         'low': 99., 'close': 100., 'volume': 10., 'turnover': 1000.,
                         'closed': True, 'source': 'websocket', 'revision': 0,
                         'contiguous_on_arrival': True, 'observed_at_ms': captured_at_ms}],
            'quote': {'bid': bid, 'ask': ask, 'bid_size': 100., 'ask_size': ask_size,
                      'turnover_24h': 1_000_000., 'timestamp_ms': captured_at_ms,
                      'source': 'websocket'},
            'indicators': {'sma_20': {'value': 100., 'ready': True}},
            'data_quality': {'warmup_complete': True, 'gap_count': 0, 'stale': False,
                'instrument_verified': True, 'forward_eligible': True, 'quote_age_ms': 0}}
        ident = self.store.save_snapshot('bybit_market', value, captured_at_ms)
        value['snapshot_id'] = ident
        with self.store.connection() as db:
            db.execute('INSERT OR REPLACE INTO md_snapshots VALUES (?,?,?,?,0,NULL)',
                (ident, 'testnet:' + strategy_id, signal_input_id, captured_at_ms))
        self.values[strategy_id] = copy.deepcopy(value)
        return copy.deepcopy(value)

    def snapshot(self, strategy_id, **_kwargs):
        return copy.deepcopy(self.values[strategy_id])

    def sample(self, strategy_id, **_kwargs):
        return copy.deepcopy(self.values[strategy_id])

    def snapshot_status(self, snapshot_id):
        value = self.store.snapshot(snapshot_id)
        return {'valid': bool(value and value.get('kind') == 'bybit_market'), 'reason': 'offline fixture'}


class SolSwarmEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [NOW_SECONDS]
        self.clock = lambda: self.now[0]
        self.store = Store(Path(self.temp.name) / 'sol-swarm.sqlite3', clock=self.clock)
        self.responses = OfflineResponses(self.clock)
        self.provider = ModelProvider(lambda: 'sol-swarm-offline-only', self.responses)
        self.runtime = Runtime(self.store, key_loader=lambda: self.fail('No provider key lookup is allowed'),
                               provider=self.provider, collector=object())
        self.runtime.update_configuration({'trading_mode': {'selected': 'sol_perpetual'}})
        self.strategy_id = self.store.get_document('sol_mode_strategy_id')
        self.runtime.update_configuration({
            'bybit': {'mode': 'paper', 'enabled': True, 'mainnet_enabled': False,
                'account_id': 'offline-paper-account', 'allowed_categories': ['linear'],
                'allowed_instruments': [{'category': 'linear', 'symbol': 'SOLUSDT'}]},
            'live_execution': {'enabled': True, 'eligible_strategy_versions': [self.strategy_id],
                'allocations': {self.strategy_id: 100.}, 'max_portfolio_notional': 100.,
                'max_position_notional': 100., 'max_margin_fraction': 1.,
                'max_spread_bps': 10., 'max_slippage_bps': 30.,
                'max_participation_fraction': .01, 'max_leverage': 1.,
                'require_exchange_protection': True}})
        self.public = FixtureTransport()
        self.paper = PaperExchange(self.store, public_transport=self.public, clock=self.clock,
                                   initial_equity=100.)
        self.adapter = BybitAdapter(self.store, self.store.config(), transport=self.paper,
            credential_loader=lambda _mode: self.fail('Paper mode must not load Bybit credentials'),
            clock=self.clock)
        self.market = SolMarketFixture(self.store, self.clock, self.adapter)
        self.memory = ResearchMemory(self.store, clock=self.clock)
        self.agents = ResponsesAgent(self.store, self.provider, clock=self.clock)
        self.controller = ExecutionController(self.store, self.adapter, self.market, self.memory,
            self.agents, eligibility=lambda sid: {'eligible': sid == self.strategy_id,
                'status': 'fixture-approved', 'reasons': []}, clock=self.clock)
        self.services = ContinuousServices(self.runtime, market=self.market, adapter=self.adapter,
                                           execution=self.controller, agents=self.agents, clock=self.clock)
        # Step methods run directly; no installed service, threads or sockets start.
        self.runtime.paused = False
        self.runtime.status = 'running'
        self.registered_at_ms = self.store.experiment(self.strategy_id)['registered_at_ms']
        self.now[0] = self.registered_at_ms / 1000 + 62
        self.snapshot = self.market.publish(self.strategy_id, self.registered_at_ms)
        self.paper.advance([self.snapshot])

    def tearDown(self):
        if self.services.support:
            self.services.support.close(wait=True)
        for swarm in self.services._retired_support:
            swarm.close(wait=False)
        self.runtime.paused = True
        self.runtime.shutdown()
        self.store.close()
        self.temp.cleanup()

    def test_closed_candle_fans_out_to_luna_then_sol_intent_is_revalidated_on_paper(self):
        self.services.on_market({'id': 'sol-candle-event-1', 'kind': 'candle_closed',
            'strategy_id': self.strategy_id, 'category': 'linear', 'symbol': 'SOLUSDT'})
        observed = self.services.observe_step()
        self.assertEqual(observed['snapshot_id'], self.snapshot['snapshot_id'])

        # Pass the configured event-coalescing window; the source quote is still fresh.
        self.now[0] += 3
        support = self.services.support_step()
        self.assertIsNotNone(support, self.services.events.state())
        self.assertEqual(len(support['reports']), 4)
        luna_requests = [request for request in self.responses.requests if request['model'] == 'gpt-6-luna']
        support_requests = [request for request in luna_requests
                            if request.get('text', {}).get('format', {}).get('name') != 'prism_trade_review']
        self.assertEqual(len(support_requests), 4)
        self.assertEqual([request['model'] for request in self.responses.requests[:4]], ['gpt-6-luna'] * 4)
        self.assertTrue(all(request['reasoning']['effort'] == 'max' for request in support_requests))
        self.assertTrue(all('tools' not in request for request in support_requests))

        decision = self.services.execution_step()
        self.assertEqual(decision['status'], 'submitted')
        review_requests = [request for request in self.responses.requests
            if request.get('text', {}).get('format', {}).get('name') == 'prism_trade_review']
        self.assertEqual(len(review_requests), 1)
        self.assertEqual([request['model'] for request in self.responses.requests],
            ['gpt-6-luna'] * 4 + ['gpt-6.1-sol', 'gpt-6-luna', 'gpt-6.1-sol'])
        trader_requests = [request for request in self.responses.requests if request['model'] == 'gpt-6.1-sol']
        self.assertEqual(len(trader_requests), 2)  # one tool proposal and its host result continuation
        first = trader_requests[0]
        self.assertEqual(first['reasoning']['effort'], 'xhigh')
        self.assertEqual([tool['name'] for tool in first['tools']], [
            'inspect_strategies', 'inspect_market', 'inspect_account', 'inspect_orders',
            'submit_order_intent', 'amend_order', 'cancel_order', 'reduce_exposure', 'hold', 'set_leverage'])

        packet = self.responses._execution_payload
        support_context = packet['support_swarm']
        self.assertEqual(support_context['status'], 'compatible')
        self.assertEqual(len(support_context['reports']), 4)
        self.assertEqual({row['source_snapshot_id'] for row in support_context['reports']},
                         {self.snapshot['snapshot_id']})
        self.assertEqual({row['report']['summary'] for row in support_context['reports']},
                         {'Fixture report for ' + role for role in
                          ('trend_momentum', 'mean_reversion', 'volatility_risk', 'market_microstructure')})
        review_packet = json.loads(review_requests[0]['input'][1]['content'])
        self.assertEqual(review_packet['tool_name'], 'submit_order_intent')
        self.assertEqual(review_packet['mode'], 'sol_perpetual')
        self.assertEqual(review_requests[0]['text']['format']['schema']['required'], ['decision', 'rationale'])

        orders = self.adapter.orders(category='linear', symbol='SOLUSDT')
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]['status'], 'open')
        self.assertEqual(orders[0]['quantity'], 0.1)
        self.assertEqual(orders[0]['filled_quantity'], 0.)
        self.assertEqual(decision['result']['tools'][0]['result']['acknowledgement_is_not_fill'], True)

        # Replaying the durable host tool identity returns its stored outcome;
        # it neither asks Luna again nor submits another order.
        review_count = len(review_requests)
        proposal = review_packet['arguments']
        with self.store.connection() as db:
            mutation = db.execute("SELECT tool_id FROM execution_mutations WHERE action='submit_order_intent'").fetchone()
        self.assertIsNotNone(mutation)
        replay = self.controller._dispatch_tool('submit_order_intent', proposal, mutation['tool_id'],
            strategy_id=self.strategy_id, approved_snapshots={self.snapshot['snapshot_id']},
            decision_snapshot=self.snapshot, world=self.controller._world())
        self.assertEqual(replay['status'], decision['result']['tools'][0]['result']['status'])
        self.assertEqual(len([request for request in self.responses.requests if
            request.get('text', {}).get('format', {}).get('name') == 'prism_trade_review']), review_count)
        self.assertEqual(len(self.adapter.orders(category='linear', symbol='SOLUSDT')), 1)

        # Re-delivering the same closed-candle event is deduplicated by its
        # durable event identity and produces no additional Sol or Luna calls.
        request_count = len(self.responses.requests)
        self.services.on_market({'id': 'sol-candle-event-1', 'kind': 'candle_closed',
            'strategy_id': self.strategy_id, 'category': 'linear', 'symbol': 'SOLUSDT'})
        self.assertIsNone(self.services.observe_step())
        self.assertEqual(len(self.responses.requests), request_count)

        self.now[0] += 1
        later = copy.deepcopy(self.snapshot)
        later['captured_at_ms'] = round(self.now[0] * 1000)
        later['attention_deadline_ms'] = later['captured_at_ms'] + 60_000
        later['quote']['timestamp_ms'] = later['captured_at_ms']
        later['snapshot_id'] = self.store.save_snapshot('bybit_market',
            {key: value for key, value in later.items() if key != 'snapshot_id'}, later['captured_at_ms'])
        self.market.values[self.strategy_id] = later
        fills = self.paper.advance([later])
        self.assertTrue(any(message['topic'] == 'execution' for message in fills))
        for message in fills:
            self.adapter._handle_private_message(message)
        reconciled = self.controller.reconcile()
        self.assertEqual(reconciled['orders'][0]['status'], 'filled')
        self.assertAlmostEqual(reconciled['orders'][0]['filled_quantity'], 0.1)
        self.assertTrue(all('X-BAPI-API-KEY' not in call['headers'] for call in self.public.calls))

        # A newer closed minute yields a new support sweep and a fresh Sol hold.
        self.now[0] += 60
        next_snapshot = self.market.publish(self.strategy_id, self.registered_at_ms + 60_000)
        self.paper.advance([next_snapshot])
        self.services.on_market({'id': 'sol-candle-event-2', 'kind': 'candle_closed',
            'strategy_id': self.strategy_id, 'category': 'linear', 'symbol': 'SOLUSDT'})
        observed_next = self.services.observe_step()
        self.assertEqual(observed_next['snapshot_id'], next_snapshot['snapshot_id'])
        self.now[0] += 3
        next_support = self.services.support_step()
        self.assertEqual(len(next_support['reports']), 4)
        next_decision = self.services.execution_step()
        self.assertEqual(next_decision['status'], 'held')
        self.assertEqual(len([request for request in self.responses.requests if
            request.get('text', {}).get('format', {}).get('name') == 'prism_trade_review']), review_count)
        self.assertEqual(len(self.adapter.orders(category='linear', symbol='SOLUSDT')), 1)


if __name__ == '__main__':
    unittest.main()
