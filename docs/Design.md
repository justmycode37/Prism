# Prism — macOS design plan

22 October 2026 · current implementation

## Final visual direction

A restrained glass widget with a small, inset glass icon rail on the left. Overview centers the total all-time paper P&L and percentage above a thin history line, with runtime status and one Start/Stop control below. The rest of the interface uses the native system font.

The user's latest references call for transparent optical depth, rounded corners, a soft transition at the perimeter, and refraction of the backdrop near the edges. There is no crisp decorative outline, rainbow frame, tagline, or stacked dashboard cards. Native glass provides the lens behavior; a diffuse highlight gives the surface thickness without drawing a separate frame.

## Dimensions and behavior

| Element | Current specification |
|---|---|
| Main window | 840 × 560-point initial content size; minimum 600 × 420; no custom maximum |
| Resize | Normal AppKit resizable window with hidden titlebar/traffic-light visuals; drag edges or corners |
| Position | Move by dragging empty glass; remember main window frame |
| Corners | 38-point glass radius |
| Navigation | Compact icon-only rail, native clear glass, tooltips and accessibility labels |
| Overview | Total all-time paper P&L, percentage of initial capital, and a thin line using every recorded valuation at its actual timestamp; no axes, grid, legend, fill, or chart background. Runtime status and one glass Start/Stop control sit below. |
| Sections | Overview, Research, Test, Execution, Agents — five rail items |
| Details | Research holds ideas; Test holds backtests; Execution holds simulated positions, fills and activity; Agents shows the collector, analyst, strategist and executor cascade, with expandable assignments, reports, model details and role manuals; the role catalog describes the four layers |
| Settings | Appearance with independent blur and milkiness sliders |

The main window uses native window resizing with visually hidden chrome. Settings opens from the small upper-right gear within the same glass window, outside the five-section rail. The user can close the main window and reopen it from the Dock or Window → Show Prism.

## Rendering architecture

The `installGlass` method in `PrismApp.swift` retains the approved GitHub renderer. A clear AppKit container holds two separate layers: the native glass backdrop, then the transparent SwiftUI hosting view. Keeping these as siblings prevents the backdrop from sampling controls. Operational pages use the same surface, with no stacked opaque dashboard cards. The navigation rail uses clear glass rather than a milky regular-glass plate.

The prototype vendors [WholeWindowLiquidGlass](https://github.com/vientooscuro/WholeWindowLiquidGlass), which uses `NSGlassEffectView` and adjusts its private rendering filters. The progressive blur bands use equal nonzero weights on the main window backdrop, so the radius controls visible blur across its surface. Smaller controls retain their existing glass treatment. A Background blur slider controls the backdrop radius from 0 to 100, defaulting to 40, shared by Light and Dark. It updates the existing renderer immediately without changing tint, opacity, brightness, or saturation settings. A separate Milkiness slider controls the appearance veil from 0% to 100%, defaulting to 0%: no added white overlay in Light or black overlay in Dark. Both values persist locally. Milkiness is a compositing opacity, not an exact measured transmission percentage.

`GlassSurface` adds only diffuse grazing light and the appearance veil. A crisp border is added solely when Increase Contrast is enabled. Native glass handles refraction; no screenshot, wallpaper copy, or screen-recording permission is used.

The local build enables `PRISM_EXPERIMENTAL_GLASS`. Removing that compilation condition excludes the private implementation and uses the public [NSGlassEffectView](https://developer.apple.com/documentation/appkit/nsglasseffectview) fallback. The sharper prototype renderer requires OS-version testing and cannot be included in an App Store target. See `ThirdParty.md` for source provenance and licenses.

## Appearance and accessibility

System is the default. Light and Dark overrides, blur, and milkiness persist in the app's own preferences. Settings uses the same material as the rest of the app, with a stronger glass treatment on the selected appearance control.

Reduce Transparency replaces the decorative surface with an opaque equivalent. Increase Contrast supplies a stronger boundary. The experimental tuner stops applying changes when either accommodation is enabled, allowing native material updates to take effect. The blur slider is disabled while these accommodations override it; milkiness is disabled with Reduce Transparency. The public fallback build omits the blur slider because it does not expose a backdrop radius. There is no animated decorative glow; the renderer does maintain a display link to reapply its glass adjustments as AppKit rebuilds its layers. Profile that idle work before production distribution.

The Start/Stop control exposes an accessible label for starting or stopping all agents. Navigation icons have page labels and tooltips. Appearance buttons expose their selection state. Command-comma opens settings; Command-W closes the key window; Command-0 shows Prism; Command-Q quits. The standard Edit menu supports text editing and secure-field paste.

## Source layout

- `App/PrismApp.swift`: application lifecycle, native window behavior, shared glass installation, and menus.
- `Design/GlassSurface.swift`: diffuse finish, frosting, accessibility appearance.
- `Design/PrismWordmark.swift` and `PrismTypography.swift`: retained wordmark assets, no longer rendered in the current interface.
- `Views/PrismHomeView.swift`: glass rail and shared page container.
- `Views/PrismOverviewView.swift`: all-time paper P&L chart and runtime controls; hover selects a recorded value without adding grid lines.
- `Views/PrismSettingsView.swift`: appearance controls.
- `Appearance/AppearancePreferences.swift`: stored appearance, blur, and milkiness preferences.
- `Vendor/WholeWindowLiquidGlass.swift`: attributed renderer behind a compilation condition.
- `Resources/Fonts/Raleway.ttf`: retained font asset; licenses included in app resources.

Xcode previews show the SwiftUI composition. Judge desktop refraction in the running app, because a preview or an isolated window capture does not reproduce the complete on-screen backdrop.

## Operational boundaries

Keep Overview as the resting state. Operational pages show actual backend state, failed experiments, role outputs, market observations, and ledger-derived simulated P&L. Detailed findings stay behind disclosure controls. Offline state is explicit; previous snapshots are identified as stale.

Before live trading, provide visible environment, runtime health, data freshness, incidents, and emergency controls. Explain the difference between pausing research, blocking new exposure, canceling entries, and flattening positions. The current Overview is not the full future trading control surface.

## Remaining review

Validate window dragging/resizing on the actual Mac, keyboard focus, VoiceOver, contrast over varied backdrops, system accessibility accommodations, and idle energy use. Current build and interaction observations are recorded in `Validation.md`.
