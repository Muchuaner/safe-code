import AppKit

@MainActor
final class GatewayAnalysis: NSStackView {
    private static let ranges = ["1h", "24h", "7d"]
    private static let rangeNames = ["近 1 小时", "近 24 小时", "近 7 天"]
    private static let mint = GatewayStyle.color(0x277c62)
    private static let blue = GatewayStyle.color(0x426fa7)
    private static let gold = GatewayStyle.color(0xac711c)
    private static let rose = GatewayStyle.color(0xbf5467)
    private static let violet = GatewayStyle.color(0x7964a7)
    private static let gray = GatewayStyle.color(0x85938d)

    private let trafficUnits = GatewaySelect()
    private let latencyUnits = GatewaySelect()
    private var rangeButtons: [GatewayButton] = []
    private var rangeIndex = 0
    private let summary = GatewayStyle.label("", color: GatewayStyle.muted, size: SafeCodeType.small)
    private let trafficSummary = GatewayStyle.label("", color: GatewayStyle.muted, size: SafeCodeType.small)
    private let traffic = GatewayLineChart()
    private let errors = GatewayLineChart()
    private let queue = NSStackView()
    private let latency = NSStackView()
    private let outcomes = NSStackView()
    private let models = NSStackView()
    private let changePeriod: () -> Void
    private let showRequests: () -> Void
    private var snapshot: [String: Any] = [:]
    var period: String { Self.ranges[rangeIndex] }

