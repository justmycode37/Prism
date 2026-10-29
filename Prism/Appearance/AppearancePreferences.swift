import AppKit
import SwiftUI

enum PrismAppearance: String, CaseIterable, Identifiable {
    case system, light, dark

    var id: String { rawValue }
    var title: String { rawValue.capitalized }
    var symbol: String {
        switch self {
        case .system: "circle.lefthalf.filled"
        case .light: "sun.max"
        case .dark: "moon"
        }
    }

    var appKitAppearance: NSAppearance? {
        switch self {
        case .system: nil
        case .light: NSAppearance(named: .aqua)
        case .dark: NSAppearance(named: .darkAqua)
        }
    }
}

@MainActor
final class AppearancePreferences: ObservableObject {
    static let blurRange = 0.0...100.0

    @Published var selection: PrismAppearance {
        didSet {
            UserDefaults.standard.set(selection.rawValue, forKey: "appearance")
            apply()
        }
    }

    @Published var backdropBlurRadius: Double {
        didSet { UserDefaults.standard.set(backdropBlurRadius, forKey: "backdropBlurRadius") }
    }

    @Published var milkiness: Double {
        didSet { UserDefaults.standard.set(milkiness, forKey: "glassMilkiness") }
    }

    init() {
        selection = PrismAppearance(rawValue: UserDefaults.standard.string(forKey: "appearance") ?? "system") ?? .system
        let blur = UserDefaults.standard.object(forKey: "backdropBlurRadius") as? Double ?? 40
        backdropBlurRadius = blur.isFinite ? min(max(blur, Self.blurRange.lowerBound), Self.blurRange.upperBound) : 40
        let milk = UserDefaults.standard.object(forKey: "glassMilkiness") as? Double ?? 0
        milkiness = milk.isFinite ? min(max(milk, 0), 1) : 0
    }

    func apply() { NSApp.appearance = selection.appKitAppearance }
}
