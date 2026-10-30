#!/bin/bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
contract_dir="$(mktemp -d "${TMPDIR:-/tmp}/prism-cascade-contract.XXXXXX")"
trap 'rm -rf "$contract_dir"' EXIT
export PRISM_CONTRACT_DIR="$contract_dir"
export PYTHONPATH="$repo_root/backend"
python3 - <<'PY'
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from prism_runtime.bybit import BybitAdapter
from prism_runtime.orchestrator import Runtime
from prism_runtime.store import Store
from prism_runtime.operations_console import projection

root = Path(os.environ['PRISM_CONTRACT_DIR'])
store = Store(root / 'cascade.sqlite3')
# State assembly is tested without reading a credential or starting a scheduler.
runtime = Runtime(store, key_loader=lambda: None)
runtime.control('start_all')
assert not runtime.paused, 'The unchanged Overview Start action must activate the cascade.'
runtime.control('pause')
store.set_document('sector_universe', {
    'id': 'ai_infrastructure', 'name': 'AI infrastructure', 'symbols': ['MRVL'],
    'discovery_status': 'partial', 'companies': [{
        'symbol': 'MRVL', 'name': 'Marvell Technology', 'themes': ['semiconductors', 'networking'],
        'source_urls': ['https://example.org/fixture'], 'evidence_ids': ['contract-evidence'],
        'discovery_reason': 'Synthetic contract fixture.'
    }]
})
store.set_document('markets', [{
    'symbol': 'MRVL', 'price': 100.0, 'change_24h': .025,
    'funding_rate': None, 'as_of_ms': 1793286000123,
    'updated_at': '2026-10-29T15:00:00.123Z',
}])
store.set_document('portfolio', {
    'equity': 10001.0, 'cash': 9900.0, 'realized_pnl': 0.0, 'unrealized_pnl': 1.0,
    'initial_equity': 10000.0, 'as_of': '2026-10-29T15:00:00.123Z',
    'pnl_history': [
        {'timestamp': '2026-10-29T15:00:00Z', 'pnl': 0.0},
        {'timestamp': '2026-10-29T15:00:00.123Z', 'pnl': 1.0},
    ],
    'positions': [{'symbol': 'MRVL', 'side': 'long', 'quantity': 1.0,
                   'entry_price': 100.0, 'mark_price': 101.0, 'unrealized_pnl': 1.0}],
    'trades': [{'id': 'contract-fill', 'symbol': 'MRVL', 'side': 'buy',
                'quantity': 1.0, 'price': 100.0, 'timestamp': '2026-10-29T15:00:00Z', 'realized_pnl': 0.0}],
})
for scored in (False, True):
    store.save_strategy({
        'id': 'scored' if scored else 'unscored', 'name': 'Cascade contract fixture',
        'symbol': 'MRVL', 'family': 'momentum', 'status': 'rejected',
        'net_return': -.01 if scored else None, 'sharpe': -.2 if scored else None,
        'max_drawdown': .03 if scored else None, 'trades': 5 if scored else None,
        'reason': 'Synthetic contract fixture; no market-performance claim.',
        'created_at': '2026-10-29T15:00:00Z',
        'spec': {'symbol': 'MRVL', 'kind': 'momentum', 'parameters': {'lookback': 5, 'threshold': .001}},
    })
store.event('cascade', 'Contract fixture assembled without network access.', 'info')
state = runtime.state()
state['operations'] = projection(mode='paper', enabled=False,
    schedule={'next_wake_at': '2026-10-30T14:25:00Z', 'rationale': 'Prepare for a sourced market open.',
              'tasks': [{'id': 'prepare', 'kind': 'prepare', 'priority': 90, 'reason': 'Market opens in five minutes.', 'status': 'pending'}]},
    calendar=[], subscriptions=[], memory={'recent_reviews': [], 'recent_revisions': []},
    execution={'risk': {'reconciliation_required': True, 'reasons': ['Offline fixture account']}},
    resources=runtime.component_budget.state(), blocked=[])