    init(changePeriod: @escaping () -> Void, showRequests: @escaping () -> Void) {
        self.changePeriod = changePeriod
        self.showRequests = showRequests
        super.init(frame: .zero)
        orientation = .vertical
        alignment = .width
        spacing = 12

        rangeButtons = Self.rangeNames.enumerated().map { index, name in
            let control = GatewayButton(name, kind:.segment, target:self, action:#selector(periodChanged(_:)))
            control.tag = index
            control.state = index == 0 ? .on : .off
            return control
        }
        let range = GatewayStyle.surface(Self.horizontal(rangeButtons, spacing:3),
            inset:NSEdgeInsets(top:3,left:3,bottom:3,right:3),radius:9,fill:GatewayStyle.header)
        let header = Self.vertical([GatewayStyle.label("流量与性能", size:SafeCodeType.large, weight:.semibold), summary],spacing:4)
        summary.heightAnchor.constraint(equalToConstant:16).isActive = true
        header.widthAnchor.constraint(greaterThanOrEqualToConstant:250).isActive = true
        let heading = Self.horizontal([header, NSView(), range])
        heading.heightAnchor.constraint(equalToConstant:52).isActive = true
        addFullWidth(heading)

        trafficUnits.addItems(withTitles: ["Token / 分钟", "请求 / 分钟", "Token 总量", "请求次数"])
        latencyUnits.addItems(withTitles: ["处理耗时", "排队等待"])
        for control in [trafficUnits, latencyUnits] {
            control.font = .systemFont(ofSize: SafeCodeType.small)
            control.target = self
            control.action = #selector(update)
        }
        let requestButton = GatewayButton("查看记录", kind: .standard, target: self, action: #selector(openRequests))
        let trafficCard = Self.card("吞吐趋势", stack: Self.vertical([trafficSummary, traffic], spacing: 6), action: trafficUnits)
        traffic.heightAnchor.constraint(equalToConstant: 172).isActive = true
        let liveCard = Self.card("并发与排队", stack: queue,
                                 action: GatewayStyle.label("● 实时", color: Self.mint, size: SafeCodeType.small))
        let top = Self.horizontal([trafficCard, liveCard])
        top.alignment = .top
        trafficCard.heightAnchor.constraint(equalTo:liveCard.heightAnchor).isActive = true
        trafficCard.widthAnchor.constraint(equalTo:top.widthAnchor,multiplier:2/3,constant:-8).isActive = true
        liveCard.widthAnchor.constraint(equalTo:top.widthAnchor,multiplier:1/3,constant:-4).isActive = true
        addFullWidth(top)

        let comparisons = Self.horizontal([
            Self.card("耗时分布", stack: latency, action: latencyUnits),
            Self.card("请求结果", stack: outcomes, action: requestButton),
            Self.card("错误与限流趋势", stack: Self.vertical([errors]),
                      action: GatewayStyle.label("失败 · 受限", color: GatewayStyle.muted, size: SafeCodeType.small))
        ])
        comparisons.distribution = .fillEqually
        comparisons.alignment = .top
        if let first = comparisons.arrangedSubviews.first {
            for next in comparisons.arrangedSubviews.dropFirst() {
                next.heightAnchor.constraint(equalTo:first.heightAnchor).isActive = true
            }
        }
        errors.heightAnchor.constraint(equalToConstant: 142).isActive = true
        addFullWidth(comparisons)

        addFullWidth(Self.card("模型用量", stack: models,
                                     action: GatewayStyle.label("历史累计", color: GatewayStyle.muted, size: SafeCodeType.small)))
        for stack in [queue, latency, outcomes, models] {
            stack.orientation = .vertical
            stack.alignment = .width
            stack.spacing = 7
        }
    }

    required init(coder: NSCoder) { fatalError("Use init(changePeriod:showRequests:)") }

    private func addFullWidth(_ child: NSView) {
        addArrangedSubview(child)
        child.widthAnchor.constraint(equalTo:widthAnchor).isActive = true
    }

    private static func horizontal(_ children: [NSView], spacing: CGFloat = 12) -> NSStackView {
        let stack = NSStackView(views: children)
        stack.spacing = spacing
        stack.alignment = .centerY
        return stack
    }

    private static func vertical(_ children: [NSView], spacing: CGFloat = 10) -> NSStackView {
        let stack = NSStackView(views: children)
        stack.orientation = .vertical
        stack.alignment = .width
        stack.spacing = spacing
        for child in children { child.widthAnchor.constraint(equalTo:stack.widthAnchor).isActive = true }
        return stack
    }

    private static func card(_ title: String, stack: NSStackView, action: NSView) -> NSView {
        let heading = horizontal([GatewayStyle.label(title, weight: .semibold), NSView(), action])
        stack.setContentHuggingPriority(.defaultLow, for: .vertical)
        let content = vertical([heading, stack])
        return GatewayStyle.surface(content, inset: NSEdgeInsets(top: 15, left: 16, bottom: 15, right: 16))
    }

    @objc private func openRequests() { showRequests() }
    @objc private func periodChanged(_ sender: NSButton) {
        let previous = rangeIndex
        rangeIndex = sender.tag
        for button in rangeButtons { button.state = button.tag == rangeIndex ? .on : .off; button.needsDisplay = true }
        if previous != rangeIndex { update(); changePeriod() }
    }
    @objc private func update() { render(snapshot) }

    func render(_ value: [String: Any]) {
        snapshot = value
        let candidate = value["analytics"] as? [String: Any] ?? [:]
        let data = candidate["period"] as? String == period ? candidate : [:]
        let count = max(0, min(168, Int(Display.number(data["count"]))))
        let start = Display.number(data["start"])
        let step = max(1, Display.number(data["step"]))
        let rows = data["timeline"] as? [[String: Any]] ?? []
        var slots: [Int: [String: Any]] = [:]
        for row in rows { slots[Int(Display.number(row["bucket"]))] = row }
        let points = (0..<count).map { slots[$0] ?? [:] }
        let requests = points.reduce(0) { $0 + Display.number($1["requests"]) }
        let tokens = points.reduce(0) { $0 + Display.number($1["tokens"]) }
        let periodName = Self.rangeNames[rangeIndex]
        summary.stringValue = data.isEmpty ? "正在读取" + periodName + "的统计"
            : "\(Display.integer(requests)) 次请求 · \(Display.integer(tokens)) Token"
        let segment = [300: "5 分钟", 3600: "1 小时", 86400: "1 天"][step] ?? ""
        trafficSummary.stringValue = data.isEmpty ? "" : periodName + " · 每段 " + segment
        let unit = max(0, trafficUnits.indexOfSelectedItem)
        let field = unit % 2 == 0 ? "tokens" : "requests"
        let labels = (0..<count).map { index in
            let date = Date(timeIntervalSince1970: Double(start+Int64(index)*step))
            return step >= 86400 ? date.formatted(.dateTime.month().day()) : date.formatted(.dateTime.hour().minute())
        }
        let values = points.map { Double(Display.number($0[field])) * (unit < 2 ? 60/Double(step) : 1) }
        traffic.set(labels: labels, series: requests == 0 ? [] : [(trafficUnits.titleOfSelectedItem ?? "", Self.mint, values)])
        errors.set(labels: labels, series: requests == 0 ? [] : [
            ("失败", Self.rose, points.map { Double(Display.number($0["failed"])) }),
            ("受限", Self.gold, points.map { Double(Display.number($0["limited"])) })
        ])
        renderQueue()
        renderLatency(data)
        renderOutcomes(data)
        renderModels()
    }

    private func replace(_ stack: NSStackView, with views: [NSView]) {
        for child in stack.arrangedSubviews {
            stack.removeArrangedSubview(child)
            child.removeFromSuperview()
        }
        for child in views {
            stack.addArrangedSubview(child)
            child.widthAnchor.constraint(equalTo:stack.widthAnchor).isActive = true
        }
    }

    private func label(_ text: String) -> NSTextField {
        GatewayStyle.label(text, color: GatewayStyle.muted, size: SafeCodeType.small)
    }

    private func paired(_ name: String, _ detail: String) -> NSStackView {
        Self.horizontal([label(name), NSView(), GatewayStyle.label(detail, size: SafeCodeType.small, weight: .semibold)])
    }

    private func progress(_ value: Int64, max limit: Int64, color: NSColor = GatewayStyle.accent) -> NSView? {
        guard limit > 0 else { return nil }
        return GatewayProgress(value: value, limit: limit, fill: color)
    }

    private func renderQueue() {
        let active = Display.number(snapshot["active_count"])
        let waiting = Display.number(snapshot["waiting"])
        let capacity = Display.number(snapshot["concurrency"])
        let limit = Display.number(snapshot["queue_limit"])
        var content: [NSView] = [paired("并发占用", "\(active) / " + (capacity == 0 ? "不限" : "\(capacity)"))]
        if let bar = progress(active, max: capacity) { content.append(bar) }
        content.append(paired("排队请求", limit > 0 ? "\(waiting) / \(limit)" : "\(waiting)"))
        if let bar = progress(waiting, max: limit, color: Self.gold) { content.append(bar) }
        let items = (snapshot["keys"] as? [[String: Any]] ?? []).filter {
            Display.number($0["active_count"] ?? $0["active"]) > 0 || Display.number($0["waiting"]) > 0
        }
        for item in items.prefix(4) {
            let running = Display.number(item["active_count"] ?? item["active"])
            let waiting = Display.number(item["waiting"])
            let fallback = String((item["id"] as? String ?? "").prefix(12))
            let note = item["note"] as? String ?? ""
            let states = [running > 0 ? "执行中 \(running)" : "", waiting > 0 ? "排队 \(waiting)" : ""].filter { !$0.isEmpty }
            content.append(paired(note.isEmpty ? fallback : note, states.joined(separator: " · ")))
        }
        if items.isEmpty { content.append(label("网关空闲")) }
        if items.count > 4 { content.append(label("还有 \(items.count-4) 把密钥正在等待")) }
        if let limit = snapshot["wait_limit"] as? NSNumber { content.append(label("最长等待 \(limit) 秒")) }
        replace(queue, with: content)
    }

    private func renderLatency(_ data: [String: Any]) {
        let choices = data["latency"] as? [String: [String: Any]] ?? [:]
        let measure = choices[latencyUnits.indexOfSelectedItem == 1 ? "wait_ms" : "duration_ms"] ?? [:]
        guard Display.number(measure["samples"]) > 0, let histogram = measure["histogram"] as? [NSNumber],
              let edges = data["latency_limits"] as? [NSNumber], histogram.count == edges.count+1, !edges.isEmpty else {
            replace(latency, with: [label("这个时段暂无耗时样本。")]); return
        }
        let average = Display.duration(measure["average_ms"] ?? NSNull())
        let percentile = Display.duration(measure["p95_ms"] ?? NSNull())
        var content: [NSView] = [label("平均 \(average) · P95 \(percentile)")]
        let peak = histogram.map(\.int64Value).max() ?? 1
        for (index, count) in histogram.enumerated() {
            let low = index == 0 ? nil : edges[index-1]
            let high = index < edges.count ? edges[index] : nil
            let duration: String
            switch (low,high) {
            case let (low?,high?) where low.intValue >= 1000:
                let format = FloatingPointFormatStyle<Double>.number.precision(.fractionLength(0...1))
                duration = (low.doubleValue/1000).formatted(format) + "–" + (high.doubleValue/1000).formatted(format) + " s"
            case let (low?,high?): duration = "\(low)–\(high) ms"
            case let (low?,nil): duration = "≥ \(low.intValue/1000) s"
            case let (nil,high?): duration = "< \(high) ms"
            default: continue
            }
            let caption = label(duration)
            caption.widthAnchor.constraint(equalToConstant: 86).isActive = true
            if let bar = progress(count.int64Value, max: peak, color: Self.blue) {
                content.append(Self.horizontal([caption, bar, label(count.stringValue)]))
            }
        }
        replace(latency, with: content)
    }

    private func renderOutcomes(_ data: [String: Any]) {
        let events = data["outcomes"] as? [[String: Any]] ?? []
        let categories = [("success","成功",Self.mint),("failed","失败",Self.rose),
                          ("limited","受限",Self.gold),("canceled","已取消",Self.gray)]
        let counts = categories.map { category in
            events.filter { $0["outcome"] as? String == category.0 }.reduce(Int64(0)) { $0 + Display.number($1["requests"]) }
        }
        let total = counts.reduce(0,+)
        guard total > 0 else { replace(outcomes, with: [label("这个时段暂无请求。")]); return }
        let donut = GatewayDonut(counts: counts, colors: categories.map(\.2),
            value: Display.percent(100*Double(counts[0])/Double(total)), caption: "成功率")
        let legend = zip(categories,counts).map { category, count in
            let line = paired("● " + category.1, Display.integer(count))
            (line.arrangedSubviews.first as? NSTextField)?.textColor = category.2
            line.toolTip = events.filter { $0["outcome"] as? String == category.0 }.map {
                "HTTP \(Display.number($0["status"])) · \(Display.number($0["requests"])) 条"
            }.joined(separator: "；")
            return line
        }
        replace(outcomes, with: [Self.horizontal([donut, Self.vertical(legend, spacing: 4)])])
    }

    private func renderModels() {
        let rows = snapshot["models"] as? [[String: Any]] ?? []
        guard !rows.isEmpty else { replace(models, with: [label("暂无模型用量。")]); return }
        let colors = [Self.mint, Self.blue, Self.violet, Self.gold, Self.gray]
        let tokens = rows.map { Display.number($0["tokens"]) }
        let total = tokens.reduce(0,+)
        var shares = Array(tokens.prefix(4))
        if tokens.count > 4 { shares.append(total-shares.reduce(0,+)) }
        let donut = GatewayDonut(counts: shares, colors: colors, value: Display.integer(total), caption: "总 Token")
        donut.isHidden = total == 0
        let entries: [[String]] = [["模型", "请求", "失败", "Token", "估算费用"]] + rows.map {
            [$0["model"] as? String ?? "—", Display.integer(Display.number($0["requests"])),
             Display.integer(Display.number($0["failures"])), Display.integer(Display.number($0["tokens"])),
             Display.usd(Display.number($0["cost_nanos"]))]
        }
        let grid = NSGridView(views: entries.enumerated().map { index, cells in
            cells.enumerated().map { column, text in
                GatewayStyle.label(text, color: column == 0 && index > 0 ? colors[min(index-1, colors.count-1)] : GatewayStyle.ink,
                                   size: SafeCodeType.small, weight: index == 0 ? .semibold : .regular)
            }
        })
        grid.columnSpacing = 12
        grid.rowSpacing = 8
        for column in 1..<5 { grid.column(at: column).xPlacement = .trailing }
        replace(models, with: [Self.horizontal([donut, grid])])
    }
}

private final class GatewayLineChart: NSView {
    typealias Series = (title: String, color: NSColor, values: [Double])
    private var labels: [String] = []
    private var series: [Series] = []
    private var selected: Int?

    func set(labels: [String], series: [Series]) {
        self.labels = labels
        self.series = series
        if let selected, !labels.indices.contains(selected) { self.selected = nil }
        setAccessibilityLabel(series.map(\.title).joined(separator: "、"))
        setAccessibilityHelp("用左右方向键查看各时段")
        setAccessibilityElement(true)
        setAccessibilityRole(.image)
        needsDisplay = true
    }

    override var acceptsFirstResponder: Bool { true }

    override func keyDown(with event: NSEvent) {
        guard !series.isEmpty else { super.keyDown(with:event); return }
        switch event.keyCode {
        case 123: selected = max(0, (selected ?? labels.count)-1)
        case 124: selected = min(labels.count-1, (selected ?? -1)+1)
        default: super.keyDown(with:event); return
        }
        refreshSelection()
    }

    override func mouseDown(with event: NSEvent) {
        guard !series.isEmpty else { return }
        window?.makeFirstResponder(self)
        let point = convert(event.locationInWindow, from: nil)
        selected = min(labels.count-1, max(0, Int(((point.x-52)/max(1,bounds.width-62)*Double(labels.count-1)).rounded())))
        refreshSelection()
    }

    private func refreshSelection() {
        guard let selected, labels.indices.contains(selected) else { return }
        setAccessibilityValue(labels[selected] + " · " + series.map {
            $0.title + " " + $0.values[selected].formatted(.number.precision(.fractionLength(0...1)))
        }.joined(separator:" · "))
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        let font = NSFont.systemFont(ofSize: SafeCodeType.small)
        let attrs: [NSAttributedString.Key: Any] = [.font:font,.foregroundColor:GatewayStyle.muted]
        guard !series.isEmpty else {
            let message = NSAttributedString(string:"这个时段暂无请求。",attributes:attrs)
            message.draw(at:NSPoint(x:max(0,(bounds.width-message.size().width)/2),y:bounds.midY))
            return
        }
        let maxValue = max(1, series.flatMap(\.values).max() ?? 1)
        let chart = NSRect(x:52,y:34,width:max(1,bounds.width-62),height:max(1,bounds.height-43))
        for tick in 0...3 {
            let y = chart.minY+chart.height*CGFloat(tick)/3
            GatewayStyle.line.setStroke()
            NSBezierPath.strokeLine(from:NSPoint(x:chart.minX,y:y),to:NSPoint(x:chart.maxX,y:y))
            let value = (maxValue*Double(tick)/3).formatted(.number.notation(.compactName).precision(.fractionLength(0...1)))
            NSAttributedString(string:value,attributes:attrs).draw(at:NSPoint(x:0,y:y-6))
        }
        let step = chart.width/CGFloat(max(1,labels.count-1))
        for (index,label) in [(0,labels.first ?? ""), (labels.count-1, labels.last ?? "")] where index >= 0 {
            let text = NSAttributedString(string:label,attributes:attrs)
            let x = min(bounds.width-text.size().width,chart.minX+CGFloat(index)*step)
            text.draw(at:NSPoint(x:x,y:17))
        }
        for item in series {
            let path = NSBezierPath()
            path.lineWidth = 2
            for (index,value) in item.values.enumerated() {
                let point = NSPoint(x:chart.minX+CGFloat(index)*step,y:chart.minY+chart.height*value/maxValue)
                if index == 0 { path.move(to:point) } else { path.line(to:point) }
            }
            item.color.setStroke()
            path.stroke()
        }
        if let selected, labels.indices.contains(selected) {
            let x = chart.minX+CGFloat(selected)*step
            GatewayStyle.grayLine.setStroke()
            NSBezierPath.strokeLine(from:NSPoint(x:x,y:chart.minY),to:NSPoint(x:x,y:chart.maxY))
            let text = NSAttributedString(string:accessibilityValue() as? String ?? "",attributes:attrs)
            text.draw(in:NSRect(x:0,y:0,width:bounds.width,height:16))
        }
    }
}

private final class GatewayDonut: NSView {
    private let counts: [Int64]
    private let colors: [NSColor]
    private let value: String
    private let caption: String
    init(counts: [Int64], colors: [NSColor], value: String, caption: String) {
        self.counts = counts; self.colors = colors; self.value = value; self.caption = caption
        super.init(frame:.zero)
        widthAnchor.constraint(equalToConstant:104).isActive = true
        heightAnchor.constraint(equalToConstant:104).isActive = true
        setAccessibilityElement(true)
        setAccessibilityRole(.image)
        setAccessibilityLabel(caption + " " + value)
    }
    required init?(coder:NSCoder) { fatalError("Use init(counts:colors:value:caption:)") }

    override func draw(_ dirtyRect: NSRect) {
        let center = NSPoint(x:bounds.midX,y:bounds.midY)
        let total = max(1,counts.reduce(0,+))
        var angle: CGFloat = 90
        for (count,color) in zip(counts,colors) where count > 0 {
            let next = angle-360*CGFloat(count)/CGFloat(total)
            let arc = NSBezierPath()
            arc.lineWidth = 11
            arc.appendArc(withCenter:center,radius:45,startAngle:angle,endAngle:next,clockwise:true)
            color.setStroke()
            arc.stroke()
            angle = next
        }
        for (text,size,y) in [(value,SafeCodeType.medium,bounds.midY),(caption,SafeCodeType.small,bounds.midY-17)] {
            let line = NSAttributedString(string:text,attributes:[.font:NSFont.systemFont(ofSize:size,weight:.medium),
                .foregroundColor:GatewayStyle.ink])
            line.draw(at:NSPoint(x:(bounds.width-line.size().width)/2,y:y))
        }
    }
}

private final class GatewayProgress: NSView {
    private let fraction: CGFloat
    private let fill: NSColor

    init(value: Int64, limit: Int64, fill: NSColor) {
        fraction = max(0,min(1,CGFloat(value)/CGFloat(limit)))
        self.fill = fill
        super.init(frame:.zero)
        heightAnchor.constraint(equalToConstant:6).isActive = true
        setAccessibilityElement(true)
        setAccessibilityRole(.progressIndicator)
        setAccessibilityValue("\(value) / \(limit)")
    }

    required init?(coder: NSCoder) { fatalError("Use init(value:limit:fill:)") }

    override func draw(_ dirtyRect: NSRect) {
        GatewayStyle.header.setFill()
        NSBezierPath(roundedRect:bounds,xRadius:3,yRadius:3).fill()
        guard fraction > 0 else { return }
        fill.setFill()
        let track = NSRect(x:0,y:0,width:bounds.width*fraction,height:bounds.height)
        NSBezierPath(roundedRect:track,xRadius:3,yRadius:3).fill()
    }
}
