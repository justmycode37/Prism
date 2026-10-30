import copy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import unittest

from prism_runtime.analytics import canonical_spec, spec_hash, signal_target
from prism_runtime.execution import execute, initial_portfolio
from prism_runtime.simulation import replay, replay_portfolio, policy_hash


def daily_market(count=100):
    zone = ZoneInfo('America/New_York')
    day = datetime(2024, 1, 2, tzinfo=zone)
    bars = []
    for i in range(count):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        start = int(day.timestamp()*1000)
        opening = int(day.replace(hour=9,minute=30).timestamp()*1000)
        closing = int(day.replace(hour=16).timestamp()*1000)
        price = 100+i
        bars.append({'start_ms':start,'end_ms':start+86400000,'open_ms':opening,'close_ms':closing,
            'open':price,'close':price+.5,'high':price+1,'low':price-.5,'volume':1_000_000,'closed':True,'asset_class':'equity'})
        day += timedelta(days=1)
    return {'symbol':'MRVL','asset_class':'equity','exchange_timezone':'America/New_York','interval_ms':86400000,
        'history_adjustment':'adjusted_close','raw_history_basis':'unadjusted','candles':copy.deepcopy(bars),'raw_candles':copy.deepcopy(bars),'corporate_actions':[]}


def frame(market,index,kind):
    row=market['raw_candles'][index]
    at=row['open_ms'] if kind=='open' else row['close_ms'] if kind=='mark' else row['end_ms']
    visible={**market,'candles':[b for b in market['candles'] if b['end_ms']<=at],
        'raw_candles':[b for b in market['raw_candles'] if b['end_ms']<=at],
        'price':row['open'] if kind=='open' else row['close'],
        'as_of_ms':row['close_ms'] if kind=='decision' else at,
        'session':{'open_ms':row['open_ms'],'close_ms':row['close_ms'],'open_price':row['open']}}
    return at,visible


def rule(market,registered,**changes):
    spec=canonical_spec({'symbol':market['symbol'],'kind':'momentum','parameters':{'lookback':3,'threshold':.002}})
    return {'id':'candidate','status':'paper','shadow':True,'spec':spec,'registered_at_ms':registered,
        'policy_hash':policy_hash({}),'validation':{'passed':True,'spec_hash':spec_hash(spec)},**changes}