assert len(state['agents']) == 33, 'The default reverse cascade must contain 33 agents.'
assert len(state['cascade']['layers']) == 4, 'All four cascade layers must be exposed.'
# Synthetic, local-only development leaderboard rows exercise native decoding and
# sorting without changing the actual empty runtime-state fixture.
leaderboard = {
    'rows': [
        {'id': 'leader', 'experiment_id': 'leader', 'name': 'Top fixture', 'symbol': 'MRVL',
         'family': 'momentum', 'phase': 'development', 'net_return': .12,
         'max_drawdown': .025, 'trades': 9, 'screen_status': 'passed',
         'prospective_status': 'queued',
         'qualification': {'qualified': False, 'status': 'not_qualified',
                           'basis': 'development_only', 'ranking_effect': 'none',
                           'reasons': ['Development evidence is not prospective qualification.']}},
        {'id': 'tie-first', 'experiment_id': 'tie-first', 'name': 'Tie fixture A', 'symbol': 'ANET',
         'phase': 'development', 'net_return': .04, 'max_drawdown': .03, 'trades': 5,
         'qualification': {'qualified': False, 'status': 'not_qualified'}},
        {'id': 'tie-second', 'experiment_id': 'tie-second', 'name': 'Tie fixture B', 'symbol': 'AVGO',
         'phase': 'development', 'net_return': .04, 'max_drawdown': .02, 'trades': 8,
         'qualification': {'qualified': False, 'status': 'not_qualified'}},
        {'id': 'negative', 'experiment_id': 'negative', 'name': 'Negative fixture', 'symbol': 'DELL',
         'phase': 'development', 'net_return': -.02, 'max_drawdown': .08, 'trades': 3},
        {'id': 'unmeasured', 'experiment_id': 'unmeasured', 'name': 'Unmeasured fixture', 'symbol': 'SMCI',
         'phase': 'development', 'net_return': None, 'max_drawdown': None, 'trades': None},
    ],
    'total': 5, 'ranked': 4, 'ranking_basis': 'development_net_return', 'limit': 100,
}
leaderboard['rows'][0].update(test_start='2025-01-01', test_end='2025-12-31')
(root / 'state.json').write_text(json.dumps(state, allow_nan=False))
(root / 'leaderboard.json').write_text(json.dumps(leaderboard, allow_nan=False))
runtime.shutdown()
store.close()
sol_store = Store(root / 'sol.sqlite3')
sol_runtime = Runtime(sol_store, key_loader=lambda: None)
sol_runtime.update_configuration({'trading_mode': {'selected': 'sol_perpetual'}})
sol_runtime.update_configuration({'bybit': {'account_id': 'contract-account'}})
sol_strategy_id = sol_store.get_document('sol_mode_strategy_id')
sol_adapter = BybitAdapter(sol_store, sol_store.config(), transport=object(),
    credential_loader=lambda _: (_ for _ in ()).throw(AssertionError('Contract projection must not read credentials')),
    clock=sol_store._clock)
sol_adapter._insert_intent('fixture-sol-fill', 'linear', 'SOLUSDT', {
    'category': 'linear', 'symbol': 'SOLUSDT', 'side': 'Buy', 'orderType': 'Limit',
    'qty': '1', 'price': '180.5', 'strategy_id': sol_strategy_id})
sol_adapter._apply_execution({'execId': 'fixture-sol-exec', 'execType': 'Trade',
    'category': 'linear', 'symbol': 'SOLUSDT', 'orderLinkId': 'fixture-sol-fill',
    'orderId': 'fixture-sol-order', 'side': 'Buy', 'execQty': '0.5', 'execPrice': '180.5',
    'execFee': '0.05415', 'feeCurrency': 'USDT', 'execPnl': '0',
    'execTime': str(sol_store._now_ms())})
sol_store.set_document('live_execution_state', {'account': {
    'equity': 102., 'daily_pnl': 2., 'daily_pnl_verified': True,
    'daily_baseline_equity': 100., 'portfolio_notional': 20., 'leverage': 10.,
    'leverage_by_instrument': {'linear:SOLUSDT': 5., 'linear:BTCUSDT': 10.},
    'mode': 'paper', 'reconciliation_required': False, 'account_binding_verified': True,
    'updated_at_ms': sol_store._now_ms(),
    'day': datetime.fromtimestamp(sol_store._now_ms() / 1000, timezone.utc).strftime('%Y-%m-%d'),
}, 'environment': 'paper', 'updated_at_ms': sol_store._now_ms(),
   'account_scope': sol_runtime._expected_execution_account_scope(sol_store.config()),
   'reconciliation_required': False, 'positions': [{
       'strategy_id': sol_strategy_id, 'category': 'linear', 'symbol': 'SOLUSDT',
       'quantity': 0.5, 'entry_price': 180.5, 'mark_price': 181.25,
       'leverage': 5., 'stop_loss': 176.}],
})
sol_state = sol_runtime.state()
sol_state['markets'] = [{
    'symbol': 'SOLUSDT', 'price': 181.25, 'change_24h': 0.012,
    'funding_rate': 0.0001, 'updated_at': '2026-10-29T15:00:00.123Z',
}]
sol_state['sol_chart'] = {
    'symbol': 'SOLUSDT', 'category': 'linear', 'timeframe': '1', 'environment': 'mainnet',
    'candles': [{'start_ms': 1793286000000, 'open': 180.0, 'high': 183.0,
                 'low': 178.0, 'close': 181.25, 'closed': False}],
}
sol_state['operations'] = projection(mode='paper', enabled=False,
    schedule={'tasks': []}, calendar=[], subscriptions=[], memory={'recent_reviews': [], 'recent_revisions': []},
    execution={'account': {'portfolio_notional': 20.0, 'reconciliation_required': False},
        'order_count': 5000, 'active_order_count': 4, 'order_history_truncated': True,
        'orders': [{'id': 'fixture-order', 'side': 'Buy', 'symbol': 'SOLUSDT',
                    'status': 'partially_filled', 'filled_quantity': 0.5,
                    'quantity': 1.0, 'price': 180.0}]},
    resources={}, blocked=[])
