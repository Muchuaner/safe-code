import AppKit

@MainActor
final class GatewayPanel: NSViewController, NSTableViewDataSource, NSTableViewDelegate, NSTextFieldDelegate {
    typealias Reply = (Result<Any, Error>) -> Void
    typealias Manage = (String, [String: Any], @escaping Reply) -> Void

    private let manage: Manage
    private let versionLabel = GatewayStyle.label("网关版本 —", color: GatewayStyle.muted, size: SafeCodeType.small)
    private let updateDot = NSView()
    private let table = NSTableView()
    private let scroller = NSScrollView()
    private let notice = NSTextField(wrappingLabelWithString: "正在读取网关…")
    private var tabs: [GatewayButton] = []
    private var selectedTab = 0
    private var renderedTab = -1
    private var renderedRows: [[String: Any]] = []
    private let search = NSTextField()
    private let filter = GatewaySelect()
    private let sort = GatewaySelect()
    private let requestFilter = GatewaySelect()
    private var clearKeyFilter: GatewayButton!
    private let requestCaption = NSTextField(labelWithString: "")
    private var searchBox: NSView!
    private let empty = NSTextField(labelWithString: "")
    private let totals = NSStackView()
    private var analysis: GatewayAnalysis!
    private var listToolbar: NSView!
    private var listFrame: NSView!
    private var metricTitles: [NSTextField] = []
    private var totalLabels: [NSTextField] = []
    private var metricDetails: [NSTextField] = []
    private let queueNow = NSTextField(labelWithString: "")
    private let queueWaiting = NSTextField(labelWithString: "")
    private let queueCapacity = NSProgressIndicator()
    private var queue: NSView!
    private var tableHeight: NSLayoutConstraint!
    private var keyPage = 0
    private let keyCount = NSTextField(labelWithString: "")
    private let pageNumber = NSTextField(labelWithString: "")
    private var previousPage: GatewayButton!
    private var nextPage: GatewayButton!
    private var pagination: NSView!
    private static let keysPerPage = 15
    private var toolbarButtons: [NSButton] = []
    private var reviewButton: NSButton!
    private weak var reviewLanguageSwitch: NSSwitch?
    private var quotaButton: NSButton!
    private var snapshot: [String: Any] = [:]
    private var visibleRows: [[String: Any]] = []
    private var keyHistory: [String: Any]?
    private var timer: Timer?
    private var fetching = false
    private var checkingQuota = false
    private var quotaAvailable = false
    private var busy = false
    private var currentColumns: [String] = []
    var ready: Bool { !snapshot.isEmpty && quotaAvailable }

    init(manage: @escaping Manage) {
        self.manage = manage
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("Use init(manage:)") }

