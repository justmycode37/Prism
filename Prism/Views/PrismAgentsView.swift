import SwiftUI

struct PrismAgentsView: View {
    @ObservedObject var runtime: RuntimeClient

    private var agents: [AgentStatus] { runtime.state?.agents ?? [] }
    private var roles: [AgentRole] { runtime.state?.agentRoles ?? [] }

    var body: some View {
        let roster = AgentRoster(agents: agents)
        VStack(alignment: .leading, spacing: 16) {
            if agents.isEmpty && roles.isEmpty {
                ConsoleCard {
                    ConsoleEmptyState(symbol: PrismPage.agents.symbol, title: "No agents yet", message: "Agents and roles appear when the runtime connects.")
                }
            } else {
                ConsoleMetricGrid {
                    ConsoleMetric(title: "Agents", value: "\(agents.count)", detail: "Across all roles")
                    ConsoleMetric(title: "Active", value: "\(agents.filter { ["working", "running", "active"].contains($0.status) }.count)", detail: "Currently working")
                    ConsoleMetric(title: "Model cost today", value: runtime.state.map { PrismFormat.money($0.budget.spentTodayUsd) } ?? "—", detail: "All agents · estimated")
                }
                ForEach(roles) { role in roleWidget(role) }
                let otherAgents = agents.filter { agent in !roles.contains { $0.id == (agent.roleId ?? agent.id) } }
                if !otherAgents.isEmpty {
                    ConsoleCard("Other agents", caption: "\(otherAgents.count) agents") {
                        ForEach(otherAgents) { agent in agentRow(agent) }
                    }
                }
                ConsoleCard {
                    DisclosureGroup("Reporting lines") {
                        VStack(alignment: .leading, spacing: 14) {
                            if !roster.rows.isEmpty { rosterSection("Firm hierarchy", rows: roster.rows) }
                            if !roster.unlinkedRows.isEmpty { rosterSection("Other agents", rows: roster.unlinkedRows) }
                        }.padding(.top, 12)
                    }.font(.system(size: 12))
                }
            }
        }
    }

