"""Crash/restart tests for durable model request identity and tool dispatch.

All subprocesses use a fresh temporary SQLite journal and an injected in-memory
transport. No installed service, network socket, credential, or paid provider is
used here.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from prism_runtime.agent_runtime import ResponsesAgent
from prism_runtime.provider import ModelProvider, ProviderError
from prism_runtime.store import Store


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / 'backend'
NOW = 1_800_000_000.0


SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'],
}
TOOL_DEFINITIONS = [{
    'type': 'function', 'name': 'record_local_action',
    'description': 'fixture',
    'parameters': {
        'type': 'object', 'additionalProperties': False,
        'properties': {'identity': {'type': 'string'}}, 'required': ['identity'],
    },
}]
PAPER_ORDER_TOOL = [{
    'type': 'function', 'name': 'submit_paper_order', 'description': 'fixture',
    'parameters': {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'category': {'type': 'string'}, 'symbol': {'type': 'string'},
            'side': {'type': 'string'}, 'quantity': {'type': 'string'},
            'price': {'type': 'string'}, 'order_link_id': {'type': 'string'},
        },
        'required': ['category', 'symbol', 'side', 'quantity', 'price', 'order_link_id'],
    },
}]
class SolProcessRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='prism-process-recovery-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / 'journal.sqlite3'
        self.marker = self.root / 'provider-calls.txt'
        self.env = os.environ.copy()
        current = self.env.get('PYTHONPATH', '')
        self.env['PYTHONPATH'] = os.pathsep.join(str(p) for p in (BACKEND, ROOT / 'backend' / 'tests')) + (os.pathsep + current if current else '')

    def _run_crashing_child(self, code: str, expected_exit: int):
        process = subprocess.Popen(
            [sys.executable, '-c', code, str(self.db_path), str(self.marker)],
            cwd=ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, close_fds=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=12)
            self.assertEqual(process.returncode, expected_exit,
                             f'child exit={process.returncode}; stdout={stdout}; stderr={stderr}')
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=3)
            self.fail(f'crash fixture exceeded its bound; stdout={stdout}; stderr={stderr}')
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)

    def _provider(self, response):
        def transport(body, key):
            raise AssertionError('restart unexpectedly attempted another provider request')
        return ModelProvider(lambda: 'injected-offline-test-key', transport=transport)

    def _agent(self, store, response=None):
        if response is None:
            provider = self._provider(None)
        else:
            def transport(body, key):
                raise AssertionError('unexpected provider request after restart')
            provider = ModelProvider(lambda: 'injected-offline-test-key', transport=transport)
        return ResponsesAgent(store, provider, clock=lambda: NOW)

    def _table_counts(self, store):
        with store.connection() as db:
            tables = ('cascade_calls', 'cascade_call_rates', 'component_calls',
                      'trading_mode_calls', 'agent_responses', 'agent_tool_results',
                      'agent_completions')
            return {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                    for table in tables}

    def _model_ledger(self, store):
        with store.connection() as db:
            calls = [dict(row) for row in db.execute('SELECT status,reserved,charged FROM cascade_calls')]
            responses = [dict(row) for row in db.execute('SELECT status,call_id FROM agent_responses')]
        return calls, responses

    def test_crash_after_admission_charges_unknown_ceiling_and_never_reissues(self):
        code = r'''import os, sys
from prism_runtime.agent_runtime import ResponsesAgent
from prism_runtime.provider import ModelProvider
from prism_runtime.store import Store
db_path, marker = sys.argv[1:]
store = Store(db_path, clock=lambda: 1800000000.0)
def transport(body, key):
    with open(marker, 'a', encoding='utf-8') as f:
        f.write('provider-request\n'); f.flush(); os.fsync(f.fileno())
    raise AssertionError('transport should not be reached')
agent = ResponsesAgent(store, ModelProvider(lambda: 'offline-test-key', transport=transport), clock=lambda: 1800000000.0)
original = agent.provider.request
def crash_after_admission(body, *, timeout_seconds=None):
    os._exit(71)
agent.provider.request = crash_after_admission
agent.structured('discovery', {'fixture': 'admission'}, {'type':'object','additionalProperties':False,'properties':{'ok':{'type':'boolean'}},'required':['ok']}, task_id='admission-task')
'''
        self._run_crashing_child(code, 71)
        store = Store(self.db_path, clock=lambda: NOW)
        self.addCleanup(store.close)
        agent = self._agent(store)
        with self.assertRaisesRegex(ProviderError, 'Uncertain previous model request'):
            agent.structured('discovery', {'fixture': 'admission'}, SCHEMA, task_id='admission-task')
        calls, responses = self._model_ledger(store)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['status'], 'unknown')
        self.assertAlmostEqual(calls[0]['charged'], calls[0]['reserved'])
        self.assertEqual(responses[0]['status'], 'running')
        self.assertEqual(self._table_counts(store), {
            'cascade_calls': 1, 'cascade_call_rates': 1, 'component_calls': 1,
            'trading_mode_calls': 0, 'agent_responses': 1,
            'agent_tool_results': 0, 'agent_completions': 0,
        })
        self.assertEqual(self.marker.read_text() if self.marker.exists() else '', '')
        self.assertEqual(agent.budget.state()['discovery']['running'], 0)

    def test_crash_after_settlement_before_receipt_finalization_is_not_reissued(self):
        code = r'''import json, os, sys
from prism_runtime.agent_runtime import ResponsesAgent
from prism_runtime.provider import ModelProvider
from prism_runtime.store import Store
db_path, marker = sys.argv[1:]
store = Store(db_path, clock=lambda: 1800000000.0)
def transport(body, key):
    with open(marker, 'a', encoding='utf-8') as f:
        f.write('provider-request\n'); f.flush(); os.fsync(f.fileno())
    return {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'{"ok":true}'}]}], 'usage':{'input_tokens':100,'output_tokens':20}}
agent = ResponsesAgent(store, ModelProvider(lambda: 'offline-test-key', transport=transport), clock=lambda: 1800000000.0)
settle = store.settle
def settle_then_crash(call_id, usage=None, **kwargs):
    settle(call_id, usage, **kwargs)
    os._exit(72)
store.settle = settle_then_crash
agent.structured('discovery', {'fixture': 'settled'}, {'type':'object','additionalProperties':False,'properties':{'ok':{'type':'boolean'}},'required':['ok']}, task_id='settled-task')
'''
        self._run_crashing_child(code, 72)
        self.assertEqual(self.marker.read_text().splitlines(), ['provider-request'])
        store = Store(self.db_path, clock=lambda: NOW)
        self.addCleanup(store.close)
        agent = self._agent(store)
        with self.assertRaisesRegex(ProviderError, 'Uncertain previous model request'):
            agent.structured('discovery', {'fixture': 'settled'}, SCHEMA, task_id='settled-task')
        calls, responses = self._model_ledger(store)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['status'], 'settled')
        self.assertGreater(calls[0]['charged'], 0)
        self.assertLess(calls[0]['charged'], calls[0]['reserved'])
        self.assertEqual(responses[0]['status'], 'running')
        self.assertEqual(agent.budget.state()['discovery']['running'], 0)
        self.assertEqual(len(self.marker.read_text().splitlines()), 1)

    def test_durable_response_replays_tool_once_without_second_paid_request(self):
        code = r'''import json, os, sys
from prism_runtime.agent_runtime import ResponsesAgent
from prism_runtime.provider import ModelProvider
from prism_runtime.store import Store
db_path, marker = sys.argv[1:]
store = Store(db_path, clock=lambda: 1800000000.0)
def transport(body, key):
    with open(marker, 'a', encoding='utf-8') as f:
        f.write('provider-request\n'); f.flush(); os.fsync(f.fileno())
    return {'status':'completed','output':[{'type':'function_call','name':'record_local_action','arguments':'{"identity":"action-1"}','call_id':'call-1'}], 'usage':{'input_tokens':100,'output_tokens':20}}
agent = ResponsesAgent(store, ModelProvider(lambda: 'offline-test-key', transport=transport), clock=lambda: 1800000000.0)
response = agent._response
def response_then_crash(*args, **kwargs):
    result = response(*args, **kwargs)
    os._exit(73)
agent._response = response_then_crash
agent.tools({'fixture':'durable-response'}, [{'type':'function','name':'record_local_action','description':'fixture','parameters':{'type':'object','additionalProperties':False,'properties':{'identity':{'type':'string'}},'required':['identity']}}], lambda *args: {'status':'ok'}, task_id='tool-replay-task', max_rounds=1)
'''
        self._run_crashing_child(code, 73)
        self.assertEqual(self.marker.read_text().splitlines(), ['provider-request'])
        store = Store(self.db_path, clock=lambda: NOW)
        self.addCleanup(store.close)
        agent = self._agent(store)
        actions = []

        def handler(name, arguments, tool_id):
            actions.append((name, arguments, tool_id))
            return {'status': 'accepted', 'fixture': True}

        first = agent.tools({'fixture': 'durable-response'}, TOOL_DEFINITIONS, handler,
                            task_id='tool-replay-task', max_rounds=1)
        self.assertEqual(first['tools'][0]['result'], {'status': 'accepted', 'fixture': True})
        self.assertEqual(len(actions), 1)
        second = agent.tools({'fixture': 'durable-response'}, TOOL_DEFINITIONS, handler,
                             task_id='tool-replay-task', max_rounds=1)
        self.assertEqual(second, first)
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(self.marker.read_text().splitlines()), 1)
        calls, responses = self._model_ledger(store)
        self.assertEqual((len(calls), calls[0]['status']), (1, 'settled'))
        self.assertEqual((len(responses), responses[0]['status']), (1, 'completed'))
        counts = self._table_counts(store)
        self.assertEqual(counts['agent_tool_results'], 1)
        self.assertEqual(counts['agent_completions'], 1)
        self.assertEqual(counts['cascade_calls'], counts['component_calls'])
        self.assertEqual(agent.budget.state()['execution']['running'], 0)

    def test_paper_exchange_accept_then_lost_ack_replays_same_order_identity(self):
        code = r'''import os, sys
from urllib.parse import urlsplit
from prism_runtime.agent_runtime import ResponsesAgent
from prism_runtime.bybit import BybitAdapter
from prism_runtime.paper_exchange import PaperExchange
from prism_runtime.provider import ModelProvider
from prism_runtime.store import Store
from test_bybit import FixtureTransport
db_path, marker = sys.argv[1:]
now = 1800000000.0
store = Store(db_path, clock=lambda: now)
config = {'bybit': {'mode':'paper','enabled':False,'mainnet_enabled':False,
    'account_id':'fixture-account','account_type':'UNIFIED','position_mode':'one_way',
    'allowed_categories':['spot'],'allowed_instruments':[{'category':'spot','symbol':'BTCUSDT'}]},
    'live_execution': {'require_exchange_protection':True}}
paper = PaperExchange(store, public_transport=FixtureTransport(), clock=lambda: now)
class LostAcknowledgement:
    def request(self, method, url, **kwargs):
        result = paper.request(method, url, **kwargs)
        if urlsplit(url).path == '/v5/order/create':
            os._exit(74)
        return result
adapter = BybitAdapter(store, config, transport=LostAcknowledgement(),
    credential_loader=lambda mode: (_ for _ in ()).throw(AssertionError('paper must not read credentials')),
    clock=lambda: now)
def model_transport(body, key):
    with open(marker, 'a', encoding='utf-8') as f:
        f.write('provider-request\n'); f.flush(); os.fsync(f.fileno())
    return {'status':'completed','output':[{'type':'function_call','name':'submit_paper_order',
        'arguments':'{"category":"spot","symbol":"BTCUSDT","side":"Buy","quantity":"0.10","price":"100","order_link_id":"crash-order-link"}',
        'call_id':'paper-call'}], 'usage':{'input_tokens':100,'output_tokens':20}}
agent = ResponsesAgent(store, ModelProvider(lambda: 'offline-test-key', transport=model_transport), clock=lambda: now)
def handler(name, args, tool_id):
    return adapter.submit_order({**args, 'order_type':'Limit'})
definitions = [{'type':'function','name':'submit_paper_order','description':'fixture',
    'parameters':{'type':'object','additionalProperties':False,'properties':{
        'category':{'type':'string'},'symbol':{'type':'string'},'side':{'type':'string'},
        'quantity':{'type':'string'},'price':{'type':'string'},'order_link_id':{'type':'string'}},
        'required':['category','symbol','side','quantity','price','order_link_id']}}]
agent.tools({'fixture':'paper-lost-ack'}, definitions, handler,
    task_id='paper-lost-ack-task', max_rounds=1)
'''
        self._run_crashing_child(code, 74)
        self.assertEqual(self.marker.read_text().splitlines(), ['provider-request'])

        from prism_runtime.bybit import BybitAdapter
        from prism_runtime.paper_exchange import PaperExchange
        from test_bybit import FixtureTransport

        store = Store(self.db_path, clock=lambda: NOW)
        self.addCleanup(store.close)
        config = {'bybit': {'mode': 'paper', 'enabled': False, 'mainnet_enabled': False,
                    'account_id': 'fixture-account', 'account_type': 'UNIFIED',
                    'position_mode': 'one_way', 'allowed_categories': ['spot'],
                    'allowed_instruments': [{'category': 'spot', 'symbol': 'BTCUSDT'}]},
                  'live_execution': {'require_exchange_protection': True}}
        paper = PaperExchange(store, public_transport=FixtureTransport(), clock=lambda: NOW)
        adapter = BybitAdapter(store, config, transport=paper,
            credential_loader=lambda mode: self.fail('paper must not read credentials'),
            clock=lambda: NOW)
        agent = self._agent(store)
        handler_calls = []

        def handler(name, arguments, tool_id):
            handler_calls.append((name, tool_id))
            return adapter.submit_order({**arguments, 'order_type': 'Limit'})

        replay = agent.tools({'fixture': 'paper-lost-ack'}, PAPER_ORDER_TOOL, handler,
                             task_id='paper-lost-ack-task', max_rounds=1)
        self.assertEqual(len(handler_calls), 1)
        self.assertEqual(replay['tools'][0]['name'], 'submit_paper_order')
        self.assertEqual(replay['tools'][0]['result']['order_link_id'], 'crash-order-link')
        reconciled = adapter.reconcile_order('crash-order-link')
        self.assertEqual(reconciled['status'], 'open')
        state = paper._load()
        self.assertEqual(len(state['orders']), 1)
        self.assertIn('crash-order-link', state['orders'])
        self.assertEqual(len(adapter.orders(limit=10)), 1)
        self.assertEqual(adapter.local_order('crash-order-link')['status'], 'open')
        self.assertEqual(len(self.marker.read_text().splitlines()), 1)
        counts = self._table_counts(store)
        self.assertEqual(counts['agent_tool_results'], 1)
        self.assertEqual(counts['cascade_calls'], 1)
        self.assertEqual(counts['component_calls'], 1)
        self.assertEqual(agent.budget.state()['execution']['running'], 0)

    def test_sol_controller_crash_after_durable_approval_blocks_changed_packet_safely(self):
        # Injected eligibility is used only to reach the controller/reviewer
        # path. This fixture does not claim that the strategy is qualified.
        code = r'''import os, sys
from pathlib import Path
from prism_runtime.agent_runtime import ResponsesAgent
from prism_runtime.bybit import BybitAdapter
from prism_runtime.live_execution import ExecutionController
from prism_runtime.orchestrator import Runtime
from prism_runtime.paper_exchange import PaperExchange
from prism_runtime.provider import ModelProvider
from prism_runtime.research_memory import ResearchMemory
from prism_runtime.store import Store
from test_bybit import FixtureTransport
from test_live_execution import NOW_SECONDS
from test_sol_swarm_e2e import OfflineResponses, SolMarketFixture
db_path, marker = sys.argv[1:]
now = [NOW_SECONDS]
clock = lambda: now[0]
store = Store(Path(db_path), clock=clock)
base_responses = OfflineResponses(clock)
def transport(body, key):
    assert key == 'sol-swarm-offline-only'
    format_name = body.get('text', {}).get('format', {}).get('name', 'tools')
    with open(marker, 'a', encoding='utf-8') as f:
        f.write(body['model'] + ':' + format_name + '\n'); f.flush(); os.fsync(f.fileno())
    return base_responses(body, key)
provider = ModelProvider(lambda: 'sol-swarm-offline-only', transport)
runtime = Runtime(store, key_loader=lambda: (_ for _ in ()).throw(AssertionError('no credential lookup')),
    provider=provider, collector=object())
runtime.update_configuration({'trading_mode': {'selected': 'sol_perpetual'}})
strategy_id = store.get_document('sol_mode_strategy_id')
runtime.update_configuration({
    'bybit': {'mode':'paper','enabled':True,'mainnet_enabled':False,
        'account_id':'offline-paper-account','allowed_categories':['linear'],
        'allowed_instruments':[{'category':'linear','symbol':'SOLUSDT'}]},
    'live_execution': {'enabled':True,'eligible_strategy_versions':[strategy_id],
        'allocations':{strategy_id:100.},'max_portfolio_notional':100.,
        'max_position_notional':100.,'max_margin_fraction':1.,'max_spread_bps':10.,
        'max_slippage_bps':30.,'max_participation_fraction':.01,'max_leverage':1.,
        'require_exchange_protection':True}})
paper = PaperExchange(store, public_transport=FixtureTransport(), clock=clock, initial_equity=100.)
adapter = BybitAdapter(store, store.config(), transport=paper,
    credential_loader=lambda mode: (_ for _ in ()).throw(AssertionError('paper must not load credentials')),
    clock=clock)
market = SolMarketFixture(store, clock, adapter)
memory = ResearchMemory(store, clock=clock)
agents = ResponsesAgent(store, provider, clock=clock)
controller = ExecutionController(store, adapter, market, memory, agents,
    eligibility=lambda sid: {'eligible': sid == strategy_id, 'status':'injected-fixture', 'reasons':[]},
    clock=clock)
registered = store.experiment(strategy_id)['registered_at_ms']
now[0] = registered / 1000 + 62
snapshot = market.publish(strategy_id, registered)
paper.advance([snapshot])
event = {'id':'controller-review-crash-event','kind':'candle_closed','strategy_id':strategy_id,
    'category':'linear','symbol':'SOLUSDT','snapshot_id':snapshot['snapshot_id'],
    'signal_input_id':snapshot['signal_input_id'],
    'attention_deadline_ms':snapshot['attention_deadline_ms']}
prepare = controller._prepare_trade_review
def approve_then_crash(*args, **kwargs):
    approval = prepare(*args, **kwargs)
    if isinstance(approval, dict) and approval.get('status') == 'approved':
        os._exit(75)
    return approval
controller._prepare_trade_review = approve_then_crash
controller.handle_event(event)
'''
        self._run_crashing_child(code, 75)
        marker_lines = self.marker.read_text().splitlines()
        self.assertEqual(len(marker_lines), 2, marker_lines)
        self.assertEqual(sum(line.startswith('gpt-6.1-sol:') for line in marker_lines), 1)
        self.assertEqual(sum(line == 'gpt-6-luna:prism_trade_review' for line in marker_lines), 1)

        from prism_runtime.agent_runtime import ResponsesAgent
        from prism_runtime.bybit import BybitAdapter
        from prism_runtime.live_execution import ExecutionController
        from prism_runtime.paper_exchange import PaperExchange
        from prism_runtime.research_memory import ResearchMemory
        from test_bybit import FixtureTransport
        from test_live_execution import NOW_SECONDS
        from test_sol_swarm_e2e import OfflineResponses, SolMarketFixture

        now = [NOW_SECONDS]
        clock = lambda: now[0]
        store = Store(self.db_path, clock=clock)
        self.addCleanup(store.close)
        strategy_id = store.get_document('sol_mode_strategy_id')
        registered = store.experiment(strategy_id)['registered_at_ms']
        now[0] = registered / 1000 + 62
        fixture_responses = OfflineResponses(clock)

        def unexpected_provider_call(body, key):
            with open(self.marker, 'a', encoding='utf-8') as f:
                f.write(body['model'] + ':' + body.get('text', {}).get('format', {}).get('name', 'tools') + '\n')
                f.flush()
                os.fsync(f.fileno())
            return fixture_responses(body, key)

        provider = ModelProvider(lambda: 'sol-swarm-offline-only', unexpected_provider_call)
        agents = ResponsesAgent(store, provider, clock=clock)
        paper = PaperExchange(store, public_transport=FixtureTransport(), clock=clock, initial_equity=100.)
        adapter = BybitAdapter(store, store.config(), transport=paper,
            credential_loader=lambda mode: self.fail('paper must not load credentials'), clock=clock)
        market = SolMarketFixture(store, clock, adapter)
        snapshot = market.publish(strategy_id, registered)
        memory = ResearchMemory(store, clock=clock)
        controller = ExecutionController(store, adapter, market, memory, agents,
            eligibility=lambda sid: {'eligible': sid == strategy_id,
                'status': 'injected-fixture', 'reasons': []}, clock=clock)
        event = {'id':'controller-review-crash-event','kind':'candle_closed','strategy_id':strategy_id,
            'category':'linear','symbol':'SOLUSDT','snapshot_id':snapshot['snapshot_id'],
            'signal_input_id':snapshot['signal_input_id'],
            'attention_deadline_ms':snapshot['attention_deadline_ms']}

        replay = controller.handle_event(event)
        # The same event may no longer produce the byte-identical Sol packet:
        # the reviewer request has now increased research spend in the dossier.
        # Request identity therefore blocks replay before the old proposal can
        # dispatch. This is the permitted conservative outcome after a crash.
        self.assertEqual(replay['status'], 'blocked', replay)
        self.assertIn('ProviderError', replay['rationale'])
        self.assertEqual(len(adapter.orders(category='linear', symbol='SOLUSDT')), 0)
        self.assertEqual(len(paper._load()['orders']), 0)
        with store.connection() as db:
            review_rows = db.execute('SELECT status,decision FROM trade_reviews').fetchall()
            mutation_rows = db.execute('SELECT status,order_link_id FROM execution_mutations').fetchall()
            decision_rows = db.execute('SELECT status FROM execution_decisions').fetchall()
        self.assertEqual(len(review_rows), 1)
        self.assertEqual((review_rows[0]['status'], review_rows[0]['decision']), ('approved', 'approve'))
        self.assertEqual(mutation_rows, [])
        self.assertEqual(len(decision_rows), 1)
        self.assertEqual(decision_rows[0]['status'], 'blocked')
        final_lines = self.marker.read_text().splitlines()
        self.assertEqual(sum(line == 'gpt-6-luna:prism_trade_review' for line in final_lines), 1)
        # Neither the initial Sol decision nor its reviewer is requested again.
        self.assertEqual(sum(line.startswith('gpt-6.1-sol:') for line in final_lines), 1)
        self.assertEqual(agents.budget.state()['execution']['calls'], 1)
        self.assertEqual(agents.budget.state()['trade_review']['calls'], 1)
        self.assertEqual(agents.budget.state()['execution']['running'], 0)
        self.assertEqual(agents.budget.state()['trade_review']['running'], 0)


if __name__ == '__main__':
    unittest.main()
