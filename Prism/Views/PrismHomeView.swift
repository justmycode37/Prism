import SwiftUI

struct PrismHomeView: View {
    @ObservedObject var runtime: RuntimeClient
    @ObservedObject var preferences: AppearancePreferences
    @Environment(\.colorScheme) private var colorScheme
    @State private var workspace: PrismWorkspace?
    @State private var pageBeforeSettings: PrismPage = .overview

    private var controlColor: Color { colorScheme == .dark ? .white.opacity(0.92) : .black.opacity(0.84) }

    init(runtime: RuntimeClient, preferences: AppearancePreferences, initialWorkspace: PrismWorkspace? = nil) {
        self.runtime = runtime
        self.preferences = preferences
        _workspace = State(initialValue: initialWorkspace)
    }

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                GlassSurface(milkiness: preferences.milkiness)
                Group {
                    if let workspace {
                        workspaceContent(workspace, availableHeight: geometry.size.height - 126)
                    } else if runtime.selectedPage == .settings {
                        VStack(spacing: 18) {
                            pageHeader
                            ScrollView { PrismSettingsView(preferences: preferences, runtime: runtime).padding(2) }
                                .scrollIndicators(.hidden)
                        }
                    } else {
                        PrismWorkspacePicker { selected in
                            runtime.selectedPage = .overview
                            workspace = selected
                        }
                        .overlay(alignment: .topTrailing) { settingsButton }
                    }
                }
                .padding(24)
            }
        }
        .ignoresSafeArea()
        .task { await runtime.monitor() }
        .onChange(of: runtime.selectedPage) { previous, selected in
            if selected == .settings && previous != .settings { pageBeforeSettings = previous }
        }
    }

    private func workspaceContent(_ workspace: PrismWorkspace, availableHeight: CGFloat) -> some View {
        HStack(spacing: 24) {
            if workspace == .agentSwarm { navigationRail }
            VStack(alignment: .leading, spacing: 18) {
                pageHeader
                if !runtime.isConnected || runtime.errorMessage != nil { connectionNotice }
                if runtime.selectedPage != .settings {
                    modeNotice(workspace)
                }
                if workspace == .singleTrader && runtime.selectedPage != .settings {
                    PrismSingleTraderView(runtime: runtime, availableHeight: availableHeight)
                        .frame(maxWidth: 920)
                        .frame(maxWidth: .infinity, alignment: .top)
                } else {
                    ScrollView {
                        pageContent(availableHeight: availableHeight)
                            .frame(maxWidth: 920, alignment: .topLeading)
                            .frame(maxWidth: .infinity, alignment: .top)
                            .padding(2).padding(.bottom, 12)
                    }
                    .scrollIndicators(.hidden)
                    .scrollContentBackground(.hidden)
                    .background(.clear)
                }
            }
        }
    }

    private var pageHeader: some View {
        HStack(spacing: 12) {
            if runtime.selectedPage == .settings {
                Button { runtime.selectedPage = pageBeforeSettings } label: {
                    Image(systemName: "chevron.left").font(.system(size: 14, weight: .regular))
                        .foregroundStyle(controlColor)
                        .frame(width: 32, height: 32)
                }
                .buttonStyle(.plain).glassEffect(.clear.interactive(), in: .circle)
                .help("Back").accessibilityLabel("Back")
            } else {
                Button { workspace = nil } label: {
                    Image(systemName: "square.grid.2x2")
                        .font(.system(size: 15, weight: .regular)).foregroundStyle(controlColor).frame(width: 32, height: 32)
                }
                .buttonStyle(.plain).glassEffect(.clear.interactive(), in: .circle)
                .help("Change workspace").accessibilityLabel("Change workspace")
                .accessibilityIdentifier("change-workspace")
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(runtime.selectedPage == .settings ? "Settings" : (workspace == .singleTrader ? "Single SOL Trader" : runtime.selectedPage.title))
                    .font(.system(size: 23, weight: .medium, design: .rounded))
                    .accessibilityAddTraits(.isHeader)
                if workspace == .agentSwarm && runtime.selectedPage != .settings {
                    Text("Agent Swarm").font(.system(size: 10)).foregroundStyle(.tertiary)
                }
            }
            Spacer(minLength: 8)
            if workspace == .singleTrader && runtime.selectedPage != .settings {
                PrismTraderControl(runtime: runtime)
            }
            if runtime.selectedPage != .settings { settingsButton }
        }
        .frame(minHeight: 36)
        .padding(.horizontal, 4)
    }

    private var settingsButton: some View {
        Button { runtime.selectedPage = .settings } label: {
            Image(systemName: PrismPage.settings.symbol)
                .font(.system(size: 16, weight: .regular)).foregroundStyle(controlColor).frame(width: 36, height: 36)
        }
        .buttonStyle(.plain).glassEffect(.clear.interactive(), in: .circle)
        .help("Settings (⌘,)").accessibilityLabel("Settings")
    }

    @ViewBuilder private func modeNotice(_ workspace: PrismWorkspace) -> some View {
        if let state = runtime.state, !workspace.matches(selectedMode: state.tradingMode?.selected) {
            HStack(spacing: 12) {
                Image(systemName: "arrow.triangle.swap").foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 4) {
                    Text("\(workspace.title) is ready to select")
                        .font(.system(size: 11, weight: .medium))
                    Text(state.runtime.status == "paused"
                         ? "The runtime checks that positions and orders are closed."
                         : "Pause the current runtime before changing modes.")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                if state.runtime.status != "paused" {
                    Button("Pause runtime") { Task { await runtime.control("pause") } }
                        .disabled(!runtime.isConnected || runtime.isBusy)
                        .help("Pause the current runtime; open positions remain")
                } else {
                    Button("Use this mode") { Task { await runtime.selectTradingMode(workspace.runtimeMode) } }
                        .disabled(!runtime.isConnected || runtime.isBusy)
                }
            }
            .buttonStyle(.borderless).font(.system(size: 11, weight: .medium))
            .padding(.horizontal, 4)
        } else if workspace == .singleTrader, runtime.state?.tradingMode?.pending == true {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "clock").foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 4) {
                    Text("SOL execution eligibility pending").font(.system(size: 11, weight: .medium))
                    Text("You can start this mode while execution checks are pending. "
                         + (runtime.state?.tradingMode?.unsupportedReason ?? "Orders require a qualified strategy and account approvals."))
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 4)
        }
    }

    private var navigationRail: some View {
        VStack(spacing: 8) {
            ForEach(PrismPage.mainSections) { page in navigationButton(page) }
        }
        .padding(8).frame(width: 60)
        .glassEffect(.clear, in: .rect(cornerRadius: 26))
        .frame(maxHeight: .infinity)
        .accessibilityElement(children: .contain).accessibilityLabel("Agent Swarm navigation")
    }

    private func navigationButton(_ page: PrismPage) -> some View {
        let selected = runtime.selectedPage == page
        return Button { runtime.selectedPage = page } label: {
            Image(systemName: page.symbol)
                .font(.system(size: 18, weight: .regular))
                .symbolRenderingMode(.monochrome)
                .foregroundStyle(selected ? .primary : .secondary)
                .frame(width: 44, height: 44).contentShape(Circle())
        }
        .buttonStyle(.plain)
        .background {
            if selected { Circle().fill(.primary.opacity(0.085)) }
        }
        .help(page.title).accessibilityLabel(page.title).accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var connectionNotice: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "wifi.slash").font(.system(size: 12)).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 4) {
                Text(runtime.isConnected ? "Runtime notice" : (runtime.state == nil ? "Runtime offline" : "Offline · showing last snapshot"))
                    .font(.system(size: 11, weight: .medium))
                if let message = runtime.errorMessage {
                    if runtime.isConnected {
                        Text(message).font(.system(size: 11)).foregroundStyle(.orange)
                            .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    } else {
                    DisclosureGroup("Connection details") {
                        Text(message).font(.system(size: 11)).textSelection(.enabled).padding(.top, 5)
                    }.font(.system(size: 10)).foregroundStyle(.secondary)
                    }
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 4)
    }

    @ViewBuilder private func pageContent(availableHeight: CGFloat) -> some View {
        switch runtime.selectedPage {
        case .overview: PrismOverviewView(runtime: runtime, availableHeight: availableHeight)
        case .research: PrismResearchView(runtime: runtime)
        case .test: PrismTestView(runtime: runtime)
        case .execution: PrismExecutionView(runtime: runtime)
        case .agents: PrismAgentsView(runtime: runtime)
        case .settings: PrismSettingsView(preferences: preferences, runtime: runtime)
        }
    }
}
