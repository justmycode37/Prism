import CoreText
import SwiftUI

@MainActor
enum PrismTypography {
    /// A light geometric face, bundled and registered only for this process.
    static let wordmark: Font = {
        if let url = Bundle.main.url(forResource: "Raleway", withExtension: "ttf", subdirectory: "Fonts") {
            CTFontManagerRegisterFontsForURL(url as CFURL, .process, nil)
        }
        let descriptor = CTFontDescriptorCreateWithAttributes([
            kCTFontNameAttribute: "Raleway-Thin",
            kCTFontVariationAttribute: [NSNumber(value: 0x77676874): NSNumber(value: 300)]
        ] as CFDictionary)
        return Font(CTFontCreateWithFontDescriptor(descriptor, 48, nil))
    }()
}
