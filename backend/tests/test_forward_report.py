import random
import unittest

from prism_runtime.experiments import forward_report


def observations(count=60):
    rng=random.Random(177)
    equity=100000.
    rows=[]
    for i in range(count):
        equity*=1+.004+rng.uniform(-.001,.001)
        rows.append({'session_ms':1700000000000+i*86400000,'equity':equity,'initial_equity':100000.,
            'valuation_complete':True,'completed_trades':(i+1)//5,'benchmark_return':.0001,
            'market_return':.0002,'sector_return':.0003})
    return rows


class ForwardReportTests(unittest.TestCase):
    def test_promotion_requires_actual_checkpoints_costs_and_independent_excess(self):
        result=forward_report(observations(),{'research':{'alpha_budget':.025}})
        self.assertEqual(result['status'],'promoted',result['reasons'])
        self.assertEqual(forward_report(observations(59))['status'],'inconclusive')
        self.assertLess(result['per_comparison_threshold'],.025)

    def test_missing_benchmarks_and_development_do_not_promote(self):
        rows=observations()
        del rows[0]['sector_return']
        self.assertEqual(forward_report(rows)['status'],'inconclusive')
        rows=observations()
        rows[0]['phase']='development'
        self.assertEqual(forward_report(rows)['status'],'inconclusive')

    def test_positive_trading_return_can_fail_operating_economics(self):
        rows=observations()
        for row in rows:
            row['research_cost_usd']=1000.
        result=forward_report(rows)
        self.assertNotEqual(result['status'],'promoted')
        self.assertLess(result['operating_pnl_usd'],0)

    def test_strategy_must_beat_sector_and_market_not_only_cash(self):
        rows=observations()
        for row in rows:
            row['sector_return']=.02
        result=forward_report(rows)
        self.assertEqual(result['status'],'inconclusive')
        self.assertGreater(result['net_return'],0)

    def test_duplicate_sessions_and_incomplete_marks_are_rejected(self):
        rows=observations()
        rows[1]['session_ms']=rows[0]['session_ms']
        with self.assertRaises(ValueError):
            forward_report(rows)
        rows=observations()
        rows[0]['valuation_complete']=False
        self.assertEqual(forward_report(rows)['status'],'inconclusive')
