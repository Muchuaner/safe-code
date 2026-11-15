import AppKit
import CryptoKit
import Foundation

/// macOS ships no RDP client, so the Windows build's one-click "打开桌面" stops at a
/// download prompt on the Mac side. This file removes that step.
///
/// Resolution order, cheapest first:
///   1. FreeRDP bundled inside the app (offline, no version drift)
///   2. FreeRDP installed on the machine (Homebrew)
///   3. Microsoft Windows App, either already present or provisioned on demand
///
/// Provisioning never asks for an administrator password: the official installer is
/// expanded with `pkgutil` and copied into `~/Applications`, which is the same result
/// as a user-scope install without touching `/Applications` or the system receipt database.
@MainActor
final class RDPClient {
    static let shared = RDPClient()

    /// Pinned to the build currently published in Homebrew's cask manifest. The SHA-256 and
    /// the Developer ID team check below mean a stale or swapped payload fails loudly instead
    /// of silently installing something else.
    private let payloadURL = "https://res.public.onecdn.static.microsoft/mro1cdnstorage/C1297A47-86C4-4C1F-97FA-950631F94777/MacAutoupdate/Windows_App_11.4.5_installer.pkg"
    private let payloadSHA256 = "bc7ed3e01cc207c095fdaac8adf328d4d92f47c43ba980a9d5850ddab44688ba"
    private let microsoftTeam = "UBF8T346G9"
    private let windowsAppBundleID = "com.microsoft.rdc.macos"

    private var provisioning = false
    private var panel: ProvisionPanel?

    enum Client {
        case freerdp(URL)
        case windowsApp(URL)
    }

    // MARK: - Discovery

    /// Returns the first usable client. The bundled build wins because it needs no Launch
    /// Services registration; a user-installed Windows App comes next because it renders
    /// Retina correctly and owns the clipboard, and a Homebrew FreeRDP is only a last
    /// resort that avoids a download.
    func locate() -> Client? {
        if let bundled = bundledFreeRDP() { return .freerdp(bundled) }
        if let app = windowsAppBundle() { return .windowsApp(app) }
        if let system = systemFreeRDP() { return .freerdp(system) }
        return nil
    }

    private func bundledFreeRDP() -> URL? {
        guard let binary = Bundle.main.resourceURL?.appendingPathComponent("RDP/sdl-freerdp"),
              FileManager.default.isExecutableFile(atPath: binary.path) else { return nil }
        return binary
    }

    /// Only the SDL build counts: `xfreerdp` on macOS needs XQuartz, so treating it as a
    /// usable client would launch a window that never appears.
    private func systemFreeRDP() -> URL? {
        for directory in ["/opt/homebrew/bin", "/usr/local/bin"] {
            let binary = URL(fileURLWithPath: directory).appendingPathComponent("sdl-freerdp")
            if FileManager.default.isExecutableFile(atPath: binary.path) { return binary }
        }
        return nil
    }