    private func roleWidget(_ role: AgentRole) -> some View {
        let members = agents.filter { ($0.roleId ?? $0.id) == role.id }
        let latest = members.filter { !$0.summary.isEmpty }.sorted {
            (PrismFormat.date($0.lastRunAt) ?? .distantPast) > (PrismFormat.date($1.lastRunAt) ?? .distantPast)
        }.first
        return ConsoleCard {
            HStack(spacing: 12) {
                ConsoleIcon(symbol: roleSymbol(role))
                VStack(alignment: .leading, spacing: 4) {
                    Text(role.name).font(.system(size: 14, weight: .medium))
                    Text(role.team).font(.system(size: 10)).foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
                Text(staffingDescription(role)).font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Text(role.mandate).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2)
            if let latest {
                Text(latest.summary).font(.system(size: 12)).lineLimit(2)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            DisclosureGroup("Agents & role details") {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(members) { agent in agentRow(agent) }
                    detail("Role mandate", text: role.mandate)
                    metadata("Role type", value: PrismFormat.label(role.kind))
                    if let template = role.templateRoleId {
                        metadata("Based on", value: roles.first { $0.id == template }?.name ?? PrismFormat.label(template))
                    }
                    creationDetails(createdBy: role.createdBy, createdAt: role.createdAt)
                    manualDisclosure(instructions: role.instructions, path: role.instructionPath)
                }.font(.system(size: 11)).padding(.top, 10)
            }.font(.system(size: 11)).tint(.secondary)
        }
    }

    private func roleSymbol(_ role: AgentRole) -> String {
        let key = role.templateRoleId ?? role.id
        if key.contains("collector") { return "tray.and.arrow.down" }
        if key.contains("analyst") { return "chart.xyaxis.line" }
        if key.contains("strategist") { return "square.stack.3d.up" }
        if key.contains("executor") { return "arrow.triangle.swap" }
        if key.contains("review") { return "checkmark.seal" }
        if key.contains("schedul") { return "clock" }
        return "person"
    }

    private func rosterSection(_ title: String, rows: [AgentRoster.Row]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased()).font(.system(size: 9, weight: .medium, design: .monospaced)).foregroundStyle(.tertiary)
            ForEach(rows) { row in
                agentRow(row.agent)
                    .padding(.leading, CGFloat(min(row.depth, 5)) * 12)
            }
        }
    }

    private func agentRow(_ agent: AgentStatus) -> some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 13) {
                if !agent.task.isEmpty { detail("Current assignment", text: agent.task) }
                if !agent.summary.isEmpty { detail("Latest report", text: agent.summary) }
                if let mandate = agent.mandate, !mandate.isEmpty {
                    detail("Mandate", text: mandate)
                }
                if let reason = agent.creationReason, !reason.isEmpty {
                    detail("Why created", text: reason)
                }
                if let capabilities = agent.capabilities {
                    capabilityDisclosure(capabilities)
                }
                if let activity = agent.recentTools {
                    toolActivityDisclosure(activity)
                }
                VStack(alignment: .leading, spacing: 7) {
                    metadata("Agent ID", value: agent.id)
                    metadata("Role", value: roleName(for: agent))
                    metadata("Reports to", value: parentName(for: agent))
                    metadata("Model", value: modelDescription(agent))
                    metadata("Last run", value: PrismFormat.time(agent.lastRunAt))
                    creationDetails(createdBy: agent.createdBy, createdAt: agent.createdAt)
                }
                manualDisclosure(instructions: agent.instructions, path: agent.instructionPath)
            }
            .font(.system(size: 11)).foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 14).padding(.leading, 18)
        } label: {
            HStack(alignment: .center, spacing: 10) {
                VStack(alignment: .leading, spacing: 5) {
                    Text(agent.name)
                        .font(.system(size: 13, weight: agent.isOrchestrator ? .medium : .regular))
                    Text(agentSubtitle(agent))
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                    if !agent.summary.isEmpty {
                        Text(agent.summary).font(.system(size: 10)).foregroundStyle(.secondary)
                            .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 8)
                StatusPill(status: agent.status)
            }
            .padding(.vertical, 6)
        }
    }

    private func capabilityDisclosure(_ capabilities: [AgentCapability]) -> some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 12) {
                if capabilities.isEmpty {
                    Text("No capabilities reported.").foregroundStyle(.tertiary)
                }
                ForEach(capabilities) { capability in
                    HStack(alignment: .top, spacing: 9) {
                        Image(systemName: capability.available ? "checkmark.circle" : "minus.circle")
                            .foregroundStyle(capability.available ? Color.mint : .secondary)
                            .accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: 5) {
                            HStack(alignment: .firstTextBaseline, spacing: 10) {
                                Text(capability.name).fontWeight(.medium).foregroundStyle(.primary)
                                Text(capability.available ? "Available" : "Unavailable")
                                    .font(.system(size: 10)).foregroundStyle(.tertiary)
                            }
                            Text(capability.description)
                            if let reason = capability.reason, !reason.isEmpty {
                                Text(reason).foregroundStyle(capability.available ? Color.secondary : .orange)
                            }
                        }
                        .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .padding(.vertical, 10).padding(.leading, 18)
        } label: {
            HStack(spacing: 10) {
                Text("Capabilities").fontWeight(.medium)
                Text("\(capabilities.filter(\.available).count) of \(capabilities.count) available")
                    .font(.system(size: 10)).foregroundStyle(.tertiary)
            }
        }
    }

    private func toolActivityDisclosure(_ activity: [AgentToolActivity]) -> some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 12) {
                if activity.isEmpty {
                    Text("No tool activity yet.").foregroundStyle(.tertiary)
                }
                ForEach(activity) { tool in
                    VStack(alignment: .leading, spacing: 5) {
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text(PrismFormat.label(tool.name)).fontWeight(.medium).foregroundStyle(.primary)
                            Spacer(minLength: 8)
                            StatusPill(status: tool.status)
                        }
                        if !tool.summary.isEmpty {
                            Text(tool.summary).textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Text("\(PrismFormat.time(tool.createdAt)) · \(toolDuration(tool.durationMs))")
                            .font(.system(size: 10)).foregroundStyle(.tertiary)
                    }
                }
            }
            .padding(.vertical, 10).padding(.leading, 18)
        } label: {
            HStack(spacing: 10) {
                Text("Recent tool activity").fontWeight(.medium)
                if !activity.isEmpty {
                    Text("\(activity.count) calls").font(.system(size: 10)).foregroundStyle(.tertiary)
                }
            }
        }
    }

    private func toolDuration(_ milliseconds: Int) -> String {
        guard milliseconds >= 1_000 else { return "\(milliseconds) ms" }
        let seconds = (Double(milliseconds) / 1_000).formatted(.number.precision(.fractionLength(1)))
        return "\(seconds) s"
    }

    private func detail(_ title: String, text: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 10, weight: .medium)).foregroundStyle(.tertiary)
            Text(text).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        }
    }

    private func metadata(_ label: String, value: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Text(label).foregroundStyle(.tertiary).frame(width: 76, alignment: .leading)
            Text(value).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        }.font(.system(size: 10))
    }

    @ViewBuilder private func creationDetails(createdBy: String?, createdAt: String?) -> some View {
        if let createdBy, !createdBy.isEmpty {
            metadata("Created by", value: agents.first { $0.id == createdBy }?.name ?? PrismFormat.label(createdBy))
        }
        if let date = PrismFormat.date(createdAt) {
            metadata("Created", value: date.formatted(date: .abbreviated, time: .shortened))
        }
    }

    @ViewBuilder private func manualDisclosure(instructions: String?, path: String?) -> some View {
        if let instructions, !instructions.isEmpty {
            AgentInstructionDisclosure(instructions: instructions)
        } else if let path, !path.isEmpty {
            metadata("Instructions", value: path)
        }
    }

    private func roleName(for agent: AgentStatus) -> String {
        roles.first { $0.id == (agent.roleId ?? agent.id) }?.name ?? agent.name
    }

    private func parentName(for agent: AgentStatus) -> String {
        guard let parent = agent.parentId, !parent.isEmpty else { return "Firm oversight" }
        return agents.first { $0.id == parent }?.name ?? PrismFormat.label(parent)
    }

    private func agentSubtitle(_ agent: AgentStatus) -> String {
        var parts = [roleName(for: agent), agent.isOrchestrator ? "Orchestrator" : "Specialist"]
        if agent.isDynamic == true { parts.append("Created") }
        return parts.joined(separator: " · ")
    }

    private func modelDescription(_ agent: AgentStatus) -> String {
        guard let model = agent.model, !model.isEmpty else { return "Not reported" }
        let name: String
        switch model {
        case "gpt-6-luna": name = "GPT-6 Luna"
        case "gpt-6.1-sol": name = "GPT-6.1 Sol"
        case "gpt-6-astra": name = "GPT-6 Astra"
        default: name = model
        }
        guard let effort = agent.reasoningEffort, !effort.isEmpty else { return name }
        return "\(name) · \(PrismFormat.label(effort)) reasoning"
    }

    private func staffingDescription(_ role: AgentRole) -> String {
        let count = agents.filter { ($0.roleId ?? $0.id) == role.id }.count
        switch count {
        case 0: return "Unstaffed"
        case 1: return "1 agent"
        default: return "\(count) agents"
        }
    }
}

