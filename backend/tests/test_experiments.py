from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
import unittest

from prism_runtime.store import Store
from prism_runtime.simulation import execution_policy
from prism_runtime.experiments import forward_report
from test_forward_report import observations


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'registry.sqlite3'
        self.clock_ms=1_760_000_000_000
        self.store=Store(self.path,clock=lambda:self.clock_ms/1000)
        self.spec={'symbol':'MRVL','kind':'momentum','parameters':{'lookback':3,'threshold':.002}}
        self.config={'research':{'max_holding_sessions':5}}
        self.registered=self.clock_ms

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, spec=None):
        return self.store.register_experiment(spec or self.spec,policy=execution_policy(self.config),
            context={'config':self.config},registered_at_ms=self.registered-400*86400000)

    def test_concurrent_duplicate_registration_is_one_frozen_experiment(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows=list(pool.map(lambda _:self.register(),range(20)))
        self.assertEqual(len({r['id'] for r in rows}),1)
        self.assertEqual(len(self.store.experiments()),1)
        self.assertEqual(rows[0]['registered_at_ms'],self.registered)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM experiment_attempts').fetchone()[0],20)

    def test_alpha_allocations_sum_to_at_most_lifetime_budget(self):
        for i in range(100):
            self.register({**self.spec,'symbol':'T'+str(i)})
        self.assertLess(sum(row['alpha_budget'] for row in self.store.experiments()),.05)

    def test_observations_are_future_only_monotonic_and_immutable(self):
        experiment=self.register()
        self.clock_ms=self.registered+10*86400000
        row={'session_ms':self.registered+86400000,'observed_at_ms':self.registered+86401000,
             'equity':100000.,'initial_equity':100000.,'valuation_complete':True}
        first=self.store.record_forward(experiment['id'],row)
        self.assertEqual(first,self.store.record_forward(experiment['id'],row))
        self.assertEqual(first['provenance']['status'],'unattested')
        for change in ({'equity':100001.},{'session_ms':self.registered},
                       {'session_ms':self.clock_ms+10000},{'equity':float('nan')}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.store.record_forward(experiment['id'],{**row,**change})
        later={**row,'session_ms':self.registered+3*86400000,'observed_at_ms':self.registered+3*86400000+1000}
        self.store.record_forward(experiment['id'],later)
        with self.assertRaises(ValueError):
            self.store.record_forward(experiment['id'],{**row,'session_ms':self.registered+2*86400000,'observed_at_ms':self.registered+2*86400000+1000})

    def test_forged_historical_success_cannot_promote(self):
        experiment=self.register()
        self.store.record_evaluation(experiment['id'],{'phase':'forward','report':{'status':'promoted'}})
        with self.assertRaises(ValueError):
            self.store.update_experiment_status(experiment['id'],'promoted','Unmeasured claim')

    def test_caller_backdate_does_not_make_old_sessions_eligible(self):
        experiment=self.register()
        self.assertEqual(experiment['registered_at_ms'],self.registered)
        old={'session_ms':self.registered-86400000,'observed_at_ms':self.registered,
             'equity':100000.,'initial_equity':100000.,'valuation_complete':True}
        with self.assertRaises(ValueError):
            self.store.record_forward(experiment['id'],old)

    def test_positive_but_unattested_historical_report_cannot_promote(self):
        experiment=self.register()
        rows=observations()
        self.clock_ms=self.registered+61*86400000
        for i,row in enumerate(rows):
            row['session_ms']=self.registered+(i+1)*86400000
            row['observed_at_ms']=row['session_ms']+1000
            self.store.record_forward(experiment['id'],row)
        report=forward_report(rows,{**self.config,'research':{**self.config['research'],'alpha_budget':experiment['alpha_budget']}})
        self.assertEqual(report['status'],'promoted')
        self.store.record_evaluation(experiment['id'],{'phase':'forward','report':report})
        with self.assertRaisesRegex(ValueError,'captured proof|attested'):
            self.store.update_experiment_status(experiment['id'],'promoted','Prospective checkpoint')
        reopened=Store(self.path)
        saved=reopened.experiment(experiment['id'])
        self.assertEqual(saved['status'],'registered')
        self.assertEqual(len(saved['observations']),60)
        self.assertTrue(all(row['provenance']['status']=='unattested' for row in saved['observations']))

    def test_snapshots_cost_attribution_and_active_results_are_retained(self):
        cutoff=self.clock_ms-1
        snapshot=self.store.save_snapshot('fixture',{'input':'frozen'},cutoff)
        self.assertEqual(snapshot,self.store.save_snapshot('fixture',{'input':'frozen'},cutoff))
        self.assertEqual(self.store.snapshot(snapshot)['value'],{'input':'frozen'})
        with self.assertRaises(ValueError):
            self.store.save_snapshot('fixture', {'input': 'not observed yet'}, self.clock_ms + 1)
        self.store.update_config({'research':{'active_collectors':3}})
        self.assertEqual(self.store.config()['research']['min_forward_trades'],8)
        call=self.store.reserve('gpt-6-luna',100,100,context={'cycle_id':'cycle','agent_id':'collector_01'})
        self.store.settle(call,{'input_tokens':100,'output_tokens':100})
        self.assertGreater(self.store.cycle_cost('cycle'),0)
        self.assertEqual(self.store.research_summary()['operating_costs']['calls'],1)
        self.store.save_strategy({'id':'active','status':'shadow'})
        for i in range(510):
            self.store.save_strategy({'id':str(i),'status':'rejected'})
        self.assertTrue(any(row['id']=='active' for row in self.store.strategies()))

    def test_quote_updates_and_frozen_experiments_share_immutable_series(self):
        from test_simulation import daily_market
        market=daily_market(20)
        self.store.set_document('markets',[market])
        first=self.store.freeze_markets([market],self.clock_ms-1)
        headers=self.store.markets(history=False)
        self.assertNotIn('candles',headers[0])
        headers[0].update(price=123.,as_of_ms=self.clock_ms)
        self.store.set_document('markets',headers)
        self.assertEqual(self.store.get_document('markets')[0]['candles'],market['candles'])
        self.assertEqual(self.store.market_snapshot(first)[0]['candles'],market['candles'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM market_series').fetchone()[0],1)
