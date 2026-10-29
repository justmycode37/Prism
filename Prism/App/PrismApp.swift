import AppKit
import SwiftUI

/// AppKit owns the transparent window; SwiftUI owns everything inside it.
@main
@MainActor
enum PrismApp {
    static func main() {
        let app = NSApplication.shared
        let delegate = PrismApplicationDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        withExtendedLifetime(delegate) {
            app.run()
        }
    }
}

@MainActor
final class PrismApplicationDelegate: NSObject, NSApplicationDelegate {
    private let preferences = AppearancePreferences()
    private var mainWindow: NSWindow?
    private let runtime = RuntimeClient()

    func applicationDidFinishLaunching(_ notification: Notification) {
        preferences.apply()
        installMenus()
        showMainWindow()
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMainWindow()
        return true
    }

    @objc private func showMainWindow() {
        if let mainWindow {
            mainWindow.makeKeyAndOrderFront(nil)
            return
        }

        let window = PrismWindow(
            contentRect: NSRect(x: 0, y: 0, width: 840, height: 560),
            styleMask: [.titled, .fullSizeContentView, .resizable, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Prism"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.titlebarSeparatorStyle = .none
        for button in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
            window.standardWindowButton(button)?.isHidden = true
        }
        window.setAccessibilityLabel("Prism")
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        window.isMovableByWindowBackground = true
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 600, height: 420)
        window.collectionBehavior = [.fullScreenNone]

        let content = PrismHomeView(runtime: runtime, preferences: preferences)
        installGlass(content, in: window)
        window.center()
        window.setFrameAutosaveName("PrismMinimalWorkspaceWindow")
        mainWindow = window
        window.makeKeyAndOrderFront(nil)
    }

    private func installGlass<Content: View>(_ content: Content, in window: NSWindow) {
        #if PRISM_EXPERIMENTAL_GLASS
        let root = PrismGlassRoot(preferences: preferences, content: content)
        let hostingView = TransparentHostingView(rootView: root)
        hostingView.wantsLayer = true
        hostingView.layer?.backgroundColor = NSColor.clear.cgColor
        let container = NSView(frame: window.contentLayoutRect)
        container.wantsLayer = true
        container.layer?.backgroundColor = NSColor.clear.cgColor
        hostingView.frame = container.bounds
        hostingView.autoresizingMask = [.width, .height]
        container.addSubview(hostingView)
        // The glass installer adds a sibling behind this hosting view. Putting
        // glass inside the hosting view can sample and distort the label itself.
        window.contentView = container
        #else
        let hostingView = TransparentHostingView(rootView: content)
        let glass = NSGlassEffectView(frame: window.contentLayoutRect)
        glass.style = .clear
        glass.cornerRadius = PrismTheme.cornerRadius
        glass.contentView = hostingView
        window.contentView = glass
        #endif
    }

    @objc private func showSettings() {
        runtime.selectedPage = .settings
        showMainWindow()
    }

    @objc private func closeWindow() { NSApp.keyWindow?.close() }

    private func installMenus() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu(title: "Prism")
        appMenu.addItem(withTitle: "About Prism", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        let settings = appMenu.addItem(withTitle: "Settings…", action: #selector(showSettings), keyEquivalent: ",")
        settings.target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Prism", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Prism", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        menu.addItem(appItem)

        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        menu.addItem(editItem)

        let windowItem = NSMenuItem()
        let windowMenu = NSMenu(title: "Window")
        let show = windowMenu.addItem(withTitle: "Show Prism", action: #selector(showMainWindow), keyEquivalent: "0")
        show.target = self
        let close = windowMenu.addItem(withTitle: "Close", action: #selector(closeWindow), keyEquivalent: "w")
        close.target = self
        windowItem.submenu = windowMenu
        menu.addItem(windowItem)
        NSApp.mainMenu = menu
        NSApp.windowsMenu = windowMenu
    }
}

#if PRISM_EXPERIMENTAL_GLASS
private struct PrismGlassRoot<Content: View>: View {
    @ObservedObject var preferences: AppearancePreferences
    let content: Content

    var body: some View {
        content.wholeWindowLiquidGlass(.init(
            backdropBlurRadius: preferences.backdropBlurRadius,
            flattenProgressiveBlur: true,
            neutralizeRegularGlassFaces: false,
            cornerRadius: PrismTheme.cornerRadius
        ))
    }
}
#endif

final class PrismWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

final class TransparentHostingView<Content: View>: NSHostingView<Content> {
    override var isOpaque: Bool { false }
}