/// Flattening keeps every agent visible while guarding against incomplete or cyclic reporting lines.
private struct AgentRoster {
    struct Row: Identifiable {
        let agent: AgentStatus
        let depth: Int
        var id: String { agent.id }
    }

    let rows: [Row]
    let unlinkedRows: [Row]

    init(agents: [AgentStatus]) {
        var knownIDs = Set<String>()
        let uniqueAgents = agents.filter { knownIDs.insert($0.id).inserted }
        let children = Dictionary(grouping: uniqueAgents) { $0.parentId ?? "" }
        var visited = Set<String>()

        func ordered(_ agents: [AgentStatus]) -> [AgentStatus] {
            agents.sorted {
                if $0.isOrchestrator != $1.isOrchestrator { return $0.isOrchestrator }
                if $0.team != $1.team { return $0.team.localizedStandardCompare($1.team) == .orderedAscending }
                return $0.name.localizedStandardCompare($1.name) == .orderedAscending
            }
        }

        func append(_ agent: AgentStatus, depth: Int, to rows: inout [Row]) {
            guard visited.insert(agent.id).inserted else { return }
            rows.append(Row(agent: agent, depth: depth))
            for child in ordered(children[agent.id] ?? []) {
                append(child, depth: depth + 1, to: &rows)
            }
        }

        var hierarchy: [Row] = []
        for root in ordered(children[""] ?? []) { append(root, depth: 0, to: &hierarchy) }
        var unlinked: [Row] = []
        for agent in ordered(uniqueAgents) where !visited.contains(agent.id) {
            append(agent, depth: 0, to: &unlinked)
        }
        rows = hierarchy
        unlinkedRows = unlinked
    }
}

