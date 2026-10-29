import SwiftUI

enum PrismTheme {
    static let cornerRadius: CGFloat = 38
    static let cyan = Color(red: 0.16, green: 0.94, blue: 1)
    static let blue = Color(red: 0.16, green: 0.31, blue: 1)
    static let violet = Color(red: 0.61, green: 0.34, blue: 1)
    static let rose = Color(red: 1, green: 0.24, blue: 0.51)
    static let amber = Color(red: 1, green: 0.73, blue: 0.38)

    static let spectrum: [Color] = [cyan, blue, violet, rose, amber, .white, cyan]
}
