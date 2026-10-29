import SwiftUI

struct PrismExecutionView: View {
    @ObservedObject var runtime: RuntimeClient
    @State private var activityExpanded = true
    var body: some View {
        VStack(alignment: .leading, spacing: 32) {
            PrismPortfolioView(runtime: runtime)
            DisclosureGroup("Activity", isExpanded: $activityExpanded) {
                PrismActivityView(runtime: runtime).padding(.top, 16).padding(.leading, 18)
            }.font(.system(size: 13)).padding(.horizontal, 4)
        }
    }
}
