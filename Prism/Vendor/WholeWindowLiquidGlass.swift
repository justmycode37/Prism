#if PRISM_EXPERIMENTAL_GLASS
import SwiftUI

// WholeWindowLiquidGlass — turns an entire macOS window into a single translucent
// Liquid Glass sheet that samples the desktop behind it (like the system volume /
// brightness HUD), with the progressive-blur ramp flattened so the backdrop reads
// crisp instead of milky.
//
// Verified on macOS 26 (Tahoe).
//
// ── ⚠️ App Store warning ─────────────────────────────────────────────────────────
// The tuner touches private CAFilter inputs (`inputBlurOpacity0..4`, `inputBlur-
// Radius`, `inputFaceColorMatrix*`) on the private `glassBackground` filter. Do NOT
// include this file in a Mac App Store target. Adding the file to a target is the
// opt-in: there is no runtime flag to disable. Gate it with your own compilation
// condition (e.g. `#if DIRECT`) at the call site or via target membership.
//
// ── Usage ────────────────────────────────────────────────────────────────────────
//   WindowGroup {
//       RootView()
//           .wholeWindowLiquidGlass()        // anywhere inside the window, once
//   }
//
// Your root view should draw a transparent/clear background — the glass becomes the
// window's backdrop, and your content floats on top of it. Translucent dark fills
// (e.g. ~60% dark panels) work well for legibility; avoid SwiftUI `.glassEffect(
// .regular)` plates on top — `.regular` reads as a milky slab over the clear sheet.
//
// ── Hard-won notes (do not "simplify" these away) ───────────────────────────────
// 1. SwiftUI `.glassEffect` can NOT do this: it never samples behind its own window.
//    Only AppKit `NSGlassEffectView` honestly samples the desktop backdrop.
// 2. `style = .clear` is the only neutral-transparent style. `.regular` is a milky
//    plate (crushed luma + oversaturation).
// 3. Do NOT apply the private `set_variant: 6` to a full window. That is the LIGHT
//    HUD-glass variant (right for a small OSD pill) — on a window it
//    overrides the dark appearance and washes everything out to white.
// 4. The "mush" is NOT the blur radius — it's the progressive-blur bands
//    (`inputBlurOpacity0..4`) of the `glassBackground` CAFilter. Zero the bands and
//    keep a small radius (~2.5) and the backdrop reads sharp, like the system HUD.
// 5. AppKit rebuilds the glass material asynchronously (theme change, resize,
//    key/non-key) and clobbers the overrides — re-apply every frame via
//    `NSView.displayLink` (it auto-pauses while the window is off screen).
// 6. Insert the NSGlassEffectView fully configured, inside a zero-duration
//    `NSAnimationContext` with implicit animation disabled: an NSGlassEffectView
//    animating layout under a SwiftUI hosting view has crashed with an
//    AttributeGraph precondition failure.
// 7. All private-SPI access is `responds(to:)`/key-presence guarded, so a future
//    SDK that drops the SPI silently falls back to the stock material.

// MARK: - Configuration

public struct WholeWindowGlassConfiguration: Sendable {
    /// Backdrop blur radius after the progressive ramp is flattened. The stock
    /// `.clear` uses ~7–8 which reads soft; ~2.5 matches the crisp system HUD.
    public var backdropBlurRadius: Double

    /// Flatten the progressive-blur bands (`inputBlurOpacity0..4`). Prism keeps
    /// uniform full-strength blur on the window backdrop and clears the bands
    /// on smaller controls. Set `false` to keep the stock progressive ramp.
    public var flattenProgressiveBlur: Bool

    /// Copy the `.clear` backing's neutral face colour matrix onto any SwiftUI
    /// `.glassEffect(.regular)` surfaces in the window, de-milking them (`.regular`
    /// crushes luma + oversaturates). Their own face opacity is preserved so the
    /// surfaces keep a readable body.
    public var neutralizeRegularGlassFaces: Bool

    /// Corner radius of the window-backing glass sheet.
    public var cornerRadius: Double

    /// Private `set_variant:` override for the backing. Leave `nil` for a full
    /// window. (6 = light HUD glass — only for small OSD-style pills.)
    public var variant: Int?

    /// Log a one-shot diagnostic summary (and a layer-tree dump when the
    /// `WHOLE_WINDOW_GLASS_DUMP=1` environment variable is set).
    public var logsDiagnostics: Bool

    public init(backdropBlurRadius: Double = 2.5,
                flattenProgressiveBlur: Bool = true,
                neutralizeRegularGlassFaces: Bool = true,
                cornerRadius: Double = 0,
                variant: Int? = nil,
                logsDiagnostics: Bool = false) {
        self.backdropBlurRadius = backdropBlurRadius
        self.flattenProgressiveBlur = flattenProgressiveBlur
        self.neutralizeRegularGlassFaces = neutralizeRegularGlassFaces
        self.cornerRadius = cornerRadius
        self.variant = variant
        self.logsDiagnostics = logsDiagnostics
    }
}

// MARK: - Public API

