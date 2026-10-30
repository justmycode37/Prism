#!/bin/bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
contract_dir="$(mktemp -d "${TMPDIR:-/tmp}/prism-runtime-client.XXXXXX")"
trap 'rm -rf "$contract_dir"' EXIT
export PRISM_RUNTIME_CLIENT_FIXTURE_DIR="$contract_dir"
export PYTHONPATH="$repo_root/backend"
python3 - <<'PY'
import json
import os
from pathlib import Path

from prism_runtime.orchestrator import Runtime
from prism_runtime.store import Store

root = Path(os.environ['PRISM_RUNTIME_CLIENT_FIXTURE_DIR'])
store = Store(root / 'offline.sqlite3')
runtime = Runtime(store, key_loader=lambda: None)
state = runtime.state()
for status, name in [('paused', 'paused.json'), ('running', 'running.json')]:
    fixture = dict(state)
    fixture['runtime'] = {**state['runtime'], 'status': status}
    (root / name).write_text(json.dumps(fixture, allow_nan=False))
runtime.shutdown()
store.close()
PY
swiftc -swift-version 6 -module-cache-path "$contract_dir/module-cache" \
    "$repo_root/Prism/Models/RuntimeState.swift" \
    "$repo_root/Prism/Models/PrismWorkspace.swift" \
    "$repo_root/Prism/Services/RuntimeClient.swift" \
    "$repo_root/Tests/RuntimeClientActionCheck.swift" \
    -o "$contract_dir/check"
python3 - "$contract_dir/check" "$contract_dir/paused.json" "$contract_dir/running.json" <<'PY'
import subprocess
import sys

subprocess.run(sys.argv[1:], check=True, timeout=20)
PY