    /// Launch Services only learns the .rdp handler after the client has run once, so a fresh
    /// install has to be found by bundle identifier or by its on-disk location instead.
    /// The registration survives the app being trashed or moved, so the path is re-checked:
    /// returning a stale URL here would make every later click fail silently.
    private func windowsAppBundle() -> URL? {
        if let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier: windowsAppBundleID),
           FileManager.default.fileExists(atPath: app.path) {
            return app
        }
        for path in ["/Applications/Windows App.app",
                     NSHomeDirectory() + "/Applications/Windows App.app",
                     "/Applications/Microsoft Remote Desktop.app"] where FileManager.default.fileExists(atPath: path) {
            return URL(fileURLWithPath: path)
        }
        return nil
    }

    /// A client the user installed themselves is used as-is; only the absence of any client
    /// triggers the download, and the answer is cached for the rest of the session.
    func ensureClient(completion: @escaping (Result<Client, Error>) -> Void) {
        if let client = locate() { completion(.success(client)); return }
        if provisioning { completion(.failure(AppError.message("远程桌面客户端正在准备中，请稍候。"))) ; return }
        provisioning = true
        let panel = ProvisionPanel()
        self.panel = panel
        panel.update("正在下载 Microsoft Windows App…", 0)
        download { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .failure(let error):
                self.finishProvisioning()
                completion(.failure(self.downloadHint(error)))
            case .success(let pkg):
                panel.update("正在安装到「个人」应用目录…", nil)
                self.install(pkg: pkg) { installResult in
                    self.finishProvisioning()
                    switch installResult {
                    case .success(let app): completion(.success(.windowsApp(app)))
                    case .failure(let error): completion(.failure(error))
                    }
                }
            }
        }
    }

    private func finishProvisioning() {
        provisioning = false
        panel?.close()
        panel = nil
    }

    // MARK: - Launch

    /// Opens the generated .rdp with the resolved client. Windows App is launched with an
    /// explicit application URL so a freshly provisioned copy works before Launch Services
    /// has registered the .rdp association.
    func launch(rdp: URL, with client: Client, completion: @escaping (Error?) -> Void) {
        switch client {
        case .windowsApp(let app):
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            NSWorkspace.shared.open([rdp], withApplicationAt: app, configuration: configuration) { _, error in
                DispatchQueue.main.async { completion(error) }
            }
        case .freerdp(let binary):
            launchFreeRDP(binary: binary, rdp: rdp, completion: completion)
        }
    }

    private func launchFreeRDP(binary: URL, rdp: URL, completion: @escaping (Error?) -> Void) {
        let settings = RDPSettings(contentsOf: rdp)
        guard let address = settings.address else {
            completion(AppError.message("远程桌面配置缺少服务器地址，请重新打开桌面。"))
            return
        }
        var arguments = ["/v:" + address, "/cert:ignore", "+clipboard", "/dynamic-resolution"]
        if let username = settings.username, !username.isEmpty { arguments.append("/u:" + username) }
        if let width = settings.width, let height = settings.height { arguments.append("/size:\(width)x\(height)") }
        let process = Process()
        process.executableURL = binary
        process.arguments = arguments
        // FreeRDP asks for the password in its own window; a terminal-backed process would
        // otherwise inherit an unusable stdin.
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do { try process.run(); completion(nil) }
        catch { completion(AppError.message("无法启动内置远程桌面客户端：\(error.localizedDescription)")) }
    }

    // MARK: - Provisioning

    private func download(completion: @escaping (Result<URL, Error>) -> Void) {
        guard let url = URL(string: payloadURL) else { completion(.failure(AppError.message("下载地址无效。"))); return }
        let delegate = PackageDownload(progress: { [weak self] fraction in
            self?.panel?.update("正在下载 Microsoft Windows App…", fraction)
        }, completion: { [weak self] result in
            switch result {
            case .success(let file):
                guard let digest = self?.sha256(of: file), digest == self?.payloadSHA256 else {
                    try? FileManager.default.removeItem(at: file)
                    completion(.failure(AppError.message("远程桌面客户端校验失败，已丢弃下载文件。请检查网络后重试。")))
                    return
                }
                completion(.success(file))
            case .failure(let error): completion(.failure(error))
            }
        })
        let session = URLSession(configuration: .default, delegate: delegate, delegateQueue: nil)
        delegate.attach(session)
        session.downloadTask(with: url).resume()
        self.downloadSession = session
        self.downloadDelegate = delegate
    }

    private var downloadSession: URLSession?
    private var downloadDelegate: PackageDownload?

    private func sha256(of file: URL) -> String? {
        guard let data = try? Data(contentsOf: file, options: .mappedIfSafe) else { return nil }
        return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private func install(pkg: URL, completion: @escaping (Result<URL, Error>) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                // pkgutil lives in /usr/sbin, not /usr/bin. The destination must not exist yet:
                // --expand-full creates it, and an existing directory makes it fail.
                let staging = FileManager.default.temporaryDirectory.appendingPathComponent("safecode-rdp-" + UUID().uuidString, isDirectory: true)
                defer { try? FileManager.default.removeItem(at: staging) }
                try RDPClient.run("/usr/sbin/pkgutil", ["--expand-full", pkg.path, staging.path])
                guard let payload = RDPClient.findApplication(in: staging) else {
                    throw AppError.message("安装包结构无法识别，请改用「打开下载页」手动安装。")
                }
                // The payload is signed by Microsoft; verifying the team identifier keeps a
                // tampered mirror from being installed as a trusted remote desktop client.
                // The bundle identifier is checked too, so a signed but wrong bundle (the
                // updater, say) can never be presented as the desktop client.
                guard RDPClient.bundleIdentifier(of: payload) == self.windowsAppBundleID else {
                    throw AppError.message("安装包里没有找到远程桌面客户端，已中止安装。")
                }
                let details = try RDPClient.run("/usr/bin/codesign", ["-dv", "--verbose=4", payload.path], captureStderr: true)
                guard details.contains("TeamIdentifier=" + self.microsoftTeam) else {
                    throw AppError.message("下载的远程桌面客户端签名不属于 Microsoft，已中止安装。")
                }
                let target = URL(fileURLWithPath: NSHomeDirectory())
                    .appendingPathComponent("Applications", isDirectory: true)
                    .appendingPathComponent(payload.lastPathComponent, isDirectory: true)
                try? FileManager.default.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
                if FileManager.default.fileExists(atPath: target.path) { try FileManager.default.removeItem(at: target) }
                try RDPClient.run("/usr/bin/ditto", [payload.path, target.path])
                // Quarantine would otherwise ask the user to confirm a file they never
                // downloaded themselves; the signature check above already judged it.
                _ = try? RDPClient.run("/usr/bin/xattr", ["-dr", "com.apple.quarantine", target.path])
                try? FileManager.default.removeItem(at: pkg)
                DispatchQueue.main.async { completion(.success(target)) }
            } catch {
                DispatchQueue.main.async { completion(.failure(error)) }
            }
        }
    }

    /// The installer payload carries more than one application (Windows App plus Microsoft
    /// AutoUpdate), and directory enumeration order is not guaranteed, so picking the first
    /// `.app` can install the updater instead of the client. Identity is checked explicitly.
    private nonisolated static func findApplication(in root: URL) -> URL? {
        guard let enumerator = FileManager.default.enumerator(at: root, includingPropertiesForKeys: [.isDirectoryKey], options: [.skipsHiddenFiles]) else { return nil }
        var found: [URL] = []
        for case let item as URL in enumerator where item.pathExtension == "app" { found.append(item) }
        if let named = found.first(where: { $0.lastPathComponent.hasPrefix("Windows App") }) { return named }
        return found.first { bundleIdentifier(of: $0) == "com.microsoft.rdc.macos" }
    }

    private nonisolated static func bundleIdentifier(of application: URL) -> String? {
        guard let data = try? Data(contentsOf: application.appendingPathComponent("Contents/Info.plist")),
              let plist = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any] else { return nil }
        return plist["CFBundleIdentifier"] as? String
    }

    @discardableResult
    private nonisolated static func run(_ tool: String, _ arguments: [String], captureStderr: Bool = false) throws -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: tool)
        process.arguments = arguments
        let pipe = Pipe()
        if captureStderr { process.standardError = pipe } else { process.standardOutput = pipe; process.standardError = FileHandle.nullDevice }
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        // codesign reports to stderr and exits non-zero for ad-hoc payloads; the caller inspects
        // the text, so only a genuinely empty result is treated as failure here.
        let text = String(data: data, encoding: .utf8) ?? ""
        if process.terminationStatus != 0 && text.isEmpty { throw AppError.message("系统工具 \(tool) 执行失败。") }
        return text
    }

    private func downloadHint(_ error: Error) -> Error {
        AppError.message("自动准备远程桌面客户端失败：\(error.localizedDescription)\n\n可以重试，或点「打开下载页」手动安装 Microsoft Windows App。")
    }
}

