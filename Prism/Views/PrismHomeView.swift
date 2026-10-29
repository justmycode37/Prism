import SwiftUI

struct PrismHomeView: View {
    @ObservedObject var runtime: RuntimeClient
    @ObservedObject var preferences: AppearancePreferences
    @State private var pageBeforeSettings: PrismPage = .overview

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                GlassSurface(milkiness: preferences.milkiness)
                HStack(spacing: 28) {
                    navigationRail
                    VStack(alignment: .leading, spacing: 20) {
                        if runtime.selectedPage != .overview {
                            HStack(spacing: 12) {
                                if runtime.selectedPage == .settings {
                                    Button { runtime.selectedPage = pageBeforeSettings } label: {
                                        Image(systemName: "chevron.left").font(.system(size: 13)).frame(width: 28, height: 28)
                                    }
                                    .buttonStyle(.plain).glassEffect(.clear.interactive(), in: .circle)
                                    .help("Back").accessibilityLabel("Back")
                                }
                                Text(runtime.selectedPage.title)
                                    .font(.system(size: 24, weight: .medium, design: .rounded))
                            }
                            .padding(.top, 10).padding(.trailing, 36)
                        }
                        if !runtime.isConnected || runtime.errorMessage != nil { connectionNotice }
                        ScrollView {
                            pageContent(availableHeight: geometry.size.height - 100)
                                .frame(maxWidth: .infinity, alignment: .topLeading)
                                .padding(.bottom, 10)
                        }
                        .scrollIndicators(.hidden)
                        .scrollContentBackground(.hidden)
                        .background(.clear)
                    }
                }
                .padding(28)
            }
            .overlay(alignment: .topTrailing) {
                if runtime.selectedPage != .settings {
                    Button { runtime.selectedPage = .settings } label: {
                        Image(systemName: "gearshape").font(.system(size: 13)).frame(width: 32, height: 32)
                    }
                    .buttonStyle(.plain).glassEffect(.clear.interactive(), in: .circle)
                    .help("Settings (⌘,)").accessibilityLabel("Settings").padding(18)
                }
            }
        }
        .ignoresSafeArea()
        .task { await runtime.monitor() }
        .onChange(of: runtime.selectedPage) { previous, selected in
            if selected == .settings && previous != .settings { pageBeforeSettings = previous }
        }
    }

    private var navigationRail: some View {
        VStack(spacing: 8) {
            ForEach(PrismPage.mainSections) { page in navigationButton(page) }
        }
        .padding(10).frame(width: 64)
        .glassEffect(.clear, in: .rect(cornerRadius: 26))
        .frame(maxHeight: .infinity)
        .accessibilityElement(children: .contain).accessibilityLabel("Navigation")
    }

    private func navigationButton(_ page: PrismPage) -> some View {
        let selected = runtime.selectedPage == page
        return Button { runtime.selectedPage = page } label: {
            Image(systemName: page.symbol)
                .font(.system(size: 17, weight: selected ? .medium : .regular))
                .foregroundStyle(selected ? .primary : .secondary)
                .frame(width: 44, height: 44).contentShape(Circle())
        }
        .buttonStyle(.plain)
        .background {
            if selected { Circle().fill(.primary.opacity(0.065)) }
        }
        .help(page.title).accessibilityLabel(page.title).accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var connectionNotice: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "circle.dotted").foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 4) {
                Text(runtime.errorMessage ?? "Connecting…").font(.system(size: 11)).foregroundStyle(.secondary)
                if runtime.state != nil && !runtime.isConnected {
                    Text("Last received snapshot").font(.system(size: 10)).foregroundStyle(.tertiary)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.top, 8)
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
