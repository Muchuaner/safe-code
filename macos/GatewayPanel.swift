import AppKit

@MainActor
final class GatewayPanel: NSViewController, NSTableViewDataSource, NSTableViewDelegate, NSSearchFieldDelegate {
    typealias Reply = (Result<Any, Error>) -> Void
    typealias Manage = (String, [String: Any], @escaping Reply) -> Void

    private let manage: Manage
    private let table = NSTableView()
    private let notice = NSTextField(wrappingLabelWithString: "正在读取网关…")
    private let tabs = NSSegmentedControl(labels: ["密钥管理", "用量分析", "请求记录"], trackingMode: .selectOne, target: nil, action: nil)
    private let search = NSSearchField()
    private let filter = NSPopUpButton()
    private let sort = NSPopUpButton()
    private let periods = NSPopUpButton()
    private let analyticsMode = NSPopUpButton()
    private let keyActions = NSPopUpButton()
    private let totals = NSStackView()
    private let trend = GatewayTrendView()
    private var totalLabels: [NSTextField] = []
    private var toolbarButtons: [NSButton] = []
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
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 14
        root.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 22),
            root.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -22),
            root.topAnchor.constraint(equalTo: view.topAnchor, constant: 18),
            root.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -18)
        ])

        let heading = NSTextField(labelWithString: "网关总览")
        heading.font = .systemFont(ofSize: 22, weight: .semibold)
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let refresh = button("刷新", #selector(refresh(_:)))
        let slots = button("处理槽位", #selector(editSlots))
        quotaButton = button("Claude 用量", #selector(queryQuota))
        quotaButton.image = NSImage(systemSymbolName: "arrow.clockwise", accessibilityDescription: "查询用量")
        quotaButton.imagePosition = .imageTrailing
        quotaButton.setAccessibilityLabel("查询 Claude 用量")
        let update = button("更新旧 Key", #selector(updateTicket))
        let create = button("创建密钥", #selector(createKey))
        toolbarButtons = [refresh, slots, update, create]
        let toolbar = NSStackView(views: [heading, spacer, refresh, slots, quotaButton, update, create])
        toolbar.alignment = .centerY
        toolbar.spacing = 8
        for item in toolbarButtons + [quotaButton!] { item.heightAnchor.constraint(equalToConstant: 28).isActive = true }
        append(toolbar, to: root)

        totals.distribution = .fillEqually
        totals.spacing = 16
        for title in ["启用密钥", "累计请求", "累计 Token", "累计估算费用", "执行中 / 排队"] {
            let label = NSTextField(labelWithString: title)
            label.textColor = .secondaryLabelColor
            let value = NSTextField(labelWithString: "—")
            value.font = .monospacedDigitSystemFont(ofSize: 21, weight: .semibold)
            let stack = NSStackView(views: [label, value])
            stack.orientation = .vertical
            stack.alignment = .leading
            stack.spacing = 5
            totals.addArrangedSubview(stack)
            totalLabels.append(value)
        }
        append(totals, to: root)
        notice.textColor = .secondaryLabelColor
        append(notice, to: root)

        tabs.selectedSegment = 0
        tabs.target = self
        tabs.action = #selector(changeTab)
        root.addArrangedSubview(tabs)

        search.placeholderString = "搜索备注或密钥 ID"
        search.delegate = self
        search.sendsSearchStringImmediately = true
        search.widthAnchor.constraint(greaterThanOrEqualToConstant: 190).isActive = true
        filter.addItems(withTitles: ["全部状态", "已启用", "已撤销"])
        sort.addItems(withTitles: ["新建在前", "最近使用", "请求最多"])
        periods.addItems(withTitles: ["近 1 小时", "近 24 小时", "近 7 天"])
        periods.selectItem(at: 1)
        analyticsMode.addItems(withTitles: ["请求趋势", "按模型", "请求结果", "处理耗时"])
        keyActions.addItems(withTitles: ["密钥操作", "编辑", "用量详情", "查看请求", "撤销", "删除"])
        for (control, action) in [(filter,#selector(updateRows)), (sort,#selector(updateRows)),
                                  (analyticsMode,#selector(updateRows)), (periods,#selector(changePeriod)),
                                  (keyActions,#selector(useKeyAction))] {
            control.target = self
            control.action = action
        }
        let filters = NSStackView(views: [search, filter, sort, keyActions, periods, analyticsMode])
        filters.spacing = 8
        root.addArrangedSubview(filters)
        append(trend, to: root)
        trend.heightAnchor.constraint(equalToConstant: 90).isActive = true

        table.dataSource = self
        table.delegate = self
        table.usesAlternatingRowBackgroundColors = true
        table.style = .inset
        table.rowHeight = 28
        table.target = self
        table.doubleAction = #selector(usageSelectedKey)
        let scroll = NSScrollView()
        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = true
        scroll.borderType = .lineBorder
        scroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        append(scroll, to: root)
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 100).isActive = true
        updateRows()
        updateButtons()
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

    private func button(_ title: String, _ action: Selector) -> NSButton {
        let view = NSButton(title: title, target: self, action: action)
        view.bezelStyle = .rounded
        view.controlSize = .regular
        return view
    }

    private func append(_ child: NSView, to parent: NSStackView) {
        parent.addArrangedSubview(child)
        child.widthAnchor.constraint(equalTo: parent.widthAnchor).isActive = true
    }

    @objc private func refresh(_ sender: Any?) { loadSnapshot(background: false) }

    private func loadSnapshot(background: Bool) {
        guard !fetching, !busy, !checkingQuota || snapshot.isEmpty else { return }
        fetching = true
        if !background { notice.stringValue = "正在读取网关…" }
        var args: [String: Any] = ["details": true, "background": background]
        if tabs.selectedSegment == 1 { args["analytics"] = ["1h", "24h", "7d"][periods.indexOfSelectedItem] }
        manage("list", args) { [weak self] result in
            guard let self else { return }
            self.fetching = false
            switch result {
            case .success(let response):
                self.snapshot = response as? [String: Any] ?? [:]
                self.notice.stringValue = self.keyHistory.map { "请求记录 · " + self.keyName($0) } ?? ""
                self.updateTotals()
                self.updateRows()
            case .failure(let error):
                self.notice.stringValue = error.localizedDescription
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
                    guard let used = windows[name]?["used"] as? Double else { return nil }
                    return label + " " + String(format: "%.1f%%", max(0, min(100, 100-used)))
                }
                self.quotaAvailable = value["state"] as? String == "available"
                self.quotaButton.title = labels.isEmpty ? "Claude 用量" : labels.joined(separator: " · ")
                self.quotaButton.toolTip = value["reason"] as? String ?? self.quotaButton.title
                if !self.quotaAvailable { self.notice.stringValue = self.quotaButton.toolTip ?? "用量暂不可用" }
            case .failure(let error):
                self.quotaButton.title = "Claude 用量"
                self.quotaButton.toolTip = error.localizedDescription
                self.notice.stringValue = error.localizedDescription
            }
            self.updateButtons()
        }
    }

    private var keys: [[String: Any]] { snapshot["keys"] as? [[String: Any]] ?? [] }
    private var selectedKey: [String: Any]? {
        guard tabs.selectedSegment == 0, visibleRows.indices.contains(table.selectedRow) else { return nil }
        return visibleRows[table.selectedRow]
    }

    private func updateButtons() {
        toolbarButtons.first?.isEnabled = !busy
        for item in toolbarButtons.dropFirst() { item.isEnabled = !busy && !snapshot.isEmpty }
        quotaButton.isEnabled = !busy && !checkingQuota
        keyActions.isEnabled = !busy && selectedKey != nil
    }

    private func updateTotals() {
        let cards = [
            Display.integer(Int64(keys.filter { $0["enabled"] as? Bool == true }.count)),
            Display.integer(keys.reduce(0) { $0 + Display.number($1["requests"]) }),
            Display.integer(keys.reduce(0) { $0 + Display.number($1["tokens"]) }),
            Display.usd(Display.number((snapshot["billing"] as? [String: Any])?["cost_nanos"])),
            "\(Display.number(snapshot["active_count"])) / \(Display.number(snapshot["waiting"]))"
        ]
        for (label, value) in zip(totalLabels, cards) { label.stringValue = value }
        toolbarButtons[1].title = "处理槽位 " + (Display.number(snapshot["concurrency"]) == 0 ? "不限" : String(Display.number(snapshot["concurrency"])))
    }

    @objc private func changeTab() {
        keyHistory = nil
        updateRows()
        loadSnapshot(background: false)
    }

    @objc private func changePeriod() { loadSnapshot(background: false) }

    func controlTextDidChange(_ obj: Notification) { updateRows() }

    @objc private func updateRows() {
        let keyID = selectedKey?["id"] as? String
        let section = tabs.selectedSegment
        search.isHidden = section != 0
        filter.isHidden = section != 0
        sort.isHidden = section != 0
        keyActions.isHidden = section != 0
        periods.isHidden = section != 1
        analyticsMode.isHidden = section != 1
        trend.isHidden = section != 1 || analyticsMode.indexOfSelectedItem != 0
        if section == 0 {
            columns([("note","备注 / 密钥 ID",220),("status","状态",78),("rpm","限频",84),("requests","请求",80),
                     ("tokens","Token",99),("cost","已用 / 余额",135),("last","最近使用",145)])
            let keyword = search.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            visibleRows = keys.filter { key in
                let active = key["enabled"] as? Bool == true
                let statusMatches = filter.indexOfSelectedItem == 0 || active == (filter.indexOfSelectedItem == 1)
                return statusMatches && (keyword.isEmpty || keyName(key).localizedCaseInsensitiveContains(keyword)
                    || (key["id"] as? String ?? "").localizedCaseInsensitiveContains(keyword))
            }
            if sort.indexOfSelectedItem > 0 {
                let field = sort.indexOfSelectedItem == 1 ? "last_used" : "requests"
                visibleRows.sort { Display.number($0[field]) > Display.number($1[field]) }
            }
        } else if section == 1 {
            let analytics = snapshot["analytics"] as? [String: Any] ?? [:]
            switch analyticsMode.indexOfSelectedItem {
            case 1:
                columns([("model","模型",185),("requests","请求",92),("tokens","Token",115),("failures","失败",84),("cost_nanos","估算费用",130)])
                visibleRows = snapshot["models"] as? [[String: Any]] ?? []
            case 2:
                columns([("outcome","结果",155),("status","HTTP",82),("requests","次数",110)])
                visibleRows = analytics["outcomes"] as? [[String: Any]] ?? []
            case 3:
                columns([("metric","指标",170),("samples","样本",80),("average_ms","平均耗时",100),("p95_ms","P95",100)])
                let latency = analytics["latency"] as? [String: [String: Any]] ?? [:]
                visibleRows = [("wait_ms","排队"),("duration_ms","处理")].compactMap { id, label in
                    guard var metrics = latency[id] else { return nil }
                    metrics["metric"] = label
                    return metrics
                }
            default:
                columns([("at","时间",160),("requests","请求",87),("tokens","Token",115),("failed","失败",85),("limited","限流",85),("canceled","取消",85)])
                let start = Display.number(analytics["start"])
                let step = Display.number(analytics["step"])
                visibleRows = (analytics["timeline"] as? [[String: Any]] ?? []).map { row in
                    var row = row
                    row["at"] = start + Display.number(row["bucket"])*step
                    return row
                }
                trend.counts = Dictionary(uniqueKeysWithValues: visibleRows.map { (Int(Display.number($0["bucket"])), Display.number($0["requests"])) })
                trend.bucketCount = Int(Display.number(analytics["count"]))
            }
        } else {
            columns([("at","时间",153),("key_id","密钥 ID",136),("model","模型",170),("outcome","结果",90),
                     ("status","HTTP",55),("wait_ms","排队",76),("duration_ms","处理",76),("tokens","Token",94)])
            visibleRows = (keyHistory?["recent"] ?? snapshot["recent"]) as? [[String: Any]] ?? []
        }
        table.reloadData()
        if let keyID, let index = visibleRows.firstIndex(where: { $0["id"] as? String == keyID }) {
            table.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false)
        }
        updateButtons()
    }

    private func columns(_ specs: [(String,String,CGFloat)]) {
        let ids = specs.map(\.0)
        guard currentColumns != ids else { return }
        currentColumns = ids
        for column in table.tableColumns { table.removeTableColumn(column) }
        for (id,title,width) in specs {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(id))
            column.title = title
            column.width = width
            column.minWidth = min(width, 60)
            table.addTableColumn(column)
        }
    }

    func tableViewSelectionDidChange(_ notification: Notification) { updateButtons() }
    func numberOfRows(in tableView: NSTableView) -> Int { visibleRows.count }

    func tableView(_ tableView: NSTableView, viewFor column: NSTableColumn?, row index: Int) -> NSView? {
        guard visibleRows.indices.contains(index), let id = column?.identifier.rawValue else { return nil }
        let row = visibleRows[index]
        let value: String
        switch id {
        case "note": value = keyName(row)
        case "status" where tabs.selectedSegment == 0: value = row["enabled"] as? Bool == true ? "已启用" : "已撤销"
        case "rpm": value = Display.number(row[id]) > 0 ? "\(Display.number(row[id])) / 分" : "不限"
        case "cost":
            let used = Display.number(row["cost_nanos"])
            let remaining = (row["budget_nanos"] as? NSNumber).map { Display.usd(max(0, $0.int64Value-used)) } ?? "不限额"
            value = Display.usd(used) + " / " + remaining
        case "at", "last": value = Display.date(row[id == "last" ? "last_used" : id])
        case "key_id": value = String((row[id] as? String ?? "").prefix(12))
        case "cost_nanos": value = Display.usd(Display.number(row[id]))
        case "outcome": value = ["success":"成功","failed":"失败","limited":"限流","canceled":"取消"][row[id] as? String ?? ""] ?? "—"
        case "wait_ms", "duration_ms", "average_ms", "p95_ms":
            value = (row[id] as? NSNumber).map { String(format: "%.0f ms", $0.doubleValue) } ?? "—"
        default: value = row[id] as? String ?? (row[id] as? NSNumber).map { Display.integer($0.int64Value) } ?? "—"
        }
        let cell = NSTextField(labelWithString: value)
        cell.lineBreakMode = .byTruncatingTail
        cell.toolTip = id == "note" ? row["id"] as? String : value
        cell.isSelectable = true
        return cell
    }

    private func keyName(_ key: [String: Any]) -> String {
        let note = key["note"] as? String ?? ""
        return note.isEmpty ? String((key["id"] as? String ?? "").prefix(12)) : note
    }

    @objc private func useKeyAction() {
        let action = keyActions.indexOfSelectedItem
        keyActions.selectItem(at: 0)
        guard let key = selectedKey, let id = key["id"] as? String else { return }
        switch action {
        case 1: editKey(key)
        case 2: usageSelectedKey()
        case 3:
            manage("usage", ["id":id]) { [weak self] response in
                guard let self else { return }
                if case .success(let value) = response {
                    self.keyHistory = key
                    self.keyHistory?["recent"] = (value as? [String: Any])?["recent"]
                    self.tabs.selectedSegment = 2
                    self.notice.stringValue = "请求记录 · " + self.keyName(key)
                    self.updateRows()
                } else if case .failure(let error) = response { self.notice.stringValue = error.localizedDescription }
            }
        case 4 where key["enabled"] as? Bool == true:
            confirm("撤销密钥", "使用该 Key 的设备将无法继续请求。") { self.mutate("revoke", ["id":id]) }
        case 5 where key["enabled"] as? Bool == false:
            confirm("删除已撤销密钥", "删除后无法恢复此密钥的用量记录。") { self.mutate("delete", ["id":id]) }
        case 4: notice.stringValue = "此密钥已撤销。"
        case 5: notice.stringValue = "请先撤销该密钥。"
        default: break
        }
    }

    @objc private func usageSelectedKey() {
        guard let key = selectedKey, let id = key["id"] as? String else { return }
        manage("usage", ["id": id]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let data):
                let models = (data as? [String: Any])?["models"] as? [[String: Any]] ?? []
                let lines = models.map { model in
                    "\(model["model"] as? String ?? "—") · \(Display.number(model["requests"])) 次 · \(Display.number(model["failures"])) 次失败\n" +
                    "输入 \(Display.integer(Display.number(model["input"]))) · 输出 \(Display.integer(Display.number(model["output"]))) · 缓存读取 \(Display.integer(Display.number(model["cache_read"]))) · 缓存写入 \(Display.integer(Display.number(model["cache_write"])))"
                }
                self.alert("用量 · "+self.keyName(key), lines.isEmpty ? "此密钥暂无用量。" : lines.joined(separator: "\n\n"))
            case .failure(let error): self.notice.stringValue = error.localizedDescription
            }
        }
    }

    @objc private func createKey() { editKey(nil) }

    private func editKey(_ key: [String: Any]?) {
        let note = field(key?["note"] as? String ?? "", placeholder: "可留空")
        let rpm = field(key.map { String(Display.number($0["rpm"])) } ?? "0", placeholder: "0 表示不限")
        let budgetValue = (key?["budget_nanos"] as? NSNumber).map { String(format: "%.2f", $0.doubleValue/1_000_000_000) } ?? ""
        let budget = field(budgetValue, placeholder: "留空不限额")
        let activation = NSSecureTextField()
        let needsActivation = key == nil && snapshot["activation_required"] as? Bool == true
        var fields = [("备注",note),("限频（次 / 分）",rpm),("累计额度（美元）",budget)]
        if needsActivation { fields.append(("激活码", activation)) }
        form(key == nil ? "创建密钥":"编辑密钥", fields) {
            let maxRate = Int(rpm.stringValue.trimmingCharacters(in: .whitespaces))
            let rawBudget = budget.stringValue.trimmingCharacters(in: .whitespaces)
            let cents = Decimal(string:rawBudget,locale:Locale(identifier:"en_US_POSIX")).map { $0*100 }
            guard let maxRate, (0...600).contains(maxRate), rawBudget.isEmpty ||
                    (cents.map { let n=NSDecimalNumber(decimal:$0); return $0>=0 && $0<=100_000_000 && Decimal(n.intValue)==$0 } == true) else {
                self.notice.stringValue = "请输入有效限频与累计额度，金额最多保留两位小数。"
                return
            }
            var args: [String: Any] = ["note":note.stringValue.trimmingCharacters(in:.whitespacesAndNewlines),
                                       "rpm":maxRate]
            args["budget_cents"] = cents.map { NSDecimalNumber(decimal:$0).intValue as Any } ?? NSNull()
            if needsActivation { args["activation_code"] = activation.stringValue }
            if let id = key?["id"] as? String { args["id"] = id }
            self.mutate(key == nil ? "issue":"update", args)
        }
    }

    @objc private func editSlots() {
        let value = field(String(Display.number(snapshot["concurrency"])), placeholder: "0 表示不限")
        form("处理槽位", [("并发数",value)]) {
            let text=value.stringValue.trimmingCharacters(in:.whitespaces)
            guard !text.isEmpty, text.allSatisfy({$0 >= "0" && $0 <= "9"}) else {
                self.notice.stringValue = "处理槽位必须是非负整数。"
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
            case .failure(let error): self.notice.stringValue = error.localizedDescription
            }
        }
    }

    private func showTicket(_ ticket: String) {
        guard let window = view.window else { return }
        let dialog = NSAlert()
        dialog.messageText = "保存 Safe Code Key"
        dialog.informativeText = "请把密钥保存到安全的位置。"
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
        field.placeholderString = placeholder
        return field
    }

    private func form(_ title: String, _ fields: [(String,NSTextField)], save: @escaping () -> Void) {
        guard let window = view.window else { return }
        let dialog = NSAlert()
        dialog.messageText = title
        let grid = NSGridView(views: fields.map { [NSTextField(labelWithString:$0.0),$0.1] })
        grid.columnSpacing = 12
        grid.rowSpacing = 9
        for (_,field) in fields { field.widthAnchor.constraint(equalToConstant:240).isActive = true }
        grid.frame.size = grid.fittingSize
        dialog.accessoryView = grid
        dialog.addButton(withTitle:"保存")
        dialog.addButton(withTitle:"取消")
        dialog.beginSheetModal(for:window) { result in if result == .alertFirstButtonReturn { save() } }
        fields.first?.1.selectText(nil)
    }

    private func alert(_ title: String, _ message: String) {
        guard let window = view.window else { return }
        let dialog = NSAlert()
        dialog.messageText = title
        dialog.informativeText = message
        dialog.beginSheetModal(for:window)
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

private enum Display {
    static func number(_ value: Any?) -> Int64 { (value as? NSNumber)?.int64Value ?? 0 }
    static func integer(_ value: Int64) -> String { value.formatted() }
    static func usd(_ nanos: Int64) -> String { "$"+String(format:"%.4f",Double(nanos)/1_000_000_000) }
    static func date(_ value: Any?) -> String {
        guard let value = value as? NSNumber else { return "—" }
        return Date(timeIntervalSince1970:value.doubleValue).formatted(date:.numeric,time:.shortened)
    }
}

private final class GatewayTrendView: NSView {
    var counts: [Int:Int64] = [:] { didSet { needsDisplay = true } }
    var bucketCount = 0 { didSet { needsDisplay = true } }
    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard bucketCount > 0 else { return }
        let peak = max(1, counts.values.max() ?? 0)
        let step = bounds.width/CGFloat(bucketCount)
        NSColor.controlAccentColor.setFill()
        for index in 0..<bucketCount {
            let amount = counts[index] ?? 0
            let height = (bounds.height-10)*CGFloat(amount)/CGFloat(peak)
            NSBezierPath(roundedRect:NSRect(x:CGFloat(index)*step+2,y:4,width:max(1,step-4),height:max(1,height)),
                         xRadius:2,yRadius:2).fill()
        }
    }
}
