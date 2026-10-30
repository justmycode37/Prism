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
PY
swiftc -module-cache-path "$contract_dir/module-cache" \
    "$repo_root/Prism/Models/RuntimeState.swift" \
    "$repo_root/Tests/RuntimeContractCheck.swift" \
    -o "$contract_dir/check"
"$contract_dir/check" "$contract_dir/state.json" "$contract_dir/leaderboard.json"