public extension View {
    /// Turns the hosting window into one translucent Liquid Glass sheet sampling
    /// the desktop behind it. Apply once anywhere inside the window. No-op on
    /// platforms other than macOS 26+.
    @ViewBuilder
    func wholeWindowLiquidGlass(_ configuration: WholeWindowGlassConfiguration = .init()) -> some View {
        #if os(macOS)
        if #available(macOS 26.0, *) {
            background(WholeWindowGlassInstaller(configuration: configuration))
        } else {
            self
        }
        #else
        self
        #endif
    }
}

#if os(macOS)
import AppKit

private let glassBackingID = NSUserInterfaceItemIdentifier("wholeWindowLiquidGlass.backing")

// MARK: - Installer

@available(macOS 26.0, *)
private struct WholeWindowGlassInstaller: NSViewRepresentable {
    let configuration: WholeWindowGlassConfiguration

    func makeNSView(context: Context) -> GlassInstallerView {
        GlassInstallerView(configuration: configuration)
    }
    func updateNSView(_ nsView: GlassInstallerView, context: Context) {
        nsView.configuration = configuration
        nsView.installIfPossible()
    }

    /// Transparent window + `NSGlassEffectView(.clear)` as the bottom-most subview
    /// of the content view. Idempotent per window.
    @MainActor
    fileprivate static func installBacking(in window: NSWindow, _ configuration: WholeWindowGlassConfiguration) {
        guard let content = window.contentView else { return }
        window.isOpaque = false
        window.backgroundColor = .clear
        guard content.subviews.first(where: { $0.identifier == glassBackingID }) == nil else { return }

        // Fully configured before insertion; implicit animation suppressed (see
        // note 6 in the header — AttributeGraph crash with SwiftUI hosting).
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0
            ctx.allowsImplicitAnimation = false
            let glass = NSGlassEffectView(frame: content.bounds)
            glass.identifier = glassBackingID
            glass.autoresizingMask = [.width, .height]
            glass.cornerRadius = configuration.cornerRadius
            glass.style = .clear
            if let variant = configuration.variant {
                applyVariant(variant, to: glass)
            }
            content.addSubview(glass, positioned: .below, relativeTo: nil)
        }
    }

    @MainActor
    private static func applyVariant(_ variant: Int, to glass: NSGlassEffectView) {
        let sel = NSSelectorFromString("set_variant:")
        guard glass.responds(to: sel) else { return }
        let fn = unsafeBitCast(glass.method(for: sel),
                               to: (@convention(c) (AnyObject, Selector, Int) -> Void).self)
        fn(glass, sel, variant)
    }
}

// MARK: - Installer probe view

/// Zero-size probe inserted by SwiftUI. It installs the glass backing once it
/// lands in a window. Using `viewDidMoveToWindow` keeps the whole install path on
/// the main actor with no async hop (the view's `window` is nil during
/// `makeNSView`, so the work has to be deferred until the view is in a window).
@available(macOS 26.0, *)
private final class GlassInstallerView: NSView {
    var configuration: WholeWindowGlassConfiguration

    init(configuration: WholeWindowGlassConfiguration) {
        self.configuration = configuration
        super.init(frame: .zero)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        installIfPossible()
    }

    func installIfPossible() {
        guard let window else { return }
        WholeWindowGlassInstaller.installBacking(in: window, configuration)
        if window.wwlg_tuner == nil {
            window.wwlg_tuner = GlassBackdropTuner(window: window, configuration: configuration)
        } else {
            // Prism: apply slider changes to the existing backdrop immediately.
            window.wwlg_tuner?.updateConfiguration(configuration)
        }
    }
}

// MARK: - Window association