// MARK: - Package download

private final class PackageDownload: NSObject, URLSessionDownloadDelegate {
    private let progress: (Double) -> Void
    private let completion: (Result<URL, Error>) -> Void
    private var settled = false

    init(progress: @escaping (Double) -> Void, completion: @escaping (Result<URL, Error>) -> Void) {
        self.progress = progress
        self.completion = completion
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64, totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        guard totalBytesExpectedToWrite > 0 else { return }
        let fraction = Double(totalBytesWritten) / Double(totalBytesExpectedToWrite)
        DispatchQueue.main.async { self.progress(fraction) }
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        // The temporary file is deleted as soon as this method returns, so move it first.
        let held = FileManager.default.temporaryDirectory.appendingPathComponent("WindowsApp-" + UUID().uuidString + ".pkg")
        do {
            try FileManager.default.moveItem(at: location, to: held)
            settle(.success(held))
        } catch { settle(.failure(error)) }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error = error { settle(.failure(error)) }
    }

    private func settle(_ result: Result<URL, Error>) {
        guard !settled else { return }
        settled = true
        DispatchQueue.main.async {
            self.completion(result)
            self.session?.invalidateAndCancel()
        }
    }

    private var session: URLSession?
    func attach(_ session: URLSession) { self.session = session }
}

