import AppKit

enum SafeCodeType {
    static let large: CGFloat = 22
    static let medium: CGFloat = 14
    static let small: CGFloat = 12
}

enum GatewayStyle {
    static let paper = color(0xf5f7f4)
    static let surface = NSColor.white
    static let ink = color(0x182a24)
    static let muted = color(0x566b61)
    static let line = color(0xdce5de)
    static let accent = color(0x24664f)
    static let wash = color(0xe6f2e9)
    static let header = color(0xf0f4f0)
    static let grayLine = color(0x85938d)
    static let danger = color(0x9f3839)
    static let dangerWash = color(0xfaeeee)

    static func color(_ rgb: UInt32) -> NSColor {
        NSColor(srgbRed: CGFloat((rgb >> 16) & 255)/255, green: CGFloat((rgb >> 8) & 255)/255,
                blue: CGFloat(rgb & 255)/255, alpha: 1)
    }

    static func label(_ title: String, color: NSColor = ink, size: CGFloat = SafeCodeType.medium,
                      weight: NSFont.Weight = .regular) -> NSTextField {
        let label = NSTextField(labelWithString: title)
        label.font = .systemFont(ofSize: size, weight: weight)
        label.textColor = color
        label.lineBreakMode = .byTruncatingTail
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return label
    }

    static func surface(_ content: NSView, inset: NSEdgeInsets = NSEdgeInsets(), radius: CGFloat = 10,
                        fill: NSColor = surface, border: Bool = true) -> NSView {
        let container = NSView()
        container.wantsLayer = true
        container.layer?.backgroundColor = fill.cgColor
        container.layer?.borderColor = line.cgColor
        container.layer?.borderWidth = border ? 1 : 0
        container.layer?.cornerRadius = radius
        container.layer?.masksToBounds = true
        container.addSubview(content)
        content.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            content.topAnchor.constraint(equalTo: container.topAnchor, constant: inset.top),
            content.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: inset.left),
            content.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -inset.right),
            content.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -inset.bottom)
        ])
        return container
    }

    static func rule() -> NSView {
        let line = NSView()
        line.wantsLayer = true
        line.layer?.backgroundColor = Self.line.cgColor
        line.heightAnchor.constraint(equalToConstant: 1).isActive = true
        return line
    }
}

final class GatewayButton: NSButton {
    enum Kind { case standard, primary, quota, tab, segment, link, dangerLink }
    private let kind: Kind
    private var hover = false
    private var tracking: NSTrackingArea?

    init(_ title: String, kind: Kind, target: AnyObject?, action: Selector?) {
        self.kind = kind
        super.init(frame: .zero)
        self.title = title
        self.target = target
        self.action = action
        isBordered = false
        setButtonType(kind == .tab || kind == .segment ? .pushOnPushOff : .momentaryPushIn)
        font = .systemFont(ofSize: textSize, weight: .medium)
        focusRingType = .exterior
    }

    required init?(coder: NSCoder) { fatalError("Use init(_:kind:target:action:)") }

    private var isLink: Bool { kind == .link || kind == .dangerLink }
    private var textSize: CGFloat { isLink || kind == .quota || kind == .segment ? SafeCodeType.small : SafeCodeType.medium }
    private var padding: CGFloat { isLink || kind == .tab ? 6 : 12 }
    private var textFont: NSFont {
        .systemFont(ofSize: textSize, weight: isLink || (kind == .tab && state == .on) ? .semibold : .medium)
    }

    override var intrinsicContentSize: NSSize {
        let width = (title as NSString).size(withAttributes: [.font: textFont]).width
        return NSSize(width: ceil(width) + padding*2 + (image == nil ? 0 : 21), height: isLink || kind == .segment ? 26 : 34)
    }

    override var title: String {
        didSet { invalidateIntrinsicContentSize(); needsDisplay = true }
    }