(root / 'sol-state.json').write_text(json.dumps(sol_state, allow_nan=False))
sol_runtime.shutdown()
sol_store.close()
PY
swiftc -module-cache-path "$contract_dir/module-cache" \
    "$repo_root/Prism/Models/RuntimeState.swift" \
    "$repo_root/Prism/Models/PrismWorkspace.swift" \
    "$repo_root/Tests/RuntimeContractCheck.swift" \
    -o "$contract_dir/check"
"$contract_dir/check" "$contract_dir/state.json" "$contract_dir/leaderboard.json" "$contract_dir/sol-state.json"
cat > "$contract_dir/SingleTraderContractCheck.swift" <<'SWIFT'
import Foundation

@main
enum SingleTraderContractCheck {
    static func main() throws {
        let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let state = try decoder.decode(RuntimeState.self, from: data)
        guard let quote = state.markets.first, let chart = state.solChart,
              let operations = state.operations else { fatalError("Single trader projections must decode") }
        precondition(quote.price == 181.25 && quote.change24h == 0.012 && quote.fundingRate == 0.0001,
                     "SOL quote fields and snake-case decoding must remain intact")
        let candle = chart.displayCandles(for: state.runtime.mode).first
        precondition(candle?.open == 180 && candle?.high == 183 && candle?.close == 181.25 && candle?.closed == false,
                     "The native chart must preserve actual OHLC and forming state")
        precondition(operations.grossExposure == 20 && operations.riskStatus == "active" &&
                     operations.section(id: "orders")?.items.first?.status == "partially_filled",
                     "Venue exposure, risk status and partial order outcomes must decode")
        precondition(operations.orderCount == 5000 && operations.activeOrderCount == 4 &&
                     operations.ordersTruncated == true,
                     "Bounded order views must preserve complete host counts and truncation")
        precondition(state.tradingMode?.leverage == 5,
                     "The SOL display must use exact instrument leverage instead of the account maximum")
        precondition(state.tradingMode?.accountStatus == "current" &&
                     state.tradingMode?.accountAsOf != nil && state.tradingMode?.accountDay != nil &&
                     state.tradingMode?.tradingPnl == 2,
                     "Account age, UTC day and current-day P&L metadata must decode")
        guard let actual = state.tradingMode?.actualSolExecution else {
            fatalError("Actual SOL execution projection must decode")
        }
        precondition(actual.environment == "paper" && actual.positionsStatus == "confirmed" &&
                     actual.confirmedFlat == false && actual.positions?.first?.quantity == 0.5,
                     "Actual SOL exposure must come from the matching Bybit paper controller state")
        precondition(actual.fillsAvailable == true && actual.fills?.first?.id == "fixture-sol-exec" &&
                     actual.fills?.first?.feeCurrency == "USDT",
                     "SOL trade history must decode actual fills with fee currency")
        precondition(!state.portfolio.positions.contains(where: { $0.symbol == "SOLUSDT" }) &&
                     !state.portfolio.trades.contains(where: { $0.symbol == "SOLUSDT" }),
                     "Legacy research portfolio activity must not be used as actual SOL execution")
        precondition(quote.freshness(at: Date(timeIntervalSince1970: 1_793_286_000.123)) == .current(age: 0),
                     "Quote freshness must recognize a timestamp at the sampled instant")
        precondition(quote.freshness(at: Date(timeIntervalSince1970: 1_793_286_030.123)) == .stale(age: 30),
                     "A quote over the freshness window must be marked stale")
        precondition(quote.freshness(at: Date(timeIntervalSince1970: 1_793_285_990.123)) == .clockSkew,
                     "A quote materially ahead of the device clock must not be called fresh")
        print("PASS: SOL quote freshness, real OHLC, venue risk and partial-order projection decode offline.")
    }
}
SWIFT
swiftc -module-cache-path "$contract_dir/module-cache" \
    "$repo_root/Prism/Models/RuntimeState.swift" \
    "$repo_root/Prism/Models/PrismWorkspace.swift" \
    "$contract_dir/SingleTraderContractCheck.swift" \
    -o "$contract_dir/single-trader-check"
"$contract_dir/single-trader-check" "$contract_dir/sol-state.json"
