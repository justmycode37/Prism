import SwiftUI

struct PrismSettingsView: View {
    @ObservedObject var preferences: AppearancePreferences
    @ObservedObject var runtime: RuntimeClient
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.colorSchemeContrast) private var contrast
    @State private var ceilingEnabled = false
    @State private var ceilingAmount = ""
    @State private var spendingDraftLoaded = false
    @State private var spendingDraftEdited = false
    @State private var spendingSaveMessage: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            ConsoleMetricGrid {
                ConsoleMetric(title: "Model cost today",
                              value: runtime.state.map { PrismFormat.money($0.budget.spentTodayUsd) } ?? "—",
                              detail: "Estimated · USD")
                ConsoleMetric(title: "Requests today",
                              value: runtime.state.map { String($0.budget.callsToday) } ?? "—",
                              detail: "Across all agents")
                ConsoleMetric(title: "Daily ceiling",
                              value: runtime.state.map { state in state.budget.dailyLimitUsd.map { PrismFormat.money($0) } ?? "Off" } ?? "—",
                              detail: runtime.state.map { "\(PrismFormat.money($0.budget.reservedUsd)) reserved" } ?? "Awaiting runtime")
            }
            ConsoleCard("Appearance") {
                VStack(alignment: .leading, spacing: 22) {
                    HStack(spacing: 12) {
                        ForEach(PrismAppearance.allCases) { appearance in appearanceButton(appearance) }
                    }
                    #if PRISM_EXPERIMENTAL_GLASS
                    VStack(alignment: .leading, spacing: 8) {
                        row("Background blur") {
                            Text(preferences.backdropBlurRadius, format: .number.precision(.fractionLength(0)))
                                .monospacedDigit().foregroundStyle(.secondary)
                        }
                        Slider(value: $preferences.backdropBlurRadius, in: AppearancePreferences.blurRange, step: 1)
                            .tint(.primary)
                            .accessibilityLabel("Background blur")
                            .accessibilityValue("\(Int(preferences.backdropBlurRadius))")
                            .disabled(reduceTransparency || contrast == .increased)
                    }
                    #endif
                    VStack(alignment: .leading, spacing: 8) {
                        row("Milkiness") {
                            Text(preferences.milkiness, format: .percent.precision(.fractionLength(0)))
                                .monospacedDigit().foregroundStyle(.secondary)
                        }
                        Slider(value: $preferences.milkiness, in: 0...1, step: 0.01)
                            .tint(.primary)
                            .accessibilityLabel("Milkiness")
                            .accessibilityValue(preferences.milkiness.formatted(.percent.precision(.fractionLength(0))))
                            .disabled(reduceTransparency)
                    }
                }
            }
            ConsoleCard("Spending controls", caption: "All agents") {
                spendingControls
            }
        }
        .font(.system(size: 13))
        .task { loadSpendingDraft() }
        .onChange(of: runtime.lastUpdated) { _, _ in loadSpendingDraft() }
    }

    private var spendingControls: some View {
        VStack(alignment: .leading, spacing: 16) {
            Toggle(isOn: Binding(
                get: { ceilingEnabled },
                set: { ceilingEnabled = $0; editSpendingDraft() }
            )) {
                HStack {
                    Text("Daily spending ceiling")
                    Spacer()
                    Text(ceilingEnabled ? "On" : "Off").foregroundStyle(.secondary)
                }
            }
            .toggleStyle(.switch)
            .disabled(!spendingDraftLoaded || !runtime.isConnected || runtime.isBusy)
            if ceilingEnabled {
                row("Amount per day (USD)") {
                    TextField("USD amount", text: Binding(
                        get: { ceilingAmount },
                        set: { ceilingAmount = $0; editSpendingDraft() }
                    ))
                    .textFieldStyle(.roundedBorder)
                    .multilineTextAlignment(.trailing)
                    .frame(width: 140)
                    .accessibilityLabel("Daily spending ceiling in USD")
                    .disabled(!runtime.isConnected || runtime.isBusy)
                }
                if spendingDraftEdited && validatedCeiling == nil {
                    Text("Enter a USD amount greater than zero.")
                        .font(.system(size: 11)).foregroundStyle(.red)
                }
            }
            HStack(spacing: 10) {
                Text("Shared by all agents. Changes apply when you save.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                Spacer(minLength: 8)
                if let spendingSaveMessage {
                    Text(spendingSaveMessage).font(.system(size: 11)).foregroundStyle(.secondary)
                }
                Button { Task { await saveSpendingDraft() } } label: {
                    Text("Save").font(.system(size: 12, weight: .medium)).frame(width: 64, height: 34)
                }
                    .buttonStyle(.plain).glassEffect(.clear.interactive(), in: .capsule)
                    .disabled(!spendingDraftEdited || !runtime.isConnected || runtime.isBusy ||
                              (ceilingEnabled && validatedCeiling == nil))
            }
        }
    }

    private var validatedCeiling: Double? {
        let trimmed = ceilingAmount.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let value = Double(trimmed), value.isFinite, value > 0 else { return nil }
        return value
    }

    private func loadSpendingDraft() {
        guard !spendingDraftEdited, let state = runtime.state else { return }
        ceilingEnabled = state.config.dailyLimitUsd != nil
        ceilingAmount = state.config.dailyLimitUsd.map { String($0) } ?? ""
        spendingDraftLoaded = true
    }

    private func editSpendingDraft() {
        spendingDraftEdited = true
        spendingSaveMessage = nil
    }

    private func saveSpendingDraft() async {
        guard !ceilingEnabled || validatedCeiling != nil else { return }
        let amount = ceilingEnabled ? validatedCeiling : nil
        if await runtime.updateDailySpendCeiling(amount) {
            ceilingEnabled = amount != nil
            ceilingAmount = amount.map { String($0) } ?? ""
            spendingDraftEdited = false
            spendingSaveMessage = "Saved"
        } else {
            spendingSaveMessage = "Could not save. Try again."
        }
    }

    private func appearanceButton(_ appearance: PrismAppearance) -> some View {
        let selected = preferences.selection == appearance
        return Button { preferences.selection = appearance } label: {
            VStack(spacing: 10) {
                Image(systemName: appearance.symbol).font(.system(size: 18, weight: .regular))
                Text(appearance.title).font(.system(size: 11))
            }
            .foregroundStyle(selected ? .primary : .secondary)
            .frame(maxWidth: .infinity).frame(height: 72).contentShape(RoundedRectangle(cornerRadius: 20))
        }
        .buttonStyle(.plain)
        .background { if selected { RoundedRectangle(cornerRadius: 20).fill(.primary.opacity(0.07)) } }
        .glassEffect(.clear.interactive(), in: .rect(cornerRadius: 20))
        .accessibilityLabel(appearance.title).accessibilityValue(selected ? "Selected" : "Not selected")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private func row<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        HStack { Text(title).font(.system(size: 12)); Spacer(); content().font(.system(size: 12)) }
    }
}
