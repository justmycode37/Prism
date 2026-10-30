#!/bin/sh
set -eu
prism_repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
prism_render_dir=/tmp/prism-ui-redesign
prism_render_bin="$prism_render_dir/UIRenderingCheck"
mkdir -p "$prism_render_dir"
rm -f "$prism_render_dir"/*.png
cd "$prism_repo_root"
swiftc -swift-version 6 -D PRISM_EXPERIMENTAL_GLASS -O -o "$prism_render_bin" \
  Prism/Models/RuntimeState.swift \
  Prism/Design/PrismTheme.swift Prism/Design/GlassSurface.swift \
  Prism/Views/ConsoleComponents.swift \
  Prism/Views/PrismHomeView.swift Prism/Views/PrismOverviewView.swift \
  Prism/Views/PrismResearchView.swift Prism/Views/PrismTestView.swift \
  Prism/Views/PrismExecutionView.swift Prism/Views/PrismPortfolioView.swift \
  Prism/Views/PrismActivityView.swift Prism/Views/PrismAgentsView.swift \
  Prism/Views/PrismSettingsView.swift Tests/UIRenderingCheck.swift
"$prism_render_bin"
