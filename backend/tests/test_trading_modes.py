import unittest
from prism_runtime.trading_modes import (
    MODE_DEFAULTS, describe, is_sol_mode, mode_settings, resource_limits,
    sol_market_policy, sol_mode_strategy_spec, validate_mode_update,
)

class TradingModeTests(unittest.TestCase):
    def test_defaults_and_partial_updates_preserve_research(self):
        self.assertEqual(mode_settings({}), MODE_DEFAULTS)
        self.assertFalse(is_sol_mode({}))
        update = validate_mode_update({'selected': 'sol_perpetual', 'swarm_size': 6})
        self.assertEqual(update, {'selected': 'sol_perpetual', 'swarm_size': 6})
        self.assertEqual(mode_settings({'trading_mode': update})['support_timeout_seconds'], 15)

    def test_invalid_limits_and_unknown_modes_fail_closed(self):
        for key, value in [('selected', 'other'), ('extra', True), ('swarm_size', 0),
                           ('swarm_size', 9), ('swarm_size', True), ('support_timeout_seconds', 0),
                           ('support_timeout_seconds', 31), ('support_max_age_seconds', 0),
                           ('support_max_age_seconds', 121), ('lifetime_model_budget_usd', 0),
                           ('lifetime_model_budget_usd', float('nan'))]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_mode_update({key: value})

    def test_exact_sol_linear_policy_and_canonical_rule(self):
        policy = sol_market_policy()
        self.assertEqual({k: policy[k] for k in ('version', 'venue', 'category', 'symbol')},
                         {'version': 2, 'venue': 'bybit', 'category': 'linear', 'symbol': 'SOLUSDT'})
        self.assertEqual(policy['primary_timeframe'], '1')
        self.assertEqual(policy['context_timeframes'], ['5', '60'])
        self.assertEqual(policy['signal_mode'], 'closed')
        self.assertEqual(policy['history_bars'], 300)
        self.assertEqual(sol_mode_strategy_spec()['parameters']['lookback'], 3)

    def test_resource_overrides_are_scoped_and_copied(self):
        base = {'model': 'base', 'concurrency': 7, 'daily_usd': None, 'other': [1]}
        research = resource_limits({}, 'execution', base)
        self.assertEqual(research, base)
        self.assertIsNot(research['other'], base['other'])
        config = {'trading_mode': {'selected': 'sol_perpetual', 'swarm_size': 5}}
        trader = resource_limits(config, 'execution', base)
        support = resource_limits(config, 'support', base)
        trade_review = resource_limits(config, 'trade_review', base)
        self.assertEqual((trader['model'], trader['reasoning_effort'], trader['concurrency']),
                         ('gpt-6.1-sol', 'xhigh', 1))
        self.assertEqual((support['model'], support['reasoning_effort'], support['concurrency']),
                         ('gpt-6-luna', 'max', 5))
        self.assertEqual((trade_review['model'], trade_review['reasoning_effort']),
                         ('gpt-6-luna', 'max'))
        self.assertEqual(base['model'], 'base')

    def test_ui_contract_and_no_live_authority(self):
        info = describe({'trading_mode': {'selected': 'sol_perpetual'}})
        self.assertEqual(info['trader_model'], 'gpt-6.1-sol')
        self.assertEqual(info['trader_reasoning_effort'], 'xhigh')
        self.assertEqual(info['support_model'], 'gpt-6-luna')
        self.assertEqual(info['support_reasoning_effort'], 'max')
        self.assertEqual(info['symbol'], 'SOLUSDT')
        self.assertEqual(info['swarm_size'], 4)
        self.assertTrue(info['not_live_authority'])
        self.assertEqual(info['pnl_basis'], 'trading_after_venue_costs')

if __name__ == '__main__':
    unittest.main()