    override var image: NSImage? {
        didSet { invalidateIntrinsicContentSize(); needsDisplay = true }
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let tracking { removeTrackingArea(tracking) }
        let area = NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeInKeyWindow, .inVisibleRect], owner: self)
        addTrackingArea(area)
        tracking = area
    }

    override func mouseEntered(with event: NSEvent) { hover = true; needsDisplay = true }
    override func mouseExited(with event: NSEvent) { hover = false; needsDisplay = true }

    override func draw(_ dirtyRect: NSRect) {
        let primary = kind == .primary
        let selected = (kind == .tab || kind == .segment) && state == .on
        let opacity: CGFloat = isEnabled ? 1 : 0.55
        if kind == .segment {
            if selected || hover {
                let path = NSBezierPath(roundedRect:bounds.insetBy(dx:0.5,dy:0.5),xRadius:6,yRadius:6)
                (selected ? GatewayStyle.surface : GatewayStyle.wash).setFill()
                path.fill()
                GatewayStyle.line.setStroke()
                path.stroke()
            }
        } else if !isLink && kind != .tab {
            let path = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 7, yRadius: 7)
            let fill = primary ? (hover ? GatewayStyle.color(0x1a503d) : GatewayStyle.accent)
                : (hover || kind == .quota ? GatewayStyle.wash : GatewayStyle.surface)
            fill.withAlphaComponent(opacity).setFill()
            path.fill()
            (primary ? fill : GatewayStyle.line).setStroke()
            path.stroke()
        } else if selected {
            GatewayStyle.accent.setFill()
            NSBezierPath(rect: NSRect(x: 0, y: isFlipped ? bounds.maxY-2 : 0, width: bounds.width, height: 2)).fill()
        } else if hover && isEnabled {
            GatewayStyle.wash.setFill()
            NSBezierPath(roundedRect: bounds, xRadius: 5, yRadius: 5).fill()
        }
        let textColor = primary ? NSColor.white : kind == .dangerLink ? GatewayStyle.danger
            : isLink || selected ? GatewayStyle.accent : kind == .tab || kind == .segment || kind == .quota ? GatewayStyle.muted : GatewayStyle.ink
        let shownTitle = kind == .quota && bounds.width+1 < intrinsicContentSize.width
            ? title.replacingOccurrences(of: "剩餘", with: "") : title
        let text = NSAttributedString(string: shownTitle, attributes: [.foregroundColor: textColor.withAlphaComponent(opacity), .font: textFont])
        let size = text.size()
        let iconWidth: CGFloat = image == nil ? 0 : 21
        let x = max(1, floor((bounds.width-size.width-iconWidth)/2))
        text.draw(at: NSPoint(x: x, y: floor((bounds.height-size.height)/2)))
        if let image {
            let config = NSImage.SymbolConfiguration(pointSize: SafeCodeType.small, weight: .medium)
            let tint = NSImage.SymbolConfiguration(paletteColors: [textColor.withAlphaComponent(opacity)])
            image.withSymbolConfiguration(config.applying(tint))?.draw(
                in: NSRect(x: x+size.width+7, y: (bounds.height-14)/2, width: 14, height: 14))
        }
    }
}

final class GatewayColumnHeader: NSTableHeaderCell {
    override func draw(withFrame cellFrame: NSRect, in controlView: NSView) {
        GatewayStyle.header.setFill()
        cellFrame.fill()
        GatewayStyle.line.setFill()
        NSRect(x: cellFrame.minX, y: cellFrame.maxY-1, width: cellFrame.width, height: 1).fill()
        let style = NSMutableParagraphStyle()
        style.lineBreakMode = .byTruncatingTail
        if ["限頻", "請求", "Token", "已用 / 餘額", "等待", "處理耗時", "估算費用"].contains(stringValue) {
            style.alignment = .right
        }
        let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: SafeCodeType.small, weight: .semibold),
            .foregroundColor: GatewayStyle.muted, .paragraphStyle: style]
        NSAttributedString(string: stringValue, attributes: attributes).draw(in: cellFrame.insetBy(dx: 12, dy: 9))
    }
}

final class GatewayTableRow: NSTableRowView {
    override func drawBackground(in dirtyRect: NSRect) {
        (isEmphasized && isSelected ? GatewayStyle.wash : GatewayStyle.surface).setFill()
        bounds.fill()
        GatewayStyle.line.setFill()
        NSRect(x: 0, y: isFlipped ? bounds.maxY-1 : 0, width: bounds.width, height: 1).fill()
    }
}

final class GatewayPageView: NSView {
    override var isFlipped: Bool { true }
}

final class GatewaySelect: NSPopUpButton {
    override var intrinsicContentSize: NSSize {
        let textFont = font ?? .systemFont(ofSize: SafeCodeType.medium)
        let width = itemTitles.map { ($0 as NSString).size(withAttributes:[.font:textFont]).width }.max() ?? 0
        return NSSize(width:ceil(width)+43,height:34)
    }

    override func draw(_ dirtyRect: NSRect) {
        let path = NSBezierPath(roundedRect:bounds.insetBy(dx:0.5,dy:0.5),xRadius:7,yRadius:7)
        (isHighlighted ? GatewayStyle.wash : GatewayStyle.surface).setFill()
        path.fill()
        GatewayStyle.line.setStroke()
        path.stroke()
        let text = NSAttributedString(string:titleOfSelectedItem ?? "",attributes:[
            .foregroundColor:GatewayStyle.ink,.font:font ?? .systemFont(ofSize:SafeCodeType.medium)])
        text.draw(at:NSPoint(x:10,y:floor((bounds.height-text.size().height)/2)))
        let x = bounds.maxX-16
        let y = bounds.midY
        let chevron = NSBezierPath()
        chevron.lineWidth = 1.7
        chevron.lineCapStyle = .round
        chevron.lineJoinStyle = .round
        let direction: CGFloat = isFlipped ? 1 : -1
        chevron.move(to:NSPoint(x:x-3.5,y:y-2*direction))
        chevron.line(to:NSPoint(x:x,y:y+1.5*direction))
        chevron.line(to:NSPoint(x:x+3.5,y:y-2*direction))
        GatewayStyle.ink.setStroke()
        chevron.stroke()
    }
}
