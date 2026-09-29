import AppKit

// Floating live tail of recent buddy commands; full history lives in AuditLog.fileURL.
final class ActivityWindowController: NSWindowController {
    private let scrollView = NSScrollView()
    private let textView = NSTextView()
    private let timeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    init() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 640, height: 420),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Autonomous Buddy — Activity"
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 420, height: 240)
        super.init(window: window)

        buildLayout()
        render()

        NotificationCenter.default.addObserver(
            self,
            selector: #selector(onStateChanged),
            name: .autonomousBuddyAppStateChanged,
            object: nil
        )
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) is unused")
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
    }

    private func buildLayout() {
        guard let contentView = window?.contentView else { return }

        scrollView.translatesAutoresizingMaskIntoConstraints = false
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.autohidesScrollers = false
        scrollView.borderType = .noBorder
        scrollView.drawsBackground = false

        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = false
        textView.drawsBackground = true
        textView.backgroundColor = .textBackgroundColor
        textView.textContainerInset = NSSize(width: 12, height: 10)
        textView.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        textView.autoresizingMask = [.width]
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.textContainer?.widthTracksTextView = true
        textView.textContainer?.containerSize = NSSize(
            width: scrollView.contentSize.width,
            height: .greatestFiniteMagnitude
        )

        scrollView.documentView = textView
        contentView.addSubview(scrollView)

        let footer = NSTextField(labelWithString: "Showing the last \(AppState.recentCommandsCap) commands · full audit log in ~/Library/Application Support/AutonomousBuddy/audit.log")
        footer.translatesAutoresizingMaskIntoConstraints = false
        footer.textColor = .secondaryLabelColor
        footer.font = .systemFont(ofSize: 11)
        footer.lineBreakMode = .byTruncatingMiddle
        contentView.addSubview(footer)

        NSLayoutConstraint.activate([
            scrollView.topAnchor.constraint(equalTo: contentView.topAnchor),
            scrollView.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            scrollView.bottomAnchor.constraint(equalTo: footer.topAnchor, constant: -6),

            footer.leadingAnchor.constraint(equalTo: contentView.leadingAnchor, constant: 12),
            footer.trailingAnchor.constraint(equalTo: contentView.trailingAnchor, constant: -12),
            footer.bottomAnchor.constraint(equalTo: contentView.bottomAnchor, constant: -8),
        ])
    }

    @objc private func onStateChanged() {
        // Notifications can post from background queues despite our onMain
        // hopping in AppState — be defensive.
        if Thread.isMainThread {
            render()
        } else {
            DispatchQueue.main.async { [weak self] in self?.render() }
        }
    }

    private func render() {
        let commands = AppState.shared.recentCommands.reversed() // oldest → newest (bottom)
        let lines: [String] = commands.map { record in
            let symbol = record.ok ? "✓" : "✗"
            let time = timeFormatter.string(from: record.timestamp)
            var line = "\(symbol)  \(time)  \(record.summary)"
            if let err = record.error, !err.isEmpty {
                line += "    — \(err)"
            }
            return line
        }
        let text: String
        if lines.isEmpty {
            text = "No commands yet. Once your device dispatches a command, it will appear here.\n"
        } else {
            text = lines.joined(separator: "\n") + "\n"
        }

        let wasNearBottom = isScrolledNearBottom()
        textView.string = text
        if wasNearBottom {
            textView.scrollToEndOfDocument(nil)
        }
    }

    private func isScrolledNearBottom() -> Bool {
        guard let clip = scrollView.contentView as NSClipView? else { return true }
        let visible = clip.documentVisibleRect
        let total = textView.bounds.height
        // Within ~40pt of the bottom counts as "tailing".
        return (visible.maxY + 40) >= total
    }

    func show() {
        NSApp.activate(ignoringOtherApps: true)
        if window?.isVisible != true {
            window?.center()
        }
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        render()
        textView.scrollToEndOfDocument(nil)
    }
}
