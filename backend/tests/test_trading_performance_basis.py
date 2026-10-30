import copy
from pathlib import Path
import tempfile
import unittest

from prism_runtime.domain import timestamp
from prism_runtime.experiments import forward_report
from prism_runtime.live_research import LiveResearch
from prism_runtime.store import Store


def market_policy():
    return {'version': 2, 'venue': 'bybit', 'category': 'linear', 'symbol': 'SOLUSDT',
            'primary_timeframe': '1', 'context_timeframes': ['5', '60'], 'history_bars': 120,
            'indicators': [{'kind': 'sma', 'period': 20}], 'signal_mode': 'closed',
            'max_data_age_seconds': 10, 'max_decision_latency_seconds': 30,
            'attention_events': ['candle_closed', 'fill', 'order', 'position', 'risk', 'data_health']}


def observations(days=60, *, equity=101., costs=2., complete=False):
    start = 1_793_145_600_000
    rows = []
    for day in range(days):
        rows.append({'phase': 'forward', 'session_ms': start + day * 86_400_000,
                     'initial_equity': 100., 'equity': equity,
                     'completed_trades': 8, 'valuation_complete': True,
                     'source_cost_complete': complete, 'data_cost_usd': costs,
                     'infrastructure_cost_usd': 0., 'research_cost_usd': 0.,
                     'benchmark_return': 0., 'market_return': 0., 'sector_return': 0.})
    return rows


class TradingPerformanceBasisTests(unittest.TestCase):
    def test_trading_basis_excludes_operating_spend_from_metrics_but_reports_it(self):
        rows = observations(complete=True)
        operating = forward_report(rows, performance_basis='operating')
        trading = forward_report(rows, performance_basis='trading')
        self.assertEqual(operating['status'], 'retired')
        self.assertEqual(trading['status'], 'inconclusive')
        self.assertEqual(trading['performance_basis'], 'trading')
        self.assertAlmostEqual(trading['trading_pnl_usd'], 1.)
        self.assertAlmostEqual(trading['operating_cost_usd'], 120.)
        self.assertAlmostEqual(trading['operating_pnl_usd'], -119.)
        self.assertAlmostEqual(trading['net_return'], .01)
        self.assertEqual(len(trading['statistics']), 3)
        self.assertNotIn('Operating capital exhausted', trading['reasons'])

    def test_trading_basis_does_not_require_complete_imported_source_costs(self):
        result = forward_report(observations(complete=False), performance_basis='trading')
        self.assertEqual(result['performance_basis'], 'trading')
        self.assertIsNotNone(result['trading_pnl_usd'])
        self.assertNotIn('Imported source costs are incomplete; promotion requires known operating costs', result['reasons'])
        self.assertEqual(forward_report(observations(complete=False))['reasons'],
                         ['Imported source costs are incomplete; promotion requires known operating costs'])

    def test_registration_preserves_legacy_policy_identity_and_freezes_explicit_identity(self):
        temp = tempfile.TemporaryDirectory()
        try:
            now = [1_793_145_600.]
            store = Store(Path(temp.name) / 'research.sqlite3', clock=lambda: now[0])
            try:
                store.save_evidence([{'id': 'source', 'source_url': 'https://example.org/fixture',
                    'content': 'Offline synthetic evidence.', 'observed_at': timestamp(now[0]), 'symbols': ['SOLUSDT']}])
                research = LiveResearch(store, clock=lambda: now[0])
                spec = {'symbol': 'SOLUSDT', 'kind': 'momentum', 'parameters': {'lookback': 3, 'threshold': 0.}}
                legacy = research.register(spec, market_policy(), hypothesis='Legacy operating fixture', evidence_ids=['source'])
                self.assertNotIn('performance_basis', legacy['policy'])
                self.assertNotIn('execution_identity', legacy['policy'])
                self.assertTrue(research._implemented_policy(legacy))
                identity = {'mode': 'sol_perpetual', 'trader_model': 'gpt-6.1-sol',
                    'trader_reasoning_effort': 'xhigh', 'support_model': 'gpt-6-luna',
                    'support_reasoning_effort': 'max', 'swarm_size': 4, 'protocol_version': 1}
                frozen = research.register(spec, market_policy(), hypothesis='Trading basis fixture',
                    evidence_ids=['source'], initial_equity=100., performance_basis='trading',
                    execution_identity=identity)
                self.assertEqual(frozen['policy']['initial_equity'], 100.)
                self.assertEqual(frozen['policy']['performance_basis'], 'trading')
                self.assertEqual(frozen['policy']['execution_identity'], identity)
                self.assertTrue(research._implemented_policy(frozen))
                changed = copy.deepcopy(frozen)
                changed['policy']['execution_identity']['trader_model'] = 'x' * 129
                self.assertFalse(research._implemented_policy(changed))
            finally:
                store.close()
        finally:
            temp.cleanup()

    def test_explicit_policy_rejects_unbounded_execution_identity(self):
        temp = tempfile.TemporaryDirectory()
        try:
            now = [1_793_145_600.]
            store = Store(Path(temp.name) / 'research.sqlite3', clock=lambda: now[0])
            try:
                store.save_evidence([{'id': 'source', 'source_url': 'https://example.org/fixture',
                    'content': 'Offline synthetic evidence.', 'observed_at': timestamp(now[0]), 'symbols': ['SOLUSDT']}])
                research = LiveResearch(store, clock=lambda: now[0])
                spec = {'symbol': 'SOLUSDT', 'kind': 'momentum', 'parameters': {'lookback': 3, 'threshold': 0.}}
                with self.assertRaises(ValueError):
                    research.register(spec, market_policy(), hypothesis='Invalid identity fixture',
                        evidence_ids=['source'], initial_equity=100., performance_basis='trading',
                        execution_identity={'nested': {'not': 'bounded'}})
            finally:
                store.close()
        finally:
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