    override func loadView() {
        view = NSView()
        view.wantsLayer = true
        view.layer?.backgroundColor = GatewayStyle.paper.cgColor

        scroller.drawsBackground = false
        scroller.hasVerticalScroller = true
        scroller.autohidesScrollers = true
        scroller.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(scroller)
        let page = GatewayPageView()
        scroller.documentView = page
        page.translatesAutoresizingMaskIntoConstraints = false
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 10
        root.translatesAutoresizingMaskIntoConstraints = false
        page.addSubview(root)
        NSLayoutConstraint.activate([
            scroller.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scroller.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scroller.topAnchor.constraint(equalTo: view.topAnchor),
            scroller.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            page.leadingAnchor.constraint(equalTo: scroller.contentView.leadingAnchor),
            page.topAnchor.constraint(equalTo: scroller.contentView.topAnchor),
            page.widthAnchor.constraint(equalTo: scroller.contentView.widthAnchor),
            root.leadingAnchor.constraint(equalTo: page.leadingAnchor, constant: 26),
            root.trailingAnchor.constraint(equalTo: page.trailingAnchor, constant: -26),
            root.topAnchor.constraint(equalTo: page.topAnchor, constant: 23),
            root.bottomAnchor.constraint(equalTo: page.bottomAnchor, constant: -28)
        ])

        let heading = GatewayStyle.label("网关总览", size: SafeCodeType.large, weight: .semibold)
        heading.setContentCompressionResistancePriority(.required, for: .horizontal)
        let refresh = button("刷新", #selector(refresh(_:)))
        reviewButton = button("前置审查", #selector(editPromptReview))
        let slots = button("处理槽位", #selector(editSlots))
        quotaButton = button("Claude 用量", #selector(queryQuota), kind: .quota)
        quotaButton.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        quotaButton.image = NSImage(systemSymbolName: "arrow.clockwise", accessibilityDescription: nil)
        quotaButton.setAccessibilityLabel("查询 Claude 用量")
        let update = button("更新旧 Key", #selector(updateTicket))
        let create = button("＋ 创建密钥", #selector(createKey), kind: .primary)
        toolbarButtons = [refresh, slots, update, create]
        updateDot.wantsLayer = true
        updateDot.layer?.backgroundColor = NSColor.systemYellow.cgColor
        updateDot.layer?.cornerRadius = 3.5
        updateDot.widthAnchor.constraint(equalToConstant: 7).isActive = true
        updateDot.heightAnchor.constraint(equalToConstant: 7).isActive = true
        updateDot.isHidden = true
        let version = row([versionLabel, updateDot], spacing: 2)
        version.alignment = .top
        let title = NSStackView(views: [heading, version])
        title.orientation = .vertical
        title.alignment = .leading
        title.spacing = 1
        let toolbar = row([title, NSView(), refresh, reviewButton, slots, quotaButton, update, create])
        append(toolbar, to: root)
        root.setCustomSpacing(19, after: toolbar)

        totals.alignment = .top
        totals.distribution = .fillEqually
        totals.spacing = 11
        let titles = ["启用密钥", "累计请求", "累计 Token", "累计估算费用", "执行中 / 排队"]
        for (index, title) in titles.enumerated() {
            let label = GatewayStyle.label(title, color: GatewayStyle.muted, size: SafeCodeType.small, weight: .medium)
            let value = GatewayStyle.label("—", size: SafeCodeType.large, weight: .bold)
            value.font = .monospacedDigitSystemFont(ofSize: SafeCodeType.large, weight: .bold)
            let detail = GatewayStyle.label("—", color: GatewayStyle.muted, size: SafeCodeType.small)
            let stack = NSStackView(views: [label, value, detail])
            stack.orientation = .vertical
            stack.alignment = .leading
            stack.spacing = 4
            let card = GatewayStyle.surface(stack, inset: NSEdgeInsets(top: 13, left: 15, bottom: 12, right: 15),
                                            fill: index == titles.count-1 ? GatewayStyle.wash : GatewayStyle.surface)
            totals.addArrangedSubview(card)
            metricTitles.append(label)
            totalLabels.append(value)
            metricDetails.append(detail)
        }
        append(totals, to: root)

        queueNow.font = .systemFont(ofSize: SafeCodeType.small, weight: .semibold)
        queueNow.textColor = GatewayStyle.ink
        queueWaiting.font = .systemFont(ofSize: SafeCodeType.small)
        queueWaiting.textColor = GatewayStyle.muted
        queueCapacity.isIndeterminate = false
        queueCapacity.style = .bar
        queueCapacity.controlSize = .small
        queueCapacity.widthAnchor.constraint(equalToConstant: 132).isActive = true
        queue = GatewayStyle.surface(row([queueNow, NSView(), queueCapacity, queueWaiting], spacing: 12),
              inset: NSEdgeInsets(top: 9, left: 14, bottom: 9, right: 14), radius: 8)
        queue.isHidden = true
        append(queue, to: root)

        notice.font = .systemFont(ofSize: SafeCodeType.small)
        notice.textColor = GatewayStyle.muted
        append(notice, to: root)

        tabs = ["密钥管理", "用量分析", "请求记录"].enumerated().map { index, title in
            let tab = button(title, #selector(changeTab(_:)), kind: .tab)
            tab.tag = index
            return tab
        }
        let tabStrip = NSStackView(views: [row(tabs + [NSView()], spacing: 19), GatewayStyle.rule()])
        tabStrip.orientation = .vertical
        tabStrip.spacing = 0
        tabStrip.alignment = .width
        append(tabStrip, to: root)

        search.placeholderString = "搜索备注或密钥 ID"
        search.setAccessibilityLabel("搜索备注或密钥 ID")
        search.font = .systemFont(ofSize: SafeCodeType.medium)
        search.delegate = self
        search.isBezeled = false
        search.drawsBackground = false
        searchBox = GatewayStyle.surface(search, inset: NSEdgeInsets(top: 8, left: 10, bottom: 8, right: 10),
                                             radius: 7)
        searchBox.heightAnchor.constraint(equalToConstant: 34).isActive = true
        searchBox.widthAnchor.constraint(equalToConstant: 260).isActive = true
        filter.addItems(withTitles: ["全部状态", "已启用", "已撤销", "已过期"])
        sort.addItems(withTitles: ["新建在前", "最近使用", "请求数最多", "Token 最多"])
        requestFilter.addItems(withTitles: ["全部结果", "成功", "失败", "受限", "已取消"])
        for (control, title, action) in [(filter,"密钥状态",#selector(resetKeyPage)), (sort,"密钥排序",#selector(resetKeyPage)),
                                  (requestFilter,"请求结果",#selector(updateRows))] {
            control.target = self
            control.action = action
            control.font = .systemFont(ofSize: SafeCodeType.medium)
            control.setAccessibilityLabel(title)
            control.heightAnchor.constraint(equalToConstant: 34).isActive = true
        }
        clearKeyFilter = button("显示所有密钥", #selector(clearHistory))
        requestCaption.font = .systemFont(ofSize: SafeCodeType.small)
        requestCaption.textColor = GatewayStyle.muted
        listToolbar = row([searchBox, filter, sort, requestFilter, clearKeyFilter, NSView(), requestCaption])
        append(listToolbar, to: root)
        analysis = GatewayAnalysis(changePeriod: { [weak self] in self?.loadSnapshot(background: false) },
                                   showRequests: { [weak self] in self?.selectTab(2) })
        append(analysis, to: root)

        table.dataSource = self
        table.delegate = self
        table.style = .plain
        table.backgroundColor = GatewayStyle.surface
        table.wantsLayer = true
        table.layer?.backgroundColor = GatewayStyle.surface.cgColor
        table.selectionHighlightStyle = .none
        table.columnAutoresizingStyle = .uniformColumnAutoresizingStyle
        table.allowsColumnReordering = false
        table.rowHeight = 60
        table.intercellSpacing = .zero
        table.target = self
        table.doubleAction = #selector(usageSelectedKey)
        table.headerView = NSTableHeaderView(frame: NSRect(x: 0, y: 0, width: 0, height: 36))
        let scroll = NSScrollView()
        scroll.documentView = table
        scroll.backgroundColor = GatewayStyle.surface
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = true
        scroll.autohidesScrollers = true
        tableHeight = scroll.heightAnchor.constraint(equalToConstant: 38)
        tableHeight.isActive = true
        empty.font = .systemFont(ofSize: SafeCodeType.medium)
        empty.textColor = GatewayStyle.muted
        empty.alignment = .center
        empty.heightAnchor.constraint(equalToConstant: 62).isActive = true
        let contents = NSStackView(views: [scroll, empty])
        contents.orientation = .vertical
        contents.alignment = .width
        contents.spacing = 0
        listFrame = GatewayStyle.surface(contents)
        append(listFrame, to: root)

        keyCount.font = .systemFont(ofSize: SafeCodeType.small)
        keyCount.textColor = GatewayStyle.muted
        pageNumber.font = keyCount.font
        pageNumber.textColor = GatewayStyle.muted
        previousPage = button("‹", #selector(pageKeys(_:)))
        previousPage.tag = -1
        previousPage.setAccessibilityLabel("上一页")
        nextPage = button("›", #selector(pageKeys(_:)))
        nextPage.tag = 1
        nextPage.setAccessibilityLabel("下一页")
        pagination = row([keyCount, NSView(), previousPage, pageNumber, nextPage])
        append(pagination, to: root)
        updateRows()
        updateButtons()
    }

    private func row(_ views: [NSView], spacing: CGFloat = 8) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.alignment = .centerY
        stack.spacing = spacing
        return stack
    }

    func start() {
        refresh(nil)
        queryQuota()
        timer = Timer.scheduledTimer(withTimeInterval: 4, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.view.window?.isVisible == true, self.view.window?.attachedSheet == nil else { return }
                self.loadSnapshot(background: true)
            }
        }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    private func button(_ title: String, _ action: Selector, kind: GatewayButton.Kind = .standard) -> GatewayButton {
        GatewayButton(title, kind: kind, target: self, action: action)
    }

    private func append(_ child: NSView, to parent: NSStackView) {
        parent.addArrangedSubview(child)
        child.widthAnchor.constraint(equalTo: parent.widthAnchor).isActive = true
    }

    @objc private func refresh(_ sender: Any?) { loadSnapshot(background: false) }

    private func showGatewayVersion(_ state: [String: Any]) {
        let revision = state["gatewayRevision"] as? Int ?? 0
        let available = revision > 0 && state["gatewayUpdateAvailable"] as? Bool == true
        versionLabel.stringValue = revision > 0 ? "网关版本 rev\(revision)" : "网关版本 —"
        let hint = available ? "有新版网关，可在「网关设置」中更新" : nil
        versionLabel.toolTip = hint
        versionLabel.setAccessibilityHelp(hint)
        updateDot.toolTip = hint
        updateDot.isHidden = !available
    }

    private func loadSnapshot(background: Bool) {
        guard !fetching, !busy, !checkingQuota || snapshot.isEmpty else { return }
        fetching = true
        if !background && snapshot.isEmpty { showNotice("正在读取网关…") }
        var args: [String: Any] = ["details": true, "background": background]
        if selectedTab == 1 { args["analytics"] = analysis.period }
        manage("list", args) { [weak self] result in
            guard let self else { return }
            self.fetching = false
            switch result {
            case .success(let response):
                self.snapshot = response as? [String: Any] ?? [:]
                self.showGatewayVersion(self.snapshot)
                if !background { self.notice.stringValue = "" }
                self.notice.isHidden = self.notice.stringValue.isEmpty
                self.updateTotals()
                self.updateRows()
                if self.selectedTab == 1, args["analytics"] as? String != self.analysis.period {
                    self.loadSnapshot(background: false)
                }
            case .failure(let error):
                self.showGatewayVersion([:])
                self.showNotice(error.localizedDescription)
            }
            self.updateButtons()
        }
    }

    @objc private func queryQuota() {
        guard !checkingQuota, !busy else { return }
        checkingQuota = true
        quotaButton.isEnabled = false
        quotaButton.title = "查询中…"
        manage("refresh_subscription", [:]) { [weak self] response in
            guard let self else { return }
            self.checkingQuota = false
            switch response {
            case .success(let result):
                let value = result as? [String: Any] ?? [:]
                let windows = value["windows"] as? [String: [String: Any]] ?? [:]
                let labels = [("five_hour", "5 小时"), ("seven_day", "7 天")].compactMap { name, label -> String? in
                    guard let used = (windows[name]?["used"] as? NSNumber)?.doubleValue else { return nil }
                    let remaining = max(0, min(100, 100-used))
                    return label + "剩余 " + Display.percent(remaining)
                }
                self.quotaAvailable = value["state"] as? String == "available"
                self.quotaButton.title = (["Claude 用量"] + labels).joined(separator: "  ")
                self.quotaButton.toolTip = value["reason"] as? String ?? self.quotaButton.title
                if !self.quotaAvailable { self.showNotice(self.quotaButton.toolTip ?? "用量暂不可用") }
            case .failure(let error):
                self.quotaButton.title = "Claude 用量"
                self.quotaButton.toolTip = error.localizedDescription
                self.showNotice(error.localizedDescription)
            }
            self.updateButtons()
        }
    }

    private var keys: [[String: Any]] { snapshot["keys"] as? [[String: Any]] ?? [] }
    private var selectedKey: [String: Any]? {
        guard selectedTab == 0, visibleRows.indices.contains(table.selectedRow) else { return nil }
        return visibleRows[table.selectedRow]
    }

    private func showNotice(_ message: String) {
        notice.stringValue = message
        notice.isHidden = message.isEmpty
    }

    private func updateButtons() {
        toolbarButtons.first?.isEnabled = !busy
        for item in toolbarButtons.dropFirst() { item.isEnabled = !busy && !snapshot.isEmpty }
        reviewButton.isEnabled = !busy && snapshot["prompt_review"] != nil
        quotaButton.isEnabled = !busy && !checkingQuota
    }

    private func updateTotals() {
        let stats = snapshot["stats"] as? [String: Any]
        let active = Display.number(snapshot["active_count"])
        let waiting = Display.number(snapshot["waiting"])
        let limit = snapshot["concurrency_text"] as? String ?? String(Display.number(snapshot["concurrency"]))
        let slotLabel = limit == "0" ? "不限" : limit
        let recent = stats != nil
        metricTitles[1].stringValue = recent ? "近 24 小时请求" : "累计请求"
        metricTitles[2].stringValue = recent ? "近 24 小时 Token" : "累计 Token"
        let cards = [
            Display.integer(Int64(keys.filter { keyState($0) == "已启用" }.count)),
            Display.integer(stats.map { Display.number($0["requests"]) } ?? keys.reduce(0) { $0 + Display.number($1["requests"]) }),
            Display.integer(stats.map { Display.number($0["tokens"]) } ?? keys.reduce(0) { $0 + Display.number($1["tokens"]) }),
            Display.usd(Display.number((snapshot["billing"] as? [String: Any])?["cost_nanos"])),
            "\(active) / \(waiting)"
        ]
        let details = [
            "共 \(keys.count) 把密钥",
            stats.map { "失败 \(Display.number($0["failures"])) · 受限 \(Display.number($0["limited"]))" } ?? "消息请求总计",
            stats.map { "近 1 分钟 \(Display.number($0["rpm"])) RPM · \(Display.number($0["tpm"])) TPM" } ?? "输入、输出及缓存",
            "按官方 API 标准价",
            stats?["average_ms"].map { "近 24 小时平均处理 " + Display.duration($0) } ?? "并发上限 " + slotLabel
        ]
        for (label, value) in zip(totalLabels, cards) { label.stringValue = value }
        for (label, value) in zip(metricDetails, details) { label.stringValue = value; label.toolTip = value }
        toolbarButtons[1].title = "处理槽位 " + slotLabel
        let review = snapshot["prompt_review"] as? [String: Any]
        reviewButton.title = review?["enabled"] as? Bool == true ? "前置审查 · 已开启" : "前置审查"
        queue.isHidden = false
        let queueLabel = active == 0 ? "网关空闲" : "正在处理 \(active) 个请求"
        let message = NSMutableAttributedString(string:"●  " + queueLabel,
            attributes:[.font:NSFont.systemFont(ofSize:SafeCodeType.small,weight:.semibold),.foregroundColor:GatewayStyle.ink])
        message.addAttribute(.foregroundColor,value:active == 0 ? GatewayStyle.grayLine : GatewayStyle.accent,
                             range:NSRange(location:0,length:1))
        queueNow.attributedStringValue = message
        queueWaiting.stringValue = "并发 \(active)" + (limit == "0" ? "（不限）" : "/" + slotLabel) + " · 排队 \(waiting)"
        queueCapacity.isHidden = limit == "0"
        queueCapacity.maxValue = max(1, Double(limit) ?? 1)
        queueCapacity.doubleValue = Double(active)
    }

    @objc private func changeTab(_ sender: NSButton) {
        selectTab(sender.tag)
    }

    func selectTab(_ index: Int) {
        guard selectedTab != index else { return }
        selectedTab = index
        keyHistory = nil
        updateRows()
        scrollToTop()
        if selectedTab == 1 { loadSnapshot(background: false) }
    }

    private func scrollToTop() {
        scroller.contentView.scroll(to: .zero)
        scroller.reflectScrolledClipView(scroller.contentView)
    }

    @objc private func clearHistory() {
        keyHistory = nil
        updateRows()
    }

    @objc private func resetKeyPage() {
        keyPage = 0
        updateRows()
    }

    @objc private func pageKeys(_ sender: NSButton) {
        keyPage += sender.tag
        updateRows()
    }

    func controlTextDidChange(_ obj: Notification) { resetKeyPage() }

    @objc private func updateRows() {
        let section = selectedTab
        searchBox.isHidden = section != 0
        filter.isHidden = section != 0
        sort.isHidden = section != 0
        for tab in tabs { tab.state = tab.tag == section ? .on : .off; tab.needsDisplay = true }
        requestFilter.isHidden = section != 2
        requestCaption.isHidden = section != 2
        clearKeyFilter.isHidden = section != 2 || keyHistory == nil
        pagination.isHidden = true
        listToolbar.isHidden = section == 1
        listFrame.isHidden = section == 1
        analysis.isHidden = section != 1
        if section == 1 {
            analysis.render(snapshot)
            return
        }
        if section == 0 {
            columns([("note","备注 / 密钥 ID",121),("status","状态",66),("policy","模型 / 失效时间",129),("rpm","限频",66),("requests","请求",56),
                     ("tokens","Token",86),("cost","已用 / 余额",92),("last","最近使用",104),("actions","操作",205)])
            let keyword = search.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            var matching = keys.filter { key in
                let statusMatches = filter.indexOfSelectedItem == 0 || keyState(key) == filter.titleOfSelectedItem
                return statusMatches && (keyword.isEmpty || keyName(key).localizedCaseInsensitiveContains(keyword)
                    || (key["id"] as? String ?? "").localizedCaseInsensitiveContains(keyword))
            }
            let field = ["created","last_used","requests","tokens"][sort.indexOfSelectedItem]
            matching.sort { Display.number($0[field]) > Display.number($1[field]) }
            let pages = max(1, (matching.count + Self.keysPerPage-1)/Self.keysPerPage)
            keyPage = min(max(0, keyPage), pages-1)
            visibleRows = Array(matching.dropFirst(keyPage*Self.keysPerPage).prefix(Self.keysPerPage))
            pagination.isHidden = matching.isEmpty
            keyCount.stringValue = "共 \(matching.count) 把密钥"
            pageNumber.stringValue = "\(keyPage+1) / \(pages)"
            previousPage.isEnabled = keyPage > 0
            nextPage.isEnabled = keyPage+1 < pages
        } else {
            columns([("at","时间",153),("key_id","密钥 ID",136),("model","模型",170),("outcome","结果",90),
                     ("status","HTTP",64),("wait_ms","等待",76),("duration_ms","处理耗时",89),("tokens","Token",94)])
            let outcomes = ["", "success", "failed", "limited", "canceled"]
            let outcome = outcomes[requestFilter.indexOfSelectedItem]
            let recent = (keyHistory?["recent"] ?? snapshot["recent"]) as? [[String: Any]] ?? []
            visibleRows = outcome.isEmpty ? recent : recent.filter { $0["outcome"] as? String == outcome }
            requestCaption.stringValue = keyHistory.map { "仅查看 · " + keyName($0) } ?? "最近 30 条 · 新采集的请求"
        }
        let itemHeight: CGFloat = section == 0 ? 57 : 40
        table.rowHeight = itemHeight
        tableHeight.constant = 37 + CGFloat(min(visibleRows.count, 12))*itemHeight
        if renderedTab != section || !(renderedRows as NSArray).isEqual(to: visibleRows) {
            renderedTab = section
            renderedRows = visibleRows
            table.reloadData()
        }
        empty.isHidden = snapshot.isEmpty || !visibleRows.isEmpty
        empty.stringValue = section == 0 ? (keys.isEmpty ? "还没有密钥。点击右上角创建第一把。" : "没有符合筛选条件的密钥。")
              : section == 2 ? "没有符合筛选条件的请求。" : "这个时段暂无记录。"
        updateButtons()
    }

    private func columns(_ specs: [(String,String,CGFloat)]) {
        let ids = specs.map(\.0)
        guard currentColumns != ids else { return }
        currentColumns = ids
        for column in table.tableColumns { table.removeTableColumn(column) }
        for (id,title,width) in specs {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(id))
            column.headerCell = GatewayColumnHeader(textCell: title)
            column.width = width
            column.minWidth = min(width, 60)
            table.addTableColumn(column)
        }
    }

    func tableView(_ tableView: NSTableView, rowViewForRow row: Int) -> NSTableRowView? {
        GatewayTableRow()
    }
    func numberOfRows(in tableView: NSTableView) -> Int { visibleRows.count }

    func tableView(_ tableView: NSTableView, viewFor column: NSTableColumn?, row index: Int) -> NSView? {
        guard visibleRows.indices.contains(index), let id = column?.identifier.rawValue else { return nil }
        let row = visibleRows[index]
        if id == "actions" { return actions(for: row) }
        let value: String
        switch id {
        case "note": value = (row["note"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "未填写备注"
        case "status" where selectedTab == 0: value = keyState(row)
        case "policy": value = (row["allowed_models"] as? [String]).map { "\($0.count) 个模型" } ?? "全部模型"
        case "rpm": value = Display.number(row[id]) > 0 ? "\(Display.number(row[id])) / 分" : "不限频"
        case "cost":
            let used = Display.number(row["cost_nanos"])
            value = Display.usd(used)
        case "at", "last": value = Display.date(row[id == "last" ? "last_used" : id])
        case "key_id": value = String((row[id] as? String ?? "").prefix(12))
        case "cost_nanos": value = Display.usd(Display.number(row[id]))
        case "outcome": value = ["success":"成功","failed":"失败","limited":"限流","canceled":"取消"][row[id] as? String ?? ""] ?? "—"
        case "wait_ms", "duration_ms", "average_ms", "p95_ms":
            value = Display.duration(row[id] ?? NSNull())
        default: value = row[id] as? String ?? (row[id] as? NSNumber).map { Display.integer($0.int64Value) } ?? "—"
        }
        let numeric = ["rpm","requests","tokens","cost","wait_ms","duration_ms","cost_nanos","average_ms","p95_ms"].contains(id)
        let label = GatewayStyle.label(value, size: SafeCodeType.medium, weight: id == "note" ? .semibold : .regular)
        label.toolTip = value
        if id == "last" { label.font = .systemFont(ofSize: SafeCodeType.small) }
        label.alignment = numeric ? .right : .left
        label.isSelectable = true
        let items: [NSView]
        if id == "note" {
            let identifier = row["id"] as? String ?? ""
            let detail = GatewayStyle.label(String(identifier.prefix(12)) + "…", color: GatewayStyle.muted, size: SafeCodeType.small)
            detail.font = .monospacedSystemFont(ofSize: SafeCodeType.small, weight: .regular)
            detail.toolTip = identifier
            detail.isSelectable = true
            items = [label, detail]
        } else if id == "policy" {
            label.toolTip = (row["allowed_models"] as? [String])?.joined(separator: "、") ?? value
            let expiry = (row["expires_at"] as? NSNumber).map {
                Date(timeIntervalSince1970: $0.doubleValue).formatted(.dateTime.year().month().day().hour().minute())
            } ?? "永不过期"
            let detail = GatewayStyle.label(expiry, color: GatewayStyle.muted, size: SafeCodeType.small)
            detail.toolTip = expiry
            items = [label, detail]
        } else if id == "cost" {
            let remaining = (row["budget_nanos"] as? NSNumber).map { $0.int64Value - Display.number(row["cost_nanos"]) }
            let detail = GatewayStyle.label(remaining.map { "余 " + Display.usd($0) } ?? "不限额",
                                            color: remaining.map { $0 <= 0 } == true ? GatewayStyle.danger : GatewayStyle.muted,
                                            size: SafeCodeType.small)
            detail.alignment = .right
            items = [label, detail]
        } else if ["status","outcome"].contains(id) && (selectedTab == 0 || id == "outcome") {
            let good = value == "已启用" || value == "成功"
            let bad = value == "已撤销" || value == "失败"
            label.textColor = good ? GatewayStyle.accent : bad ? GatewayStyle.danger : GatewayStyle.muted
            label.font = .systemFont(ofSize: SafeCodeType.small, weight: .semibold)
            let badge = GatewayStyle.surface(label, inset: NSEdgeInsets(top: 3, left: 7, bottom: 3, right: 7), radius: 5,
                fill: good ? GatewayStyle.wash : bad ? GatewayStyle.dangerWash : GatewayStyle.header, border: false)
            return cell(self.row([badge, NSView()], spacing: 0), inset: 7)
        } else {
            items = [label]
        }
        let columnContent = NSStackView(views: items)
        columnContent.orientation = .vertical
        columnContent.alignment = numeric ? .trailing : .leading
        columnContent.spacing = 3
        return cell(columnContent)
    }

    private func cell(_ content: NSView, inset: CGFloat = 12) -> NSView {
        let wrapper = NSView()
        wrapper.addSubview(content)
        content.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: wrapper.leadingAnchor, constant: inset),
            content.trailingAnchor.constraint(equalTo: wrapper.trailingAnchor, constant: -inset),
            content.centerYAnchor.constraint(equalTo: wrapper.centerYAnchor)
        ])
        return wrapper
    }

    private func actions(for key: [String: Any]) -> NSView {
        let detail = rowAction("用量详情", #selector(keyDetails(_:)), key: key)
        var buttons: [NSView] = [detail]
        if key["bound_device"] is String {
            buttons.append(rowAction("解绑", #selector(unbindRowKey(_:)), key: key))
        }
        if key["enabled"] as? Bool == true {
            buttons += [rowAction("编辑", #selector(editRowKey(_:)), key: key),
                        rowAction("撤销", #selector(removeRowKey(_:)), key: key, danger: true)]
        } else if snapshot["can_delete_revoked_keys"] as? Bool == true {
            buttons.append(rowAction("彻底删除", #selector(removeRowKey(_:)), key: key, danger: true))
        }
        return cell(row(buttons + [NSView()], spacing: 3))
    }

    private func rowAction(_ title: String, _ action: Selector, key: [String: Any], danger: Bool = false) -> GatewayButton {
        let control = button(title, action, kind: danger ? .dangerLink : .link)
        control.identifier = (key["id"] as? String).map(NSUserInterfaceItemIdentifier.init(rawValue:))
        control.isEnabled = !busy
        return control
    }

    private func key(for sender: NSButton) -> [String: Any]? {
        keys.first { ($0["id"] as? String) == sender.identifier?.rawValue }
    }

    @objc private func keyDetails(_ sender: NSButton) {
        if let key = key(for: sender) { showUsage(key) }
    }

    @objc private func editRowKey(_ sender: NSButton) {
        if !busy, let key = key(for: sender) { editKey(key) }
    }

    @objc private func unbindRowKey(_ sender: NSButton) {
        guard !busy, let key = key(for: sender), let id = key["id"] as? String,
              let device = key["bound_device"] as? String else { return }
        confirm("解绑设备", "解除「" + keyName(key) + "」的设备绑定？该 Key 的余额和用量记录将保留。") {
            self.mutate("unbind", ["id":id, "deviceId":device])
        }
    }

    @objc private func removeRowKey(_ sender: NSButton) {
        guard !busy, let key = key(for: sender), let id = key["id"] as? String else { return }
        if key["enabled"] as? Bool == true {
            confirm("撤销密钥", "使用该 Key 的设备将无法继续请求。") { self.mutate("revoke", ["id":id]) }
        } else if snapshot["can_delete_revoked_keys"] as? Bool == true {
            confirm("删除已撤销密钥", "删除后无法恢复此密钥的用量记录。") { self.mutate("delete", ["id":id]) }
        }
    }

    private func keyName(_ key: [String: Any]) -> String {
        let note = key["note"] as? String ?? ""
        return note.isEmpty ? String((key["id"] as? String ?? "").prefix(12)) : note
    }

    private func keyState(_ key: [String: Any]) -> String {
        if key["expired"] as? Bool == true { return "已过期" }
        return key["enabled"] as? Bool == true ? "已启用" : "已撤销"
    }

    @objc private func usageSelectedKey() {
        if let key = selectedKey { showUsage(key) }
    }

    private func showUsage(_ key: [String: Any]) {
        guard let id = key["id"] as? String else { return }
        manage("usage", ["id": id]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let data):
                let usage = data as? [String: Any] ?? [:]
                let models = usage["models"] as? [[String: Any]] ?? []
                let lines = models.map { model in
                    "\(model["model"] as? String ?? "—") · \(Display.number(model["requests"])) 次 · \(Display.number(model["failures"])) 次失败\n" +
                    "输入 \(Display.integer(Display.number(model["input"]))) · 输出 \(Display.integer(Display.number(model["output"]))) · 缓存读取 \(Display.integer(Display.number(model["cache_read"]))) · 缓存写入 \(Display.integer(Display.number(model["cache_write"])))"
                }
                guard let window = self.view.window, window.attachedSheet == nil else { return }
                let dialog = NSAlert()
                dialog.messageText = "用量详情 · " + self.keyName(key)
                dialog.informativeText = lines.isEmpty ? "此密钥暂无用量。" : lines.joined(separator: "\n\n")
                dialog.addButton(withTitle: "关闭")
                dialog.addButton(withTitle: "查看此密钥的请求")
                dialog.beginSheetModal(for: window) { response in
                    if response == .alertSecondButtonReturn {
                        self.keyHistory = key
                        self.keyHistory?["recent"] = usage["recent"]
                        self.selectedTab = 2
                        self.requestFilter.selectItem(at: 0)
                        self.updateRows()
                        self.scrollToTop()
                    }
                }
            case .failure(let error): self.showNotice(error.localizedDescription)
            }
        }
    }

    @objc func createKey() { editKey(nil) }

    private func editKey(_ key: [String: Any]?) {
        let note = field(key?["note"] as? String ?? "", placeholder: "可留空")
        let rpm = field(key.map { String(Display.number($0["rpm"])) } ?? "0", placeholder: "0 表示不限")
        let budgetValue = (key?["budget_nanos"] as? NSNumber).map { String(format: "%.2f", $0.doubleValue/1_000_000_000) } ?? ""
        let budget = field(budgetValue, placeholder: "留空不限额")
        let activation = NSSecureTextField()
        let needsActivation = key == nil && snapshot["activation_required"] as? Bool == true
        let policySupported = snapshot["key_policy_supported"] as? Bool == true
        let modelPicker = GatewayKeyModelPicker(selected: key?["allowed_models"] as? [String]) { [weak self] reply in
            self?.manage("model_catalog", [:], reply)
        }
        let expiry = NSDatePicker()
        expiry.datePickerStyle = .textFieldAndStepper
        expiry.datePickerElements = [.yearMonthDay, .hourMinute]
        expiry.setAccessibilityLabel("到期日期与时间")
        let savedExpiry = (key?["expires_at"] as? NSNumber)?.int64Value
        expiry.dateValue = savedExpiry.map { Date(timeIntervalSince1970: Double($0)) } ?? Date().addingTimeInterval(7 * 86400)
        let never = NSButton(checkboxWithTitle: "永不过期", target: nil, action: nil)
        never.state = savedExpiry == nil ? .on : .off
        let expiryControls = row([never, expiry], spacing: 6)
        expiry.isHidden = never.state == .on
        let expiryToggle = GatewayKeyExpiryToggle(button: never, datePicker: expiry)
        modelPicker.button.isEnabled = policySupported
        never.isEnabled = policySupported
        var fields: [(String, NSView)] = [("备注",note),("限频（次 / 分）",rpm),("累计额度（美元）",budget),
                                          ("模型管理", modelPicker.button), ("失效时间", expiryControls)]
        let devicePolicy = NSPopUpButton()
        devicePolicy.addItems(withTitles:["Safe Code 桌面接入","普通 API"])
        devicePolicy.selectItem(at:key == nil || key?["device_access"] as? Bool == true ? 0 : 1)
        let devicePolicySupported = snapshot["device_policy_supported"] as? Bool == true
        if devicePolicySupported { fields.append(("接入用途", devicePolicy)) }
        if needsActivation { fields.append(("激活码", activation)) }
        let message = !policySupported ? "请先在「网关设置」升级 VPS 网关，以设置模型权限和失效时间。" :
            snapshot["device_service_problem"] as? String != nil ?
                "设备服务尚未就绪。请在网关设置中运行故障修复，或先选择普通 API。" : ""
        form(key == nil ? "创建密钥":"编辑密钥", fields,message:message) { [expiryToggle] in
            let maxRate = Int(rpm.stringValue.trimmingCharacters(in: .whitespaces))
            let rawBudget = budget.stringValue.trimmingCharacters(in: .whitespaces)
            let cents = Decimal(string:rawBudget,locale:Locale(identifier:"en_US_POSIX")).map { $0*100 }
            guard let maxRate, (0...600).contains(maxRate), rawBudget.isEmpty ||
                    (cents.map { let n=NSDecimalNumber(decimal:$0); return $0>=0 && $0<=100_000_000 && Decimal(n.intValue)==$0 } == true) else {
                self.showNotice("请输入有效限频与累计额度，金额最多保留两位小数。")
                return
            }
            var args: [String: Any] = ["note":note.stringValue.trimmingCharacters(in:.whitespacesAndNewlines),
                                       "rpm":maxRate]
            args["budget_cents"] = cents.map { NSDecimalNumber(decimal:$0).intValue as Any } ?? NSNull()
            if policySupported {
                let models = modelPicker.allowedModels
                guard models?.isEmpty != true else { self.showNotice("请至少选择一个允许使用的模型。"); return }
                args["allowed_models"] = models as Any? ?? NSNull()
                let seconds = expiryToggle.expiration
                guard seconds == nil || seconds == savedExpiry || Double(seconds ?? 0) > Date().timeIntervalSince1970 else {
                    self.showNotice("请选择晚于现在的失效时间。")
                    return
                }
                args["expires_at"] = seconds as Any? ?? NSNull()
            }
            let allowDevice = devicePolicy.indexOfSelectedItem == 0
            if devicePolicySupported && (key == nil || allowDevice != (key?["device_access"] as? Bool == true)) {
                args["device_access"] = allowDevice
            }
            if needsActivation { args["activation_code"] = activation.stringValue }
            if let id = key?["id"] as? String { args["id"] = id }
            self.mutate(key == nil ? "issue":"update", args)
        }
    }

    @objc private func editPromptReview() {
        guard let settings = snapshot["prompt_review"] as? [String: Any] else { return }
        let selection = NSPopUpButton()
        selection.addItems(withTitles: ["内置模型 · Sonnet", "内置模型 · Opus", "DeepSeek Flash"])
        let model = settings["model"] as? String ?? "sonnet"
        selection.selectItem(at: settings["provider"] as? String == "deepseek" ? 2 : model == "opus" ? 1 : 0)
        let key = NSSecureTextField()
        let hasKey = settings["has_deepseek_key"] as? Bool == true
        key.placeholderString = hasKey ? "已保存，粘贴可替换" : "选择 DeepSeek 时填写"
        let enabled = NSSwitch()
        enabled.state = settings["enabled"] as? Bool == true ? .on : .off
        let language = NSSwitch()
        language.state = settings["language_replacement"] as? Bool == true ? .on : .off
        reviewLanguageSwitch = language
        enabled.target = self
        enabled.action = #selector(updateReviewLanguage(_:))
        updateReviewLanguage(enabled)
        let languageSupported = settings["language_replacement"] is Bool
        var fields: [(String, NSView)] = [("审查模型", selection), ("DeepSeek API Key", key), ("安全审查", enabled)]
        if languageSupported { fields.append(("语言替换", language)) }
        let info = "开启语言替换后，上方模型会将通过安全审查的提示词翻译成英文。选择 DeepSeek 时，本轮文字会发送给 DeepSeek。"
        form("前置审查", fields, message: info) {
            let deepseek = selection.indexOfSelectedItem == 2
            let secret = key.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            if enabled.state == .on && deepseek && secret.isEmpty && !hasKey {
                self.showNotice("请先填写 DeepSeek API Key，再开始审查。")
                return
            }
            var args: [String: Any] = ["enabled": enabled.state == .on, "provider": deepseek ? "deepseek" : "builtin",
                                       "model": deepseek ? model : selection.indexOfSelectedItem == 1 ? "opus" : "sonnet"]
            if languageSupported { args["language_replacement"] = language.state == .on }
            if deepseek && !secret.isEmpty { args["deepseek_key"] = secret }
            self.mutate("set_prompt_review", args)
        }
    }

    @objc private func updateReviewLanguage(_ sender: NSSwitch) {
        reviewLanguageSwitch?.isEnabled = sender.state == .on
        if sender.state == .off { reviewLanguageSwitch?.state = .off }
    }

    @objc private func editSlots() {
        let value = field(String(Display.number(snapshot["concurrency"])), placeholder: "0 表示不限")
        form("处理槽位", [("并发数",value)]) {
            let text=value.stringValue.trimmingCharacters(in:.whitespaces)
            guard !text.isEmpty, text.allSatisfy({$0 >= "0" && $0 <= "9"}) else {
                self.showNotice("处理槽位必须是非负整数。")
                return
            }
            self.mutate("set_concurrency", ["concurrency":text])
        }
    }

    @objc private func updateTicket() {
        let ticket = NSSecureTextField()
        form("更新旧 Key", [("旧 Safe Code Key",ticket)]) {
            let full = ticket.stringValue.trimmingCharacters(in:.whitespacesAndNewlines)
            if !full.isEmpty { self.mutate("refresh_ticket",["ticket":full]) }
        }
    }

    private func mutate(_ action: String, _ args: [String: Any]) {
        guard !busy else { return }
        busy = true
        updateButtons()
        manage(action,args) { [weak self] result in
            guard let self else { return }
            self.busy = false
            self.updateButtons()
            switch result {
            case .success(let value):
                if let ticket = (value as? [String: Any])?["ticket"] as? String { self.showTicket(ticket) }
                self.loadSnapshot(background: false)
            case .failure(let error): self.showNotice(error.localizedDescription)
            }
        }
    }

    private func showTicket(_ ticket: String) {
        guard let window = view.window else { return }
        let dialog = NSAlert()
        dialog.messageText = "保存 Safe Code Key"
        dialog.informativeText = "请复制后回到 Safe Code 主界面的「API Key 接入」粘贴。"
        let content = field(ticket, placeholder: "")
        content.isEditable = false
        content.frame = NSRect(x:0,y:0,width:410,height:26)
        dialog.accessoryView = content
        dialog.addButton(withTitle:"复制 Key")
        dialog.addButton(withTitle:"关闭")
        dialog.beginSheetModal(for:window) { result in
            if result == .alertFirstButtonReturn {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(ticket, forType:.string)
            }
        }
    }

    private func field(_ value: String, placeholder: String) -> NSTextField {
        let field = NSTextField(string: value)
        field.font = .systemFont(ofSize: SafeCodeType.medium)
        field.placeholderString = placeholder
        return field
    }

    private func form(_ title: String, _ fields: [(String,NSView)], message: String = "", save: @escaping () -> Void) {
        guard let window = view.window else { return }
        let dialog = NSAlert()
        dialog.messageText = title
        dialog.informativeText = message
        let grid = NSGridView(views: fields.map { [GatewayStyle.label($0.0),$0.1] })
        grid.columnSpacing = 12
        grid.rowSpacing = 9
        for (_,field) in fields {
            (field as? NSControl)?.font = .systemFont(ofSize: SafeCodeType.medium)
            field.widthAnchor.constraint(greaterThanOrEqualToConstant:240).isActive = true
        }
        grid.frame.size = grid.fittingSize
        dialog.accessoryView = grid
        dialog.addButton(withTitle:"保存")
        dialog.addButton(withTitle:"取消")
        dialog.beginSheetModal(for:window) { result in if result == .alertFirstButtonReturn { save() } }
        (fields.first?.1 as? NSTextField)?.selectText(nil)
    }

    private func confirm(_ title: String, _ message: String, action: @escaping () -> Void) {
        guard let window = view.window else { return }
        let dialog = NSAlert()
        dialog.messageText = title
        dialog.informativeText = message
        dialog.addButton(withTitle:"取消")
        dialog.addButton(withTitle:"确认")
        dialog.beginSheetModal(for: window) { result in if result == .alertSecondButtonReturn { action() } }
    }
}

@MainActor
private final class GatewayKeyExpiryToggle: NSObject {
    private let button: NSButton
    private let datePicker: NSDatePicker

    init(button: NSButton, datePicker: NSDatePicker) {
        self.button = button
        self.datePicker = datePicker
        super.init()
        button.target = self
        button.action = #selector(update)
        datePicker.toolTip = "按本机时区显示，到期后停止接受新请求。"
    }

    var expiration: Int64? {
        button.state == .on ? nil : Int64(datePicker.dateValue.timeIntervalSince1970)
    }

    @objc private func update() {
        datePicker.isHidden = button.state == .on
    }
}

@MainActor
private final class GatewayKeyModelPicker: NSObject {
    let button = NSButton(title: "全部模型", target: nil, action: nil)
    private let mode = NSPopUpButton()
    private let popover = NSPopover()
    private let scroll = NSScrollView()
    private let document = GatewayPageView()
    private let options = NSStackView()
    private let status = NSTextField(wrappingLabelWithString: "")
    private let help = NSTextField(wrappingLabelWithString: "「Default」「Best」等自动选项由 CloudCLI 决定实际使用的模型。")
    private let retry = NSButton(title: "重试", target: nil, action: nil)
    private let load: (@escaping GatewayPanel.Reply) -> Void
    private var selected: Set<String>
    private var catalog: [[String: String]] = []
    private var loading = false

    var allowedModels: [String]? { mode.indexOfSelectedItem == 0 ? nil : selected.sorted() }

    init(selected: [String]?, load: @escaping (@escaping GatewayPanel.Reply) -> Void) {
        self.selected = Set(selected ?? [])
        self.load = load
        super.init()
        mode.addItems(withTitles: ["全部模型", "仅允许所选模型"])
        mode.selectItem(at: selected == nil ? 0 : 1)
        mode.target = self
        mode.action = #selector(changeMode)
        mode.setAccessibilityLabel("模型权限")
        button.target = self
        button.action = #selector(open)
        button.setAccessibilityLabel("模型管理")
        retry.target = self
        retry.action = #selector(loadCatalog)
        retry.isHidden = true
        options.orientation = .vertical
        options.alignment = .leading
        options.spacing = 5
        document.addSubview(options)
        scroll.documentView = document
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.borderType = .bezelBorder
        for label in [status, help] {
            label.font = .systemFont(ofSize: SafeCodeType.small)
            label.textColor = GatewayStyle.muted
        }
        let controller = NSViewController()
        controller.view = NSView(frame: NSRect(x: 0, y: 0, width: 350, height: 300))
        controller.view.wantsLayer = true
        controller.view.layer?.backgroundColor = GatewayStyle.surface.cgColor
        let content = NSStackView(views: [mode, status, retry, scroll, help])
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 8
        content.translatesAutoresizingMaskIntoConstraints = false
        controller.view.addSubview(content)
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: controller.view.leadingAnchor, constant: 14),
            content.trailingAnchor.constraint(equalTo: controller.view.trailingAnchor, constant: -14),
            content.topAnchor.constraint(equalTo: controller.view.topAnchor, constant: 14),
            content.bottomAnchor.constraint(lessThanOrEqualTo: controller.view.bottomAnchor, constant: -14),
            scroll.widthAnchor.constraint(equalTo: content.widthAnchor),
            scroll.heightAnchor.constraint(equalToConstant: 154),
            help.widthAnchor.constraint(equalTo: content.widthAnchor),
            status.widthAnchor.constraint(equalTo: content.widthAnchor)
        ])
        popover.contentViewController = controller
        popover.behavior = .transient
        updateSummary()
    }

    @objc private func open() {
        changeMode()
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .maxY)
    }

    @objc private func changeMode() {
        let limited = allowedModels != nil
        scroll.isHidden = !limited
        help.isHidden = !limited
        if !limited {
            status.isHidden = true
            retry.isHidden = true
        } else if catalog.isEmpty {
            loadCatalog()
        } else {
            renderChoices()
        }
        popover.contentSize = NSSize(width: 350, height: limited ? 300 : 58)
        updateSummary()
    }

    @objc private func loadCatalog() {
        guard !loading else { return }
        loading = true
        status.stringValue = "正在读取 CloudCLI 模型…"
        status.isHidden = false
        retry.isHidden = true
        renderChoices()
        load { [weak self] result in
            guard let self else { return }
            self.loading = false
            guard self.allowedModels != nil else { return }
            switch result {
            case .success(let value):
                self.catalog = (value as? [String: [[String: String]]])?["models"] ?? []
                self.status.stringValue = self.catalog.isEmpty ? "CloudCLI 当前没有返回可用模型。" : ""
            case .failure(let error):
                self.status.stringValue = "无法读取模型。" + error.localizedDescription
            }
            self.status.isHidden = !self.catalog.isEmpty
            self.retry.isHidden = !self.catalog.isEmpty
            self.status.toolTip = self.status.stringValue
            self.renderChoices()
        }
    }

    private func renderChoices() {
        for option in options.arrangedSubviews { option.removeFromSuperview() }
        let known = Set(catalog.compactMap { $0["id"] })
        let entries = catalog + selected.subtracting(known).sorted().map { ["id": $0, "name": $0] }
        for entry in entries {
            guard let id = entry["id"] else { continue }
            let name = entry["name"] ?? id
            let option = NSButton(checkboxWithTitle: name == id ? id : "\(name) · \(id)", target: self, action: #selector(toggle(_:)))
            option.font = .systemFont(ofSize: SafeCodeType.small)
            option.identifier = NSUserInterfaceItemIdentifier(id)
            option.state = selected.contains(id) ? .on : .off
            option.toolTip = option.title
            option.cell?.lineBreakMode = .byTruncatingTail
            option.widthAnchor.constraint(equalToConstant: 300).isActive = true
            options.addArrangedSubview(option)
        }
        options.edgeInsets = NSEdgeInsets(top: 6, left: 6, bottom: 6, right: 6)
        let size = options.fittingSize
        options.frame = NSRect(origin: .zero, size: size)
        document.frame.size = NSSize(width: size.width, height: max(154, size.height))
        scroll.contentView.scroll(to: .zero)
        scroll.reflectScrolledClipView(scroll.contentView)
    }

    @objc private func toggle(_ option: NSButton) {
        guard let id = option.identifier?.rawValue else { return }
        if option.state == .on { selected.insert(id) } else { selected.remove(id) }
        updateSummary()
    }

    private func updateSummary() {
        button.title = allowedModels.map { $0.isEmpty ? "选择模型…" : "仅允许 \($0.count) 个模型" } ?? "全部模型"
    }
}

enum Display {
    static func number(_ value: Any?) -> Int64 { (value as? NSNumber)?.int64Value ?? 0 }
    static func integer(_ value: Int64) -> String { value.formatted() }
    static func usd(_ nanos: Int64) -> String { "$"+String(format:"%.4f",Double(nanos)/1_000_000_000) }
    static func percent(_ value: Double) -> String {
        (value/100).formatted(.percent.precision(.fractionLength(0...1)))
    }
    static func duration(_ value: Any) -> String {
        guard let value = value as? NSNumber else { return "—" }
        return value.doubleValue < 1000 ? String(format: "%.0f ms", value.doubleValue) : String(format: "%.1f s", value.doubleValue/1000)
    }
    static func date(_ value: Any?) -> String {
        guard let value = value as? NSNumber else { return "—" }
        return Date(timeIntervalSince1970:value.doubleValue).formatted(.dateTime.month(.twoDigits).day(.twoDigits).hour(.twoDigits(amPM: .omitted)).minute())
    }
}
