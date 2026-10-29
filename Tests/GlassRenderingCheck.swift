// Manual rendering regression: coloured stripes and text sit behind the real glass.
// See docs/Validation.md for build and comparison instructions.
import AppKit
import SwiftUI

@main
@MainActor
enum GlassCheck {
    static func main() {
        let app = NSApplication.shared
        let delegate = Delegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        withExtendedLifetime(delegate) { app.run() }
    }
}

@MainActor
final class Delegate: NSObject, NSApplicationDelegate {
    var window: NSWindow?
    func applicationDidFinishLaunching(_ notification: Notification) {
        let window = NSWindow(contentRect: NSRect(x: 300, y: 200, width: 720, height: 480), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "Glass Rendering Check"
        let pattern = PatternView(frame: window.contentLayoutRect)
        pattern.wantsLayer = true
        let host = NSHostingView(rootView: CheckView())
        host.frame = pattern.bounds
        host.autoresizingMask = [.width, .height]
        host.wantsLayer = true
        host.layer?.backgroundColor = NSColor.clear.cgColor
        pattern.addSubview(host)
        window.contentView = pattern
        self.window = window
        window.makeKeyAndOrderFront(nil)
        appMenu()
        NSApp.activate(ignoringOtherApps: true)
    }
    func appMenu() {
        let menu = NSMenu()
        let item = NSMenuItem()
        item.submenu = NSMenu()
        item.submenu?.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(item)
        NSApp.mainMenu = menu
    }
}

final class PatternView: NSView {
    override func draw(_ dirtyRect: NSRect) {
        NSColor.white.setFill()
        bounds.fill()
        for column in 0..<36 {
            (column.isMultiple(of: 2) ? NSColor.systemBlue : NSColor.systemOrange).setFill()
            NSRect(x: column * 20, y: 0, width: 20, height: 480).fill()
        }
        let text = "PRISM 0123456789" as NSString
        text.draw(at: NSPoint(x: 80, y: 250), withAttributes: [.font: NSFont.systemFont(ofSize: 45), .foregroundColor: NSColor.black])
    }
}

struct CheckView: View {
    @State private var radius = 0.0
    var body: some View {
        VStack {
            HStack {
                Button("Blur 0") { radius = 0 }
                Button("Blur 100") { radius = 100 }
                Button("Light") { NSApp.appearance = NSAppearance(named: .aqua) }
                Button("Dark") { NSApp.appearance = NSAppearance(named: .darkAqua) }
                Text("Radius \(Int(radius))")
            }.padding(20).foregroundStyle(.black).background(.white)
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .wholeWindowLiquidGlass(.init(backdropBlurRadius: radius, neutralizeRegularGlassFaces: false, cornerRadius: 24))
    }
}
