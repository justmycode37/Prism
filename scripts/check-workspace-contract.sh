#!/bin/sh
set -eu
prism_repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
prism_workspace_check_dir=$(mktemp -d "${TMPDIR:-/tmp}/prism-workspace-contract.XXXXXX")
trap 'rm -rf "$prism_workspace_check_dir"' EXIT
swiftc -swift-version 6 \
  "$prism_repo_root/Prism/Models/RuntimeState.swift" \
  "$prism_repo_root/Prism/Models/PrismWorkspace.swift" \
  "$prism_repo_root/Tests/WorkspaceContractCheck.swift" \
  -o "$prism_workspace_check_dir/check"
"$prism_workspace_check_dir/check"