// MARK: - Progress panel

/// A small non-modal panel: the download is the only long wait in the app, and blocking the
/// main thread with a modal alert would freeze the WebView and hide the reason it takes a while.
@MainActor
private final class ProvisionPanel {
    private let window: NSWindow
    private let label = NSTextField(labelWithString: "正在准备远程桌面客户端…")
    private let bar = NSProgressIndicator()

    init() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 420, height: 124),
                          styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "Safe Code"
        window.isReleasedWhenClosed = false
        window.center()
        let container = NSView(frame: window.contentLayoutRect)
        label.frame = NSRect(x: 24, y: 74, width: 372, height: 20)
        label.font = NSFont.systemFont(ofSize: 13)
        bar.frame = NSRect(x: 24, y: 44, width: 372, height: 12)
        bar.style = .bar
        bar.isIndeterminate = false
        bar.minValue = 0
        bar.maxValue = 1
        bar.doubleValue = 0
        let hint = NSTextField(labelWithString: "首次使用需要下载约 90 MB，安装到当前用户目录，不需要管理员密码。")
        hint.frame = NSRect(x: 24, y: 18, width: 372, height: 16)
        hint.font = NSFont.systemFont(ofSize: 11)
        hint.textColor = .secondaryLabelColor
        container.addSubview(label)
        container.addSubview(bar)
        container.addSubview(hint)
        window.contentView = container
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func update(_ text: String, _ fraction: Double?) {
        label.stringValue = text
        if let fraction = fraction {
            bar.isIndeterminate = false
            bar.doubleValue = max(0, min(1, fraction))
        } else {
            bar.isIndeterminate = true
            bar.startAnimation(nil)
        }
    }

    func close() {
        bar.stopAnimation(nil)
        window.orderOut(nil)
    }
}

// MARK: - .rdp parsing

/// Reads only the fields this app writes, so a hand-edited or third-party .rdp file cannot
/// inject arguments into the FreeRDP command line.
private struct RDPSettings {
    let address: String?
    let username: String?
    let width: Int?
    let height: Int?

    init(contentsOf url: URL) {
        let text = (try? String(contentsOf: url, encoding: .utf8)) ?? ""
        var address: String?; var username: String?; var width: Int?; var height: Int?
        for line in text.split(separator: "\n") {
            let field = line.trimmingCharacters(in: .whitespaces)
            if field.hasPrefix("full address:s:") { address = RDPSettings.sanitize(String(field.dropFirst(15))) }
            else if field.hasPrefix("username:s:") { username = RDPSettings.sanitize(String(field.dropFirst(11))) }
            else if field.hasPrefix("desktopwidth:i:") { width = Int(field.dropFirst(15)) }
            else if field.hasPrefix("desktopheight:i:") { height = Int(field.dropFirst(16)) }
        }
        self.address = address
        self.username = username
        self.width = width
        self.height = height
    }

    /// Strips characters that FreeRDP would read as a flag separator or shell metacharacter.
    private static func sanitize(_ value: String) -> String {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: ".-_:@"))
        return String(value.unicodeScalars.filter { allowed.contains($0) })
    }
}