private nonisolated(unsafe) var wwlg_tunerKey: UInt8 = 0
private extension NSWindow {
    /// Retained for the window's lifetime; released with the window.
    @MainActor
    var wwlg_tuner: GlassBackdropTuner? {
        get { objc_getAssociatedObject(self, &wwlg_tunerKey) as? GlassBackdropTuner }
        set { objc_setAssociatedObject(self, &wwlg_tunerKey, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }
}

// MARK: - Backdrop tuner

/// Walks the window's layer tree every frame, finds every Liquid Glass backdrop
/// filter (identified by its `inputBlurOpacity0` key, not by name), flattens the
/// progressive-blur ramp, and optionally copies the `.clear` backing's neutral
/// face colour matrix onto `.regular` SwiftUI glass surfaces.
@MainActor
private final class GlassBackdropTuner: NSObject {
    private weak var window: NSWindow?
    // `nonisolated(unsafe)` so the nonisolated `deinit` can invalidate it. All
    // other access is on the main actor; `CADisplayLink.invalidate()` is safe.
    private nonisolated(unsafe) var link: CADisplayLink?
    private var didLogOnce = false
    private var configuration: WholeWindowGlassConfiguration
    private let dumpTreeRequested = ProcessInfo.processInfo.environment["WHOLE_WINDOW_GLASS_DUMP"] == "1"

    /// Neutral face colour matrix captured once from the `.clear` backing.
    /// `inputFaceOpacity` is deliberately NOT copied: `.regular` surfaces keep
    /// their own face body for legibility (zeroing it leaves text floating over
    /// the raw backdrop).
    private var clearFace: [String: Any]?
    private let faceKeys = [
        "inputFaceColorMatrixWhite", "inputFaceColorMatrixBlack",
        "inputFaceColorMatrixSaturation", "inputFaceColorMatrixFillColor",
        "inputFaceColorMatrixMaxLuma", "inputFaceColorMatrixMaxLumaSDR",
    ]

    init(window: NSWindow, configuration: WholeWindowGlassConfiguration) {
        self.window = window
        self.configuration = configuration
        super.init()
        guard let view = window.contentView else { return }
        let link = view.displayLink(target: self, selector: #selector(tick))
        link.add(to: .current, forMode: .common)
        self.link = link
    }

    deinit { link?.invalidate() }

    func updateConfiguration(_ configuration: WholeWindowGlassConfiguration) {
        self.configuration = configuration
        if !configuration.neutralizeRegularGlassFaces { clearFace = nil }
        tick()
    }

    @objc private func tick() {
        // Prism: keep system accessibility accommodations intact.
        guard !NSWorkspace.shared.accessibilityDisplayShouldReduceTransparency,
              !NSWorkspace.shared.accessibilityDisplayShouldIncreaseContrast else { return }
        guard let root = window?.contentView?.layer else { return }
        if configuration.neutralizeRegularGlassFaces { captureClearFace() }
        let backing = window?.contentView?.subviews.first(where: { $0.identifier == glassBackingID })?.layer
        let tuned = tuneTree(root, backing: backing)
        if configuration.logsDiagnostics, !didLogOnce {
            didLogOnce = true
            NSLog("WholeWindowLiquidGlass: tuned \(tuned) glass-filter layer(s) in \(window?.title ?? "?")")
            if dumpTreeRequested || tuned == 0 { dumpTree(root, depth: 0) }
        }
    }

    private func walk(_ layer: CALayer, _ visit: (CALayer) -> Void) {
        visit(layer)
        layer.sublayers?.forEach { walk($0, visit) }
    }

    private func tuneTree(_ layer: CALayer, backing: CALayer?, insideBacking: Bool = false) -> Int {
        let inside = insideBacking || layer === backing
        var count = tuneGlassFilters(on: layer, uniformBlur: inside) ? 1 : 0
        for child in layer.sublayers ?? [] {
            count += tuneTree(child, backing: backing, insideBacking: inside)
        }
        return count
    }

    private func captureClearFace() {
        guard clearFace == nil,
              let backing = window?.contentView?.subviews.first(where: { $0.identifier == glassBackingID }),
              let backingLayer = backing.layer else { return }
        var found: [String: Any]?
        walk(backingLayer) { layer in
            guard found == nil, let filters = layer.filters else { return }
            for f in filters {
                let obj = f as AnyObject
                guard let keys = obj.value(forKey: "inputKeys") as? [String],
                      keys.contains("inputBlurOpacity0") else { continue }
                var dict: [String: Any] = [:]
                for k in faceKeys where keys.contains(k) {
                    if let v = obj.value(forKey: k) { dict[k] = v }
                }
                found = dict
                return
            }
        }
        if let found { clearFace = found }
    }

    @discardableResult
    private func tuneGlassFilters(on layer: CALayer, uniformBlur: Bool) -> Bool {
        guard let filters = layer.filters, !filters.isEmpty else { return false }
        var touched = false
        for f in filters {
            let obj = f as AnyObject
            guard let keys = obj.value(forKey: "inputKeys") as? [String],
                  keys.contains("inputBlurOpacity0") else { continue }
            let name = (obj.value(forKey: "name") as? String) ?? ""
            func set(_ value: Any, _ key: String) {
                obj.setValue(value, forKey: key)
                if !name.isEmpty { layer.setValue(value, forKeyPath: "filters.\(name).\(key)") }
            }
            if configuration.flattenProgressiveBlur {
                // Equal nonzero band weights make blur uniform. Zero weights
                // bypass blur entirely, even while inputBlurRadius changes.
                // Apply this only to the window backdrop, preserving controls.
                for key in keys where key.hasPrefix("inputBlurOpacity") { set(uniformBlur ? 1.0 : 0.0, key) }
                if keys.contains("inputBlurRadius") { set(configuration.backdropBlurRadius, "inputBlurRadius") }
            }
            if let clearFace {
                for (k, v) in clearFace where keys.contains(k) { set(v, k) }
            }
            touched = true
        }
        if touched { layer.filters = filters } // re-assign so the layer re-reads mutated copies
        return touched
    }

    private func dumpTree(_ layer: CALayer, depth: Int) {
        guard depth < 12 else { return }
        let pad = String(repeating: "  ", count: depth)
        let cls = String(describing: type(of: layer))
        let fnames = (layer.filters ?? []).map { ($0 as AnyObject).value(forKey: "name") as? String ?? "?" }
        let fsuffix = fnames.isEmpty ? "" : " filters=\(fnames)"
        NSLog("WholeWindowLiquidGlass: \(pad)\(cls) frame=\(layer.frame)\(fsuffix)")
        layer.sublayers?.forEach { dumpTree($0, depth: depth + 1) }
    }
}
#endif

#endif