class SimulationTests(unittest.TestCase):
    def test_holding_limit_counts_the_entry_session(self):
        market=daily_market(30)
        spec=rule(market,1)['spec']
        report=replay(spec,[market],{'research':{'max_holding_sessions':3}})
        buy=next(row for row in report['portfolio']['trades'] if row['side']=='buy')
        sell=next(row for row in report['portfolio']['trades'] if row['side']=='sell')
        opens=[row['open_ms'] for row in market['raw_candles']]
        self.assertEqual(opens.index(sell['timestamp_ms'])-opens.index(buy['timestamp_ms']),3)

    def test_portfolio_replay_applies_correlated_exposure_and_cost_caps(self):
        market=daily_market(70)
        other=copy.deepcopy(market)
        other['symbol']='MU'
        specs=[rule(market,1)['spec'],{**rule(market,1)['spec'],'symbol':'MU'}]
        report=replay_portfolio(specs,[market,other])
        buys=[row for row in report['portfolio']['trades'] if row['side']=='buy']
        first_time=min(row['timestamp_ms'] for row in buys)
        first=[row for row in buys if row['timestamp_ms']==first_time]
        opening=next(row['open'] for row in market['raw_candles'] if row['open_ms']==first_time)
        gross=sum(row['quantity']*opening for row in first)
        costs=sum(row['fee']+row['quantity']*(row['price']-opening) for row in first)
        self.assertLessEqual(gross/(100000-costs),.10000001)
        self.assertGreater(report['portfolio']['fees_paid'],0)

    def test_exit_liquidity_limits_partial_fills_without_counting_round_trips(self):
        market=daily_market()
        now,quote=frame(market,10,'decision')
        strategy=rule(market,now-1)
        book=execute(initial_portfolio(),[strategy],[],[quote],{},now=now)
        opening,quote=frame(market,11,'open')
        book=execute(book,[strategy],[],[quote],{},now=opening)
        quote=copy.deepcopy(quote)
        quote['raw_candles'][-1]['volume']=100.
        quote['session']['open_ms']=opening+86400000
        quote['as_of_ms']=opening+86400000
        exited=execute(book,[strategy],[],[quote],{'risk':{'kill_switch':True}},now=opening+86400000)
        self.assertTrue(exited['positions'])
        self.assertEqual(exited['trades'][-1]['quantity'],1.)
        self.assertFalse(exited['trades'][-1]['position_closed'])
        repeat=execute(exited,[strategy],[],[quote],{'risk':{'kill_switch':True}},now=opening+86400000)
        self.assertEqual(len(exited['trades']),len(repeat['trades']))

    def test_weekend_signal_waits_for_actual_observed_session(self):
        market=daily_market()
        friday=next(i for i,b in enumerate(market['raw_candles']) if i>10 and datetime.fromtimestamp(b['open_ms']/1000,ZoneInfo('America/New_York')).weekday()==4)
        now,quote=frame(market,friday,'decision')
        strategy=rule(market,now-1)
        queued=execute(initial_portfolio(),[strategy],[],[quote],{},now=now)
        opening,quote=frame(market,friday+1,'open')
        self.assertEqual(datetime.fromtimestamp(opening/1000,ZoneInfo('America/New_York')).weekday(),0)
        result=execute(queued,[strategy],[],[quote],{},now=opening)
        self.assertEqual(len(result['trades']),1)

    def test_precommitment_is_required_and_missed_open_never_backfilled(self):
        market=daily_market()
        now,quote=frame(market,10,'decision')
        strategy=rule(market,now-1)
        queued=execute(initial_portfolio(),[strategy],[],[quote],{},now=now)
        self.assertFalse(queued['trades'])
        self.assertEqual(len(queued['pending_orders']),1)
        opening,quote=frame(market,11,'open')
        filled=execute(queued,[strategy],[],[quote],{},now=opening)
        self.assertEqual(len(filled['positions']),1)
        late=copy.deepcopy(quote)
        late['as_of_ms']=opening+180000
        missed=execute(queued,[strategy],[],[late],{},now=opening+180000)
        self.assertFalse(missed['trades'])
        self.assertTrue(any('missed opening' in w for w in missed['warnings']))
        created_after=execute(initial_portfolio(),[rule(market,opening+1)],[],[late],{},now=opening+180000)
        self.assertFalse(created_after['trades'])

    def test_replay_and_forward_use_identical_policy_ledger_and_fills(self):
        market=daily_market(70)
        spec=rule(market,1)['spec']
        result=replay(spec,[market])
        strategy=rule(market,market['raw_candles'][0]['open_ms']-1,id='replay')
        book=initial_portfolio()
        for index in range(70):
            for kind in ('open','mark','decision'):
                now,quote=frame(market,index,kind)
                book=execute(book,[strategy],[],[quote],{},now=now)
        self.assertEqual(result['portfolio']['trades'],book['trades'])
        self.assertAlmostEqual(result['portfolio']['cash'],book['cash'])
        self.assertAlmostEqual(result['portfolio']['equity'],book['equity'])
        self.assertGreater(len(book['trades']),2)
        self.assertGreater(book['fees_paid'],0)

    def test_split_and_dividend_entitlement_are_idempotent_and_cash_reconciles(self):
        market=daily_market()
        now,quote=frame(market,10,'decision')
        strategy=rule(market,now-1)
        book=execute(initial_portfolio(),[strategy],[],[quote],{},now=now)
        opening,quote=frame(market,11,'open')
        book=execute(book,[strategy],[],[quote],{},now=opening)
        quantity=book['positions'][0]['quantity']
        cash=book['cash']
        quote=copy.deepcopy(quote)
        quote.update(price=quote['price']/2-1,as_of_ms=opening+60000)
        quote['corporate_actions']=[{'id':'split','type':'split','ratio':2,'effective_at_ms':opening+1000,'available_at_ms':opening+1000},
            {'id':'div','type':'dividend','amount':1,'effective_at_ms':opening+2000,'available_at_ms':opening+2000,'payable_at_ms':opening+120000}]
        adjusted=execute(book,[strategy],[],[quote],{},now=opening+60000)
        self.assertAlmostEqual(adjusted['positions'][0]['quantity'],quantity*2)
        self.assertAlmostEqual(adjusted['cash'],cash)
        self.assertAlmostEqual(adjusted['receivables'][0]['amount'],quantity*2)
        repeated=execute(adjusted,[strategy],[],[quote],{},now=opening+60000)
        self.assertEqual(repeated['applied_actions'],['split','div'])
        self.assertEqual(repeated['equity'],adjusted['equity'])
        quote['as_of_ms']=opening+120000
        paid=execute(repeated,[strategy],[],[quote],{},now=opening+120000)
        self.assertFalse(paid['receivables'])
        self.assertAlmostEqual(paid['cash'],cash+quantity*2)
        self.assertAlmostEqual(paid['equity']-paid['initial_equity'],paid['realized_pnl']+paid['unrealized_pnl'])

    def test_policy_change_and_missing_session_metadata_cannot_authorize_fill(self):
        market=daily_market()
        now,quote=frame(market,10,'decision')
        strategy=rule(market,now-1)
        book=execute(initial_portfolio(),[strategy],[],[quote],{},now=now)
        opening,quote=frame(market,11,'open')
        changed=execute(book,[strategy],[],[quote],{'risk':{'fee_bps':12}},now=opening)
        self.assertFalse(changed['trades'])
        del quote['session']
        missing=execute(book,[strategy],[],[quote],{},now=opening)
        self.assertFalse(missing['trades'])

    def test_equivalent_id_cannot_reconsume_opening_event(self):
        market=daily_market()
        now,quote=frame(market,10,'decision')
        strategy=rule(market,now-1)
        first=execute(initial_portfolio(),[strategy],[],[quote],{},now=now)
        renamed=execute(first,[{**strategy,'id':'different-label'}],[],[quote],{},now=now)
        self.assertEqual(first['seen_signals'],renamed['seen_signals'])
        self.assertEqual(len(renamed['pending_orders']),1)

    def test_event_signal_does_not_see_later_surprise(self):
        market=daily_market(10)
        spec={'symbol':'MRVL','kind':'event_surprise','parameters':{'lookback':3,'threshold':.1}}
        bars=copy.deepcopy(market['candles'])
        end=bars[-1]['end_ms']
        bars[-1]['event_features']=[{'symbol':'MRVL','id':'event','value':.5,'session_ms':end-1000,'available_at_ms':end+1,'evidence_ids':['actual']}]
        self.assertEqual(signal_target(spec,bars),0)
        bars[-1]['event_features'][0]['available_at_ms']=end
        self.assertEqual(signal_target(spec,bars),1)
        bars[-1]['event_features'][0]['event_identity']='same-earnings'
        bars[-1]['event_features'].append({'symbol':'MRVL','id':'revision','event_identity':'same-earnings',
            'value':-.5,'session_ms':end-1000,'available_at_ms':end,'evidence_ids':['correction']})
        bars[-1]['event_features'][0]['available_at_ms']=end-1
        self.assertEqual(signal_target(spec,bars),0)

    def test_terminal_settlement_is_not_an_invented_quote_fill(self):
        market=daily_market()
        now,quote=frame(market,10,'decision')
        strategy=rule(market,now-1)
        queued=execute(initial_portfolio(),[strategy],[],[quote],{},now=now)
        opening,quote=frame(market,11,'open')
        book=execute(queued,[strategy],[],[quote],{},now=opening)
        quote=copy.deepcopy(quote)
        quote['corporate_actions']=[{'id':'terminal','type':'delisting_settlement','cash_price':0.,
            'effective_at_ms':opening+1000,'available_at_ms':opening+1000}]
        quote['as_of_ms']=opening-10000000
        settled=execute(book,[strategy],[],[quote],{},now=opening+2000)
        self.assertFalse(settled['positions'])
        self.assertEqual(settled['trades'][-1]['side'],'settlement')
        self.assertLess(settled['realized_pnl'],0)
        self.assertAlmostEqual(settled['equity']-settled['initial_equity'],settled['realized_pnl']+settled['unrealized_pnl'])

    def test_revised_action_does_not_credit_entitlement_twice(self):
        market=daily_market()
        now,quote=frame(market,10,'decision')
        strategy=rule(market,now-1)
        queued=execute(initial_portfolio(),[strategy],[],[quote],{},now=now)
        opening,quote=frame(market,11,'open')
        book=execute(queued,[strategy],[],[quote],{},now=opening)
        quote=copy.deepcopy(quote)
        quote.update(as_of_ms=opening+60000)
        quote['corporate_actions']=[{'id':'div','type':'dividend','amount':1.,
            'effective_at_ms':opening+1000,'available_at_ms':opening+1000}]
        first=execute(book,[strategy],[],[quote],{},now=opening+60000)
        quote['corporate_actions'][0]['amount']=2.
        revised=execute(first,[strategy],[],[quote],{},now=opening+60000)
        self.assertTrue(revised['data_conflict'])
        self.assertFalse(revised['valuation_complete'])
        self.assertEqual(revised['receivables'],first['receivables'])