private struct AgentInstructionDisclosure: View {
    let instructions: String
    @State private var isExpanded = false

    var body: some View {
        DisclosureGroup(isExpanded: $isExpanded) {
            if isExpanded {
                AgentManualView(markdown: instructions)
                    .padding(.vertical, 12).padding(.leading, 12)
            }
        } label: {
            Text("Role instructions").font(.system(size: 11, weight: .medium))
        }
    }
}

private struct AgentManualView: View {
    private struct Block: Identifiable {
        enum Kind { case heading(Int), prose, code, divider }
        let id: Int
        let kind: Kind
        let text: String
    }

    private let blocks: [Block]

    init(markdown: String) {
        var result: [Block] = []
        var paragraph: [String] = []
        var inCode = false

        func flush() {
            guard !paragraph.isEmpty else { return }
            result.append(Block(id: result.count, kind: inCode ? .code : .prose, text: paragraph.joined(separator: "\n")))
            paragraph.removeAll()
        }

        var lines = markdown.components(separatedBy: .newlines)
        if lines.first?.trimmingCharacters(in: .whitespaces) == "---",
           let end = lines.dropFirst().firstIndex(where: { $0.trimmingCharacters(in: .whitespaces) == "---" }) {
            lines.removeFirst(end + 1)
        }

        for line in lines {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") {
                flush()
                inCode.toggle()
            } else if inCode {
                paragraph.append(line)
            } else if trimmed.isEmpty {
                flush()
            } else if trimmed == "---" || trimmed == "***" {
                flush()
                result.append(Block(id: result.count, kind: .divider, text: ""))
            } else {
                let level = trimmed.prefix { $0 == "#" }.count
                if (1...6).contains(level), trimmed.dropFirst(level).hasPrefix(" ") {
                    flush()
                    result.append(Block(id: result.count, kind: .heading(level), text: String(trimmed.dropFirst(level + 1))))
                } else {
                    paragraph.append(line)
                }
            }
        }
        flush()
        blocks = result
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(blocks) { block in
                switch block.kind {
                case .heading(let level):
                    Text(inlineMarkdown(block.text))
                        .font(.system(size: level == 1 ? 16 : 12, weight: .semibold))
                        .foregroundStyle(.primary).padding(.top, level == 1 ? 0 : 8)
                        .accessibilityAddTraits(.isHeader)
                case .prose:
                    Text(inlineMarkdown(block.text)).font(.system(size: 11)).lineSpacing(4)
                case .code:
                    Text(block.text).font(.system(size: 10, design: .monospaced)).lineSpacing(3)
                case .divider:
                    Divider().overlay(.primary.opacity(0.06))
                }
            }
        }
        .textSelection(.enabled)
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func inlineMarkdown(_ text: String) -> AttributedString {
        (try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(text)
    }
}
