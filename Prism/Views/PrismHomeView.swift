import SwiftUI

struct PrismHomeView: View {
    @ObservedObject var runtime: RuntimeClient
    @ObservedObject var preferences: AppearancePreferences
    @State private var pageBeforeSettings: PrismPage = .overview

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                GlassSurface(milkiness: preferences.milkiness)
                HStack(spacing: 24) {
                    navigationRail
                    VStack(alignment: .leading, spacing: 18) {
                        pageHeader
                        if !runtime.isConnected || runtime.errorMessage != nil { connectionNotice }
                        ScrollView {
                            pageContent(availableHeight: geometry.size.height - 126)
                                .frame(maxWidth: 920, alignment: .topLeading)
                                .frame(maxWidth: .infinity, alignment: .top)
                                .padding(2).padding(.bottom, 12)
                        }
                        .scrollIndicators(.hidden)
                        .scrollContentBackground(.hidden)
                        .background(.clear)
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

    private var pageHeader: some View {
        HStack(spacing: 12) {
            if runtime.selectedPage == .settings {
                Button { runtime.selectedPage = pageBeforeSettings } label: {
                    Image(systemName: "chevron.left").font(.system(size: 14, weight: .regular))
                        .frame(width: 32, height: 32)
                }
                .buttonStyle(.plain).glassEffect(.clear.interactive(), in: .circle)
                .help("Back").accessibilityLabel("Back")
            }
            Text(runtime.selectedPage.title)
                .font(.system(size: 23, weight: .medium, design: .rounded))
                .accessibilityAddTraits(.isHeader)
            Spacer(minLength: 8)
            if runtime.selectedPage != .settings {
                Button { runtime.selectedPage = .settings } label: {
                    Image(systemName: PrismPage.settings.symbol)
                        .font(.system(size: 16, weight: .regular)).frame(width: 36, height: 36)
                }
                .buttonStyle(.plain).glassEffect(.clear.interactive(), in: .circle)
                .help("Settings (⌘,)").accessibilityLabel("Settings")
            }
        }
        .frame(minHeight: 36)
        .padding(.horizontal, 4)
    }

    private var navigationRail: some View {
        VStack(spacing: 8) {
            ForEach(PrismPage.mainSections) { page in navigationButton(page) }
        }
        .padding(8).frame(width: 60)
        .glassEffect(.clear, in: .rect(cornerRadius: 26))
        .frame(maxHeight: .infinity)
        .accessibilityElement(children: .contain).accessibilityLabel("Navigation")
    }

    private func navigationButton(_ page: PrismPage) -> some View {
        let selected = runtime.selectedPage == page
        return Button { runtime.selectedPage = page } label: {
            Image(systemName: page.symbol)
                .font(.system(size: 18, weight: .regular))
                .symbolRenderingMode(.hierarchical)
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
                    DisclosureGroup("Connection details") {
                        Text(message).font(.system(size: 11)).textSelection(.enabled).padding(.top, 5)
                    }.font(.system(size: 10)).foregroundStyle(.secondary)
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
