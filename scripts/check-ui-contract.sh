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
assert len(state['agents']) == 33, 'The default reverse cascade must contain 33 agents.'
assert len(state['cascade']['layers']) == 4, 'All four cascade layers must be exposed.'
(root / 'state.json').write_text(json.dumps(state, allow_nan=False))
runtime.shutdown()
store.close()
PY
swiftc -module-cache-path "$contract_dir/module-cache" \
    "$repo_root/Prism/Models/RuntimeState.swift" \
    "$repo_root/Tests/RuntimeContractCheck.swift" \
    -o "$contract_dir/check"
"$contract_dir/check" "$contract_dir/state.json"
