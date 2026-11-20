import AppKit
import WebKit
import Security

private let bundleID = "org.safecode.desktop"
private let safeCodeVersion = "Beta-0.4.4.19"
private enum AppError: LocalizedError {
    case message(String)
    case keychainCancelled
    var errorDescription: String? {
        switch self {
        case .message(let text): return text
        case .keychainCancelled: return "未获得钥匙串授权，仍可手动填写服务器登录信息。"
        }
    }
}
private enum NativeDiagnosticOperation: String {
    case loginRestore="login_restore", accountCustomization="account_customization"
    case desktopKeyEnable="desktop_key_enable", desktopKeyDisable="desktop_key_disable", desktopKeyRestore="desktop_key_restore"
    case claudeDesktopStart="claude_desktop_start", claudeDesktopStop="claude_desktop_stop"
    case desktopLaunch="desktop_launch"
}
private func json(_ value: Any) -> String {
    guard JSONSerialization.isValidJSONObject(value), let data = try? JSONSerialization.data(withJSONObject: value, options: [.fragmentsAllowed, .sortedKeys]), let string = String(data: data, encoding: .utf8) else { return "null" }; return string
}
private func jsString(_ value: String) -> String { String(json([value]).dropFirst().dropLast()) }
private func keychainSecret(allowPrompt: Bool, allowCreate: Bool) throws -> Data {
    if !allowPrompt {
        // Existing keys use the macOS login keychain, which has its own interaction setting.
        var previous = DarwinBoolean(false)
        guard SecKeychainGetUserInteractionAllowed(&previous) == errSecSuccess,
              SecKeychainSetUserInteractionAllowed(false) == errSecSuccess else {
            throw AppError.message("暂时无法静默访问 macOS 钥匙串。")
        }
        defer { _ = SecKeychainSetUserInteractionAllowed(previous.boolValue) }
        return try readKeychainSecret(allowCreate: allowCreate)
    }
    return try readKeychainSecret(allowCreate: allowCreate)
}
private func readKeychainSecret(allowCreate: Bool) throws -> Data {
    let query: [String: Any] = [kSecClass as String:kSecClassGenericPassword, kSecAttrService as String:bundleID, kSecAttrAccount as String:"local-encryption-v1", kSecReturnData as String:true, kSecMatchLimit as String:kSecMatchLimitOne]
    var result: CFTypeRef?; let status = SecItemCopyMatching(query as CFDictionary, &result)
    if status == errSecSuccess, let data = result as? Data, data.count == 32 { return data }
    if status == errSecItemNotFound && !allowCreate {
        throw AppError.message("找不到原来的钥匙串密钥，已保留本机加密数据。")
    }
    if status == errSecUserCanceled { throw AppError.keychainCancelled }
    guard status == errSecItemNotFound else { throw AppError.message("未获得钥匙串授权，仍可手动填写服务器登录信息。") }
    var data = Data(count:32); let random = data.withUnsafeMutableBytes { SecRandomCopyBytes(kSecRandomDefault, 32, $0.baseAddress!) }
    guard random == errSecSuccess else { throw AppError.message("无法生成本机加密密钥。") }
    let item: [String:Any] = [kSecClass as String:kSecClassGenericPassword, kSecAttrService as String:bundleID, kSecAttrAccount as String:"local-encryption-v1", kSecValueData as String:data, kSecAttrAccessible as String:kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
    let added = SecItemAdd(item as CFDictionary,nil)
    guard added == errSecSuccess else { throw AppError.message("无法保存本机钥匙串密钥（\(added)）。") }; return data
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler, WKNavigationDelegate, WKUIDelegate {
    private var window: NSWindow!
    private var web: WKWebView!
    private var backend: Process?
    private var stdin: FileHandle?
    private var readBuffer = Data()
    private var completions: [String:(Result<Any,Error>)->Void] = [:]
    private var statusItem: NSStatusItem!
    private var popover = NSPopover()
    private var stateText: NSTextField!
    private var durationText: NSTextField!
    private var values: [NSTextField] = []
    private var busy = false, guarding = false, quitting = false, ready = false
    private var directory = URL(fileURLWithPath:NSHomeDirectory()).appendingPathComponent("Library/Application Support/Safe Code", isDirectory:true)
    private var resources: URL { Bundle.main.resourceURL! }
    private var index: URL { resources.appendingPathComponent("Web/index.html") }
    private var smokeOutput: URL?
    private var servicesWindow: NSWindow?
    private var servicesWeb: WKWebView?
    private var gatewayWindow: NSWindow?
    private var gatewayWeb: WKWebView?
    private var gatewayContext: String?
    private var gatewayMutationPending = false
    private var gatewayOpening = false
    private var gatewayPage: URL { resources.appendingPathComponent("Web/gateway.html") }
    private struct DialogSpec {
        let id: String
        let title: String
        let size: NSSize
    }
    private var dialogWindows: [(id: String, window: NSWindow)] = []
    private var requestedDialogs: [DialogSpec] = []
    private var takingDialogSnapshot = false
    private var dialogHost: NSWindow { dialogWindows.last?.window ?? window }
    private var didRunSmoke = false
    private var encryptionKey: Data?
    private var unlockingSecrets = false
    private var hasSavedLogin: Bool { hasLocalFile("login.aes") }
    private var hasDesktopKey: Bool { hasLocalFile("desktop-key.state") }
    private var hasEncryptedData: Bool {
        if hasSavedLogin || hasDesktopKey || hasLocalFile("desktop-ssh.aes") { return true }
        let drafts = directory.appendingPathComponent("account-drafts",isDirectory:true)
        return (try? FileManager.default.contentsOfDirectory(atPath:drafts.path))?.contains(where:{$0.hasSuffix(".aes")}) == true
    }
    private func hasLocalFile(_ name:String) -> Bool {
        FileManager.default.fileExists(atPath:directory.appendingPathComponent(name).path)
    }
    private var restarted = false
    private var isolated = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            let args = CommandLine.arguments
            if let i = args.firstIndex(of:"--smoke-test"), args.count > i+1 {
                isolated=true;smokeOutput=URL(fileURLWithPath:args[i+1]);directory=smokeOutput!.deletingLastPathComponent().appendingPathComponent("isolated-data",isDirectory:true)
            }
            if let i=args.firstIndex(of:"--preview-sandbox"),args.count>i+1 {isolated=true;directory=URL(fileURLWithPath:args[i+1],isDirectory:true)}
            if !isolated, let existing=NSRunningApplication.runningApplications(withBundleIdentifier:bundleID).first(where:{$0.processIdentifier != ProcessInfo.processInfo.processIdentifier}) { existing.activate(options:[.activateAllWindows]); NSApp.terminate(nil);return }
            try FileManager.default.createDirectory(at:directory,withIntermediateDirectories:true,attributes:[.posixPermissions:0o700])
            makeMenu(); makeWindow(); makeStatus()
            if !isolated {
                encryptionKey=try? keychainSecret(allowPrompt:false,allowCreate:!hasEncryptedData)
            }
            try startBackend()
            web.loadFileURL(index,allowingReadAccessTo:resources.appendingPathComponent("Web",isDirectory:true))
            window.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true)
        } catch { fail(error.localizedDescription); NSApp.terminate(nil) }
    }
    private func makeMenu() {
        let main=NSMenu();let app=NSMenu();let root=NSMenuItem();root.submenu=app;main.addItem(root)
        app.addItem(withTitle:"关于 Safe Code",action:#selector(about),keyEquivalent:"")
        app.addItem(withTitle:"更新与反馈",action:#selector(openServices),keyEquivalent:"").target=self
        app.addItem(.separator());app.addItem(withTitle:"显示主界面",action:#selector(showMain),keyEquivalent:"0")
        app.addItem(withTitle:"关闭窗口",action:#selector(closeKeyWindow),keyEquivalent:"w").target=self
        app.addItem(withTitle:"隐藏 Safe Code",action:#selector(NSApplication.hide(_:)),keyEquivalent:"h")
        app.addItem(.separator());app.addItem(withTitle:"安全退出",action:#selector(quit),keyEquivalent:"q")
        let edit=NSMenu(title:"编辑");let editItem=NSMenuItem(title:"编辑",action:nil,keyEquivalent:"");editItem.submenu=edit;main.addItem(editItem)
        for (name,selector,key) in [("撤销",Selector(("undo:")),"z"),("剪切",#selector(NSText.cut(_:)),"x"),("复制",#selector(NSText.copy(_:)),"c"),("粘贴",#selector(NSText.paste(_:)),"v"),("全选",#selector(NSText.selectAll(_:)),"a")] {edit.addItem(withTitle:name,action:selector,keyEquivalent:key)}
        NSApp.mainMenu=main
    }
    private var shim: String { """
        window.__safeCodeDesktop=true;window.__safeCodePlatform='macos';
        window.chrome={webview:{postMessage:m=>window.webkit.messageHandlers.native.postMessage(m)}};
        if(!crypto.randomUUID)crypto.randomUUID=()=>{const b=new Uint8Array(16);crypto.getRandomValues(b);b[6]=(b[6]&15)|64;b[8]=(b[8]&63)|128;const h=[...b].map(x=>x.toString(16).padStart(2,'0')).join('');return h.slice(0,8)+'-'+h.slice(8,12)+'-'+h.slice(12,16)+'-'+h.slice(16,20)+'-'+h.slice(20)};
        document.addEventListener('click',e=>{if(!window.safeCodeNativeReady&&!e.target.closest?.('[data-safe-code-startup-close]')){e.preventDefault();e.stopImmediatePropagation();}},true);
        document.addEventListener('DOMContentLoaded',()=>{if(!window.safeCodeNativeReady){document.body.innerHTML='<p style="padding:32px;font:14px system-ui">客户端界面未能加载，请检查应用是否完整。</p>';}});
        """ }
    private func configuration(services: Bool=false, gateway: Bool=false) -> WKWebViewConfiguration {
        let config=WKWebViewConfiguration();config.websiteDataStore = .nonPersistent()
        config.preferences.javaScriptCanOpenWindowsAutomatically=false
        config.userContentController.add(self,name:"native")
        config.userContentController.addUserScript(WKUserScript(source:shim,injectionTime:.atDocumentStart,forMainFrameOnly:true))
        if !gateway {
            let flag=services ? "window.__safeCodeServicesWindow=true" : "window.__safeCodeNativeDialogs=true"
            config.userContentController.addUserScript(WKUserScript(source:flag,injectionTime:.atDocumentStart,forMainFrameOnly:true))
        }
        return config
    }
    private func makeWindow() {
        window=NSWindow(contentRect:NSRect(x:0,y:0,width:360,height:536),styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false)
        window.title=isolated ? "Safe Code · 隔离预览":"Safe Code";window.minSize=NSSize(width:360,height:500);window.center();window.delegate=self;window.isReleasedWhenClosed=false
        web=WKWebView(frame:.zero,configuration:configuration());web.navigationDelegate=self;web.uiDelegate=self
        window.contentView=web
    }
    private func makeStatus() {
        statusItem=NSStatusBar.system.statusItem(withLength:NSStatusItem.squareLength)
        statusItem.button?.image=NSImage(systemSymbolName:"externaldrive.connected.to.line.below",accessibilityDescription:"Safe Code 挂载状态")
        statusItem.button?.target=self;statusItem.button?.action=#selector(toggleStatus)
        let controller=NSViewController();controller.view=NSView(frame:NSRect(x:0,y:0,width:276,height:196))
        let main=NSStackView();main.orientation = .vertical;main.spacing=10;main.alignment = .leading;main.translatesAutoresizingMaskIntoConstraints=false
        controller.view.addSubview(main);NSLayoutConstraint.activate([main.leadingAnchor.constraint(equalTo:controller.view.leadingAnchor,constant:16),main.trailingAnchor.constraint(equalTo:controller.view.trailingAnchor,constant:-16),main.topAnchor.constraint(equalTo:controller.view.topAnchor,constant:14)])
        let heading=NSTextField(labelWithString:"Safe Code");heading.font = .systemFont(ofSize:14,weight:.semibold);main.addArrangedSubview(heading)
        stateText=NSTextField(labelWithString:"未挂载目录");stateText.font = .systemFont(ofSize:11);stateText.textColor = .secondaryLabelColor;main.addArrangedSubview(stateText)
        durationText=NSTextField(labelWithString:"0 个目录  ·  0:00:00");durationText.font = .monospacedDigitSystemFont(ofSize:11,weight:.regular);main.addArrangedSubview(durationText)
        let metrics=NSStackView();metrics.orientation = .horizontal;metrics.distribution = .fillEqually;metrics.spacing=12
        for title in ["发送","接收","读写"] {let column=NSStackView();column.orientation = .vertical;column.alignment = .leading;column.spacing=3;let label=NSTextField(labelWithString:title);label.font = .systemFont(ofSize:10);label.textColor = .secondaryLabelColor;let value=NSTextField(labelWithString:title=="读写" ? "0 次":"0 B");value.font = .monospacedDigitSystemFont(ofSize:12,weight:.medium);values.append(value);column.addArrangedSubview(label);column.addArrangedSubview(value);metrics.addArrangedSubview(column)}
        main.addArrangedSubview(metrics);metrics.widthAnchor.constraint(equalTo:main.widthAnchor).isActive=true
        let buttons=NSStackView();buttons.orientation = .horizontal;buttons.spacing=7;buttons.distribution = .fillEqually
        for (title,selector) in [("主界面",#selector(showMain)),("重连",#selector(reconnect)),("安全退出",#selector(quit))] {let b=NSButton(title:title,target:self,action:selector);b.bezelStyle = .rounded;b.controlSize = .small;buttons.addArrangedSubview(b)}
        main.addArrangedSubview(buttons);buttons.widthAnchor.constraint(equalTo:main.widthAnchor).isActive=true
        popover.contentViewController=controller;popover.behavior = .transient;popover.contentSize=NSSize(width:276,height:196)
    }
    private func startBackend() throws {
        let process=Process();process.executableURL=Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/Core/SafeCode.MacBackend")
        process.arguments=["--data-directory",directory.path]
        var env=ProcessInfo.processInfo.environment;env["DOTNET_CLI_TELEMETRY_OPTOUT"]="1";process.environment=env
        let input=Pipe(),output=Pipe();process.standardInput=input;process.standardOutput=output;process.standardError=FileHandle.nullDevice
        stdin=input.fileHandleForWriting
        output.fileHandleForReading.readabilityHandler={ [weak self] handle in
            let data=handle.availableData
            if data.isEmpty {handle.readabilityHandler=nil;return}
            DispatchQueue.main.async {self?.receive(data)}
        }
        process.terminationHandler={ [weak self] p in DispatchQueue.main.async {
            guard let self=self,!self.quitting else{return}
            self.ready=false;self.stdin=nil;self.readBuffer.removeAll()
            let replies=Array(self.completions.values);self.completions.removeAll()
            for reply in replies{reply(.failure(AppError.message("连接核心已退出，请重新连接服务器。")))}
            if self.smokeOutput != nil {self.fail("连接核心异常退出，状态码 \(p.terminationStatus)。");self.quitting=true;NSApp.terminate(nil);return}
            if !self.restarted {
                self.restarted=true
                do {try self.startBackend()} catch {self.fail(error.localizedDescription)}
            }
            self.fail("连接核心已退出，加密通道已断开。已尝试恢复核心并撤销残留本机控制授权；请重新连接服务器，检查挂载状态。")
        }}
        try process.run();backend=process
        var boot:[String:Any]=["offline":isolated]
        if let encryptionKey { boot["key"]=encryptionKey.base64EncodedString() }
        try stdin?.write(contentsOf:Data((json(boot)+"\n").utf8))
    }
    private func send(_ message:[String:Any]) {
        do {guard backend?.isRunning==true,let stdin=stdin else{throw AppError.message("客户端核心未运行。")};try stdin.write(contentsOf:Data((json(message)+"\n").utf8))}
        catch {if let id=message["id"] as? String {if let reply=completions.removeValue(forKey:id){reply(.failure(error))}else{answer(id,error:error.localizedDescription)}}}
    }
    private func request(_ op:String,_ args:[String:Any]=[:],completion:@escaping(Result<Any,Error>)->Void) {
        let id="native-"+UUID().uuidString;completions[id]=completion;send(["id":id,"op":op,"args":args])
    }
    private func receive(_ data:Data) {
        readBuffer.append(data)
        while let newline=readBuffer.firstIndex(of:10) {
            let line=readBuffer.prefix(upTo:newline);readBuffer.removeSubrange(...newline)
            guard let message=(try? JSONSerialization.jsonObject(with:line)) as? [String:Any] else{continue}
            if let id=message["id"] as? String {
                if let reply=completions.removeValue(forKey:id) {if let error=message["error"] as? String {reply(.failure(AppError.message(error)))}else{reply(.success(message["result"] ?? NSNull()))}}
                else {web.evaluateJavaScript("window.safeCodeNativeResponse?.("+json(message)+")",completionHandler:nil)}
                continue
            }
            switch message["type"] as? String {
            case "ready":ready=true
            case "logs":web.evaluateJavaScript("window.safeCodeNativeLogs?.("+json(message["lines"] ?? [])+")",completionHandler:nil)
            case "status":updateStatus(message)
            case "updateProgress":(servicesWeb ?? web).evaluateJavaScript("window.safeCodeUpdateProgress?.(\(message["done"] ?? 0),\(message["total"] ?? 0))",completionHandler:nil)
            case "openUrl":if let raw=message["url"] as? String{openURL(raw)}
            case "openFile":if let raw=message["path"] as? String{openFile(raw)}
            default:break
            }
        }
    }
    private func answer(_ id:String,_ result:Any=NSNull(),error:String?=nil,in view:WKWebView?=nil) {
        let response:[String:Any]=["id":id,"result":result,"error":error ?? NSNull()]
        (view ?? web).evaluateJavaScript("window.safeCodeNativeResponse?.("+json(response)+")",completionHandler:nil)
    }
    func userContentController(_ userContentController: WKUserContentController,didReceive message:WKScriptMessage) {
        guard message.frameInfo.isMainFrame,let source=message.frameInfo.request.url,source.isFileURL,let body=message.body as? [String:Any] else{return}
        if source.standardizedFileURL.path==gatewayPage.path {
            guard let view=message.webView,view===gatewayWeb,
                  let id=body["id"] as? String,let op=body["op"] as? String,let args=body["args"] as? [String:Any] else{return}
            gatewayRequest(op,args,id:id,from:view)
            return
        }
        guard source.standardizedFileURL.path==index.path,let id=body["id"] as? String,let op=body["op"] as? String,let args=body["args"] as? [String:Any] else{return}
        if let view=message.webView,view===servicesWeb {
            let allowed=["services.preferences","services.consent","diagnostics.consent","updates.check","updates.download","updates.install","feedback.send"]
            guard allowed.contains(op) else{return}
            serviceRequest(op,args,id:id,from:view)
            return
        }
        guard message.webView===web else{return}
        switch op {
        case "login.restoreAvailable":answer(id,!isolated && encryptionKey==nil && hasSavedLogin)
        case "login.restoreSaved":
            guard !isolated && hasSavedLogin else{answer(id);return}
            withUnlockedSecrets(replyID:id,diagnostic:.loginRestore){self.request("login.restoreSaved",completion:$0)}
        case "account.draft.get" where !isolated && args["interactive"] as? Bool == true && encryptionKey==nil:
            request(op,args){[weak self] result in
                guard let self=self else{return}
                if case .failure(let error)=result,error.localizedDescription=="本机加密数据尚未解锁。" {
                    self.withUnlockedSecrets(replyID:id,diagnostic:.accountCustomization){self.request(op,args,completion:$0)}
                }else{self.answer(id,with:result)}
            }
        case "claudeDesktop.start":
            guard !hasDesktopKey else{answer(id,error:"请先停用 API Key 接入，再启用 SSH 接入。");return}
            guard args["accepted"] as? Bool == true else{answer(id,error:"请先确认 SSH 接入说明。");return}
            withUnlockedSecrets(replyID:id,diagnostic:.claudeDesktopStart){self.request(op,args,completion:$0)}
        case "claudeDesktop.stop" where hasLocalFile("desktop-ssh.aes"):
            withUnlockedSecrets(replyID:id,diagnostic:.claudeDesktopStop){self.request(op,args,completion:$0)}
        case "account.draft.save","desktopKey.enable":
            withUnlockedSecrets(replyID:id,diagnostic:op=="desktopKey.enable" ? .desktopKeyEnable : .accountCustomization){self.request(op,args,completion:$0)}
        case "desktopKey.status","desktopKey.disable":
            if hasDesktopKey && (op=="desktopKey.disable" || args["interactive"] as? Bool == true) {
                withUnlockedSecrets(replyID:id,diagnostic:op=="desktopKey.disable" ? .desktopKeyDisable : .desktopKeyRestore){self.request(op,args,completion:$0)}
            }else{send(body)}
        case "secrets.initialize":answer(id,error:"仅原生客户端可解锁本机数据。")
        case "diagnostics.native-failure":answer(id,error:"该操作仅供原生客户端内部调用。")
        case "window.smoke.dialog" where smokeOutput != nil:
            answer(id,["id":dialogWindows.last?.id ?? "",
                       "title":dialogHost.title,
                       "count":dialogWindows.count,
                       "contentHeight":dialogHost.contentRect(forFrameRect:dialogHost.frame).height,
                       "visible":dialogHost.isVisible,
                       "native":dialogHost !== window && web.window===dialogHost && window.contentView !== web])
        case "window.smoke.closeDialog" where smokeOutput != nil:
            dialogWindows.last?.window.performClose(nil)
            answer(id,true)
        case "window.smoke.gateway" where smokeOutput != nil:
            inspectGatewayWindow(id,args:args)
        case "window.fitKeyDialog":
            if let active=dialogWindows.last,active.id=="desktop-key-dialog",
               let height=args["height"] as? Double,height.isFinite {
                let current=active.window.contentRect(forFrameRect:active.window.frame)
                let screenHeight=active.window.screen?.visibleFrame.height ?? 720
                let fitted=max(120,min(height,screenHeight-70))
                if abs(current.height-fitted)>1 {
                    active.window.setContentSize(NSSize(width:current.width,height:fitted))
                }
            }
            answer(id,true)
        case "window.dialogs":
            let panels=args["panels"] as? [[String:Any]] ?? []
            requestedDialogs=panels.prefix(24).compactMap { panel in
                guard let identifier=panel["id"] as? String,let title=panel["title"] as? String,
                      let width=panel["width"] as? Double,let height=panel["height"] as? Double,
                      width.isFinite,height.isFinite else{return nil}
                return DialogSpec(id:identifier,title:title,size:NSSize(width:max(300,min(width,850)),height:max(180,min(height,680))))
            }
            reconcileDialogs()
            answer(id,true)
        case "window.resize":let wide=args["wide"] as? Bool ?? false;window.setContentSize(NSSize(width:wide ? 740:360,height:wide ? 634:536));window.center();answer(id,true)
        case "window.drag":if let event=NSApp.currentEvent,event.type == .leftMouseDown {window.performDrag(with:event)};answer(id,true)
        case "window.minimize":window.miniaturize(nil);answer(id,true)
        case "window.maximize":window.zoom(nil);answer(id,true)
        case "window.close":answer(id,true);window.performClose(nil)
        case "copy":NSPasteboard.general.clearContents();NSPasteboard.general.setString(args["text"] as? String ?? "",forType:.string);answer(id,true)
        case "pickFolder":let picker=NSOpenPanel();picker.title="选择要挂载到 VPS 的本机目录";picker.canChooseFiles=false;picker.canChooseDirectories=true;picker.allowsMultipleSelection=false;picker.beginSheetModal(for:dialogHost){[weak self] result in self?.answer(id,result == .OK ? picker.url?.path ?? "":NSNull())}
        case "services.open":openServices();answer(id,true)
        case "window.gateway.open":openGateway(id)
        case "connect","preview.begin":
            guard !gatewayMutationPending && !gatewayOpening else{answer(id,error:"请等待当前网关操作完成。");return}
            gatewayWindow?.close()
            send(body)
        case "services.consent","services.confirm","diagnostics.consent","updates.check":serviceRequest(op,args,id:id,from:web)
        case "mount.status.open":toggleStatus();answer(id,true)
        case "open":openURL(args["url"] as? String ?? "");answer(id,true)
        default:send(body)
        }
    }
    private func reconcileDialogs() {
        guard !takingDialogSnapshot else{return}
        for (existing,spec) in zip(dialogWindows,requestedDialogs) where existing.id==spec.id {
            existing.window.title=spec.title
        }
        let current=dialogWindows.map { $0.id }
        let next=requestedDialogs.map { $0.id }
        guard current != next else{return}
        if next.starts(with:current) && dialogHost.isVisible {
            // Preserve the underlying content while the one live WebView is in the child window.
            takingDialogSnapshot=true
            let host=dialogHost
            web.takeSnapshot(with:nil){[weak self,weak host] image,_ in
                guard let self=self else{return}
                self.takingDialogSnapshot=false
                self.presentDialogs(background:host===self.dialogHost ? image : nil)
            }
        }else{
            presentDialogs(background:nil)
        }
    }
    private func presentDialogs(background:NSImage?) {
        let shared=zip(dialogWindows,requestedDialogs).prefix(while:{$0.id==$1.id}).count
        guard shared != dialogWindows.count || shared != requestedDialogs.count else{return}
        let previous=dialogHost
        let previousRemains=shared==dialogWindows.count
        if previousRemains {
            // A snapshot stays only in memory and does not take focus or receive input.
            let placeholder=NSImageView()
            placeholder.image=background
            placeholder.imageScaling = .scaleAxesIndependently
            previous.contentView=placeholder
            previous.ignoresMouseEvents=true
        }else{
            previous.contentView=nil
        }
        while dialogWindows.count>shared {
            let closed=dialogWindows.removeLast().window
            dialogHost.removeChildWindow(closed)
            closed.orderOut(nil)
        }
        for spec in requestedDialogs.dropFirst(shared) {
            let parent=dialogHost
            let screen=parent.screen ?? NSScreen.main
            let visible=screen?.visibleFrame ?? NSRect(x:0,y:0,width:850,height:680)
            let size=NSSize(width:min(spec.size.width,visible.width),height:min(spec.size.height,visible.height-40))
            let panel=NSWindow(contentRect:NSRect(origin:.zero,size:size),styleMask:[.titled,.closable,.resizable],backing:.buffered,defer:false)
            panel.title=spec.title
            panel.contentMinSize=NSSize(width:min(size.width,320),height:min(size.height,spec.id=="desktop-key-dialog" ? 120:220))
            panel.isReleasedWhenClosed=false
            panel.delegate=self
            let frame=panel.frame
            panel.setFrameOrigin(NSPoint(x:min(max(parent.frame.midX-frame.width/2,visible.minX),visible.maxX-frame.width),
                                         y:min(max(parent.frame.midY-frame.height/2,visible.minY),visible.maxY-frame.height)))
            parent.addChildWindow(panel,ordered:.above)
            dialogWindows.append((spec.id,panel))
        }
        let active=dialogHost
        active.contentView=web
        active.ignoresMouseEvents=false
        let front=dialogWindows.last?.id ?? ""
        web.evaluateJavaScript("window.safeCodePresentNativeDialog?.("+jsString(front)+")",completionHandler:nil)
        active.makeKeyAndOrderFront(nil)
        active.makeFirstResponder(web)
    }
    private func answer(_ id:String,with result:Result<Any,Error>) {
        switch result {
        case .success(let value):answer(id,value)
        case .failure(let error):answer(id,error:error.localizedDescription)
        }
    }
    private func reportNativeFailure(_ operation:NativeDiagnosticOperation,cancelled:Bool=false,completion:@escaping(String?)->Void) {
        guard !isolated else{completion(nil);return}
        request("diagnostics.native-failure",["operation":operation.rawValue,"cancelled":cancelled]){ result in
            if case .success(let value)=result {completion(value as? String)}else{completion(nil)}
        }
    }
    private func withUnlockedSecrets(replyID:String,diagnostic: NativeDiagnosticOperation,perform: @escaping (@escaping (Result<Any,Error>)->Void)->Void) {
        guard !isolated else {answer(replyID,error:"隔离预览不会访问钥匙串。");return}
        let reply: (Result<Any,Error>)->Void = { [weak self] in self?.answer(replyID,with:$0) }
        let replyToUnlockFailure: (Error)->Void = { [weak self] error in
            guard let self=self else{return}
            let cancelled: Bool
            if case .keychainCancelled = error as? AppError {cancelled=true}else{cancelled=false}
            self.reportNativeFailure(diagnostic,cancelled:cancelled){[weak self] issue in
                self?.answer(replyID,error:error.localizedDescription+(issue.map{"\n问题编号："+$0} ?? ""))
            }
        }
        if encryptionKey != nil {perform(reply);return}
        guard !unlockingSecrets else {answer(replyID,error:"正在等待钥匙串授权。");return}
        do {
            unlockingSecrets=true
            let key=try keychainSecret(allowPrompt:true,allowCreate:!hasEncryptedData)
            request("secrets.initialize",["key":key.base64EncodedString()]){[weak self] result in
                guard let self=self else{return}
                self.unlockingSecrets=false
                switch result {
                case .success:self.encryptionKey=key;perform(reply)
                case .failure(let error):replyToUnlockFailure(error)
                }
            }
        }catch{
            unlockingSecrets=false
            replyToUnlockFailure(error)
        }
    }
    private func serviceRequest(_ op:String,_ args:[String:Any],id:String,from view:WKWebView) {
        request(op,args){[weak self,weak view] result in
            guard let self=self,let view=view else{return}
            switch result {
            case .success(let value):
                let other=view===self.web ? self.servicesWeb : self.web
                if ["services.consent","services.confirm","diagnostics.consent"].contains(op) {
                    other?.evaluateJavaScript("window.safeCodeApplyPreferences?.("+json(value)+")",completionHandler:nil)
                } else if op=="updates.check" && view===self.servicesWeb {
                    other?.evaluateJavaScript("window.safeCodeShowUpdateNotice?.("+json(value)+")",completionHandler:nil)
                }
                self.answer(id,value,in:view)
            case .failure(let error):
                self.answer(id,error:error.localizedDescription,in:view)
            }
        }
    }
    private func openGateway(_ id:String) {
        if let panel=gatewayWindow {panel.makeKeyAndOrderFront(nil);answer(id,true);return}
        guard !gatewayOpening else{answer(id,true);return}
        if smokeOutput != nil {presentGateway(context:nil);answer(id,true);return}
        gatewayOpening=true
        request("safeCodeApi.context"){[weak self] result in
            guard let self=self else{return}
            self.gatewayOpening=false
            switch result {
            case .success(let value):
                guard let context=value as? String else{self.answer(id,error:"无法绑定当前 SSH 会话。");return}
                self.presentGateway(context:context)
                self.answer(id,true)
            case .failure(let error):self.answer(id,error:error.localizedDescription)
            }
        }
    }
    private func presentGateway(context:String?) {
        let visible=dialogHost.screen?.visibleFrame ?? NSScreen.main?.visibleFrame ?? NSRect(x:0,y:0,width:1100,height:730)
        let width=min(1100,visible.width-24)
        let size=NSSize(width:width,height:min(670,visible.height-46,width * 0.72))
        let panel=NSWindow(contentRect:NSRect(origin:.zero,size:size),styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false)
        panel.title="Safe Code API · 网关管理"
        panel.contentMinSize=NSSize(width:min(740,size.width),height:min(400,size.height))
        panel.isReleasedWhenClosed=false
        panel.delegate=self
        let view=WKWebView(frame:.zero,configuration:configuration(gateway:true))
        view.navigationDelegate=self
        panel.contentView=view
        gatewayContext=context
        gatewayWeb=view
        gatewayWindow=panel
        view.loadFileURL(gatewayPage,allowingReadAccessTo:gatewayPage.deletingLastPathComponent())
        panel.setFrameOrigin(NSPoint(x:visible.midX-panel.frame.width/2,y:visible.midY-panel.frame.height/2))
        panel.makeKeyAndOrderFront(nil)
    }
    private func gatewayRequest(_ op:String,_ args:[String:Any],id:String,from view:WKWebView) {
        if op=="copy" {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(args["text"] as? String ?? "",forType:.string)
            answer(id,true,in:view)
            return
        }
        guard op=="safeCodeApi.manage",let action=args["action"] as? String else{answer(id,error:"未知网关管理操作。",in:view);return}
        if smokeOutput != nil && action=="list" {
            answer(id,["keys":[],"active":false,"waiting":0,"origin":"https://gateway.example.test"],in:view)
            return
        }
        if smokeOutput != nil && action=="settings" {
            answer(id,["bind":"192.0.2.1","port":45143],in:view)
            return
        }
        guard let context=gatewayContext else{answer(id,error:"SSH 会话已变更，请重新打开网关管理。",in:view);return}
        guard !gatewayMutationPending else{answer(id,error:"请等待当前网关操作完成。",in:view);return}
        let modifies=["issue","update","revoke","delete","set_port","set_origin","set_concurrency"].contains(action)
        if modifies {gatewayMutationPending=true}
        var options=args
        options["context"]=context
        request(op,options){[weak self,weak view] result in
            guard let self=self else{return}
            if modifies {self.gatewayMutationPending=false}
            guard let view=view,view===self.gatewayWeb else{return}
            switch result {
            case .success(let value):self.answer(id,value,in:view)
            case .failure(let error):self.answer(id,error:error.localizedDescription,in:view)
            }
        }
    }
    private func inspectGatewayWindow(_ id:String,args:[String:Any]) {
        guard let panel=gatewayWindow,let view=gatewayWeb else{answer(id,["visible":false]);return}
        if args["close"] as? Bool == true {panel.performClose(nil);answer(id,gatewayWindow==nil);return}
        let previous=dialogHost.frame
        let gatewayOrigin=panel.frame.origin
        if args["move"] as? Bool == true {panel.setFrameOrigin(NSPoint(x:panel.frame.minX+20,y:panel.frame.minY))}
        let check = """
        (() => {
          if (!window.safeCodeNativeReady || document.querySelector('#create-key').disabled) return false;
          const panel = document.querySelector('#settings-dialog');
          if (!panel.open) document.querySelector('#gateway-settings').click();
          return panel.open && document.querySelector('#settings-address').value === '192.0.2.1'
            && document.querySelector('#settings-port').value === '45143';
        })()
        """
        view.evaluateJavaScript(check){[weak self] value,_ in
            self?.answer(id,["visible":panel.isVisible,"ready":value as? Bool == true,
                             "native":panel.styleMask.contains(.titled)&&panel.isMovable&&panel.contentView===view,
                             "landscape":panel.frame.width>panel.frame.height,"moved":panel.frame.origin != gatewayOrigin,
                             "parentUnmoved":self?.dialogHost.frame==previous])
        }
    }
    @objc private func openServices() {
        if let servicesWindow {
            servicesWindow.makeKeyAndOrderFront(nil)
            servicesWeb?.evaluateJavaScript("window.safeCodeRefreshPreferences?.()",completionHandler:nil)
            return
        }
        let panel=NSWindow(contentRect:NSRect(x:0,y:0,width:440,height:565),styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false)
        panel.title="更新与反馈"
        panel.minSize=NSSize(width:360,height:400)
        panel.isReleasedWhenClosed=false
        panel.delegate=self
        let view=WKWebView(frame:.zero,configuration:configuration(services:true))
        view.navigationDelegate=self
        view.uiDelegate=self
        panel.contentView=view
        servicesWeb=view
        servicesWindow=panel
        view.loadFileURL(index,allowingReadAccessTo:resources.appendingPathComponent("Web",isDirectory:true))
        panel.center()
        panel.makeKeyAndOrderFront(nil)
    }
    private func openURL(_ raw:String) {
        guard let url=URL(string:raw),url.scheme=="https" || (url.scheme=="http" && ["127.0.0.1","localhost","[::1]","::1"].contains(url.host ?? "")) else{return};NSWorkspace.shared.open(url)
    }
    private func openFile(_ path:String) {
        let url=URL(fileURLWithPath:path).standardizedFileURL;let root=directory.standardizedFileURL.path+"/"
        guard url.path.hasPrefix(root),["rdp","dmg"].contains(url.pathExtension.lowercased()),FileManager.default.fileExists(atPath:url.path) else{fail("拒绝打开非客户端生成的文件。");return}
        if url.pathExtension=="rdp" {openRemoteDesktop(url);return}
        NSWorkspace.shared.open(url)
    }
    private func openRemoteDesktop(_ document:URL) {
        let workspace=NSWorkspace.shared
        let bundleID="com.microsoft.rdc.macos"
        let roots=[URL(fileURLWithPath:"/Applications",isDirectory:true),
                   FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications",isDirectory:true)]
        let installed=roots.flatMap{root in ["Windows App.app","Microsoft Remote Desktop.app"].map{root.appendingPathComponent($0)}}
        let explicitClient=([workspace.urlForApplication(withBundleIdentifier:bundleID)].compactMap{$0}+installed)
            .first{Bundle(url:$0)?.bundleIdentifier==bundleID}
        let showLaunchError: (String)->Void = {[weak self] message in
            self?.reportNativeFailure(.desktopLaunch){[weak self] issue in
                self?.fail(message+(issue.map{"\n问题编号："+$0} ?? ""))
            }
        }
        if let client=explicitClient {
            workspace.open([document],withApplicationAt:client,configuration:NSWorkspace.OpenConfiguration()){_,error in
                if error != nil {DispatchQueue.main.async {showLaunchError("无法打开 Windows App，请确认应用仍可运行。")}}
            }
        }else if let handler=workspace.urlForApplication(toOpen:document),Bundle(url:handler) != nil {
            if !workspace.open(document) {showLaunchError("无法打开远程桌面配置，请检查默认 RDP 客户端。")}
        }else {
            showLaunchError("请先安装支持 RDP 的 macOS 客户端（例如 Windows App），再打开远程桌面。工作用户密码需在远程桌面客户端自行输入。")
        }
    }
    func windowShouldClose(_ sender:NSWindow) -> Bool {
        if sender===servicesWindow {sender.orderOut(nil);return false}
        if sender===gatewayWindow {return !gatewayMutationPending}
        if let active=dialogWindows.last {
            if sender===active.window {
                web.evaluateJavaScript("window.safeCodeCloseNativeDialog?.("+jsString(active.id)+")",completionHandler:nil)
            }else{active.window.makeKeyAndOrderFront(nil)}
            return false
        }
        if guarding || busy {window.orderOut(nil);return false}
        quit();return false
    }
    func windowWillClose(_ notification:Notification) {
        guard let panel=notification.object as? NSWindow,panel===gatewayWindow else{return}
        gatewayWeb?.configuration.userContentController.removeScriptMessageHandler(forName:"native")
        gatewayWeb=nil
        gatewayWindow=nil
        gatewayContext=nil
    }
    func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool)->Bool{showMain();return true}
    func applicationShouldTerminate(_ sender:NSApplication)->NSApplication.TerminateReply {
        if quitting{return .terminateNow};quit();return .terminateCancel
    }
    @objc private func closeKeyWindow(){NSApp.keyWindow?.performClose(nil)}
    @objc private func showMain(){popover.performClose(nil);window.orderFront(nil);dialogHost.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true)}
    @objc private func toggleStatus(){guard let button=statusItem.button else{return};if popover.isShown{popover.performClose(nil)}else{popover.show(relativeTo:button.bounds,of:button,preferredEdge:.minY)}}
    @objc private func reconnect(){request("mount.reconnect"){[weak self] result in if case .failure(let error)=result{self?.fail(error.localizedDescription)}}}
    @objc private func about(){NSApp.orderFrontStandardAboutPanel(options:[.applicationName:"Safe Code",.applicationVersion:safeCodeVersion,.credits:NSAttributedString(string:"macOS 客户端 · SSH、部署与目录挂载\n第三方适配器保持关闭")])}
    @objc private func quit(){
        guard !quitting else{return}
        if gatewayMutationPending {gatewayWindow?.makeKeyAndOrderFront(nil);return}
        request("app.shutdown"){[weak self] result in guard let self=self else{return};switch result{case .success:self.quitting=true;self.stdin?.closeFile();self.backend?.terminate();NSApp.terminate(nil);case .failure(let error):self.showMain();self.fail("尚未安全退出："+error.localizedDescription)}}
    }
    private func updateStatus(_ message:[String:Any]) {
        busy=message["busy"] as? Bool ?? false;let mount=message["mount"] as? [String:Any] ?? [:];let host=message["host"] as? [String:Any] ?? [:]
        let count=mount["count"] as? Int ?? 0;guarding=count>0 || (host["enabled"] as? Bool ?? false) || (message["desktopKeyRunning"] as? Bool ?? false) || (message["desktopSshRunning"] as? Bool ?? false)
        stateText.stringValue=(host["enabled"] as? Bool ?? false) ? "本机控制已启用":mount["message"] as? String ?? "未挂载目录"
        let seconds=Int((mount["durationSeconds"] as? Double ?? 0));durationText.stringValue="\(count) 个目录  ·  \(seconds/3600):\(String(format:"%02d",seconds/60%60)):\(String(format:"%02d",seconds%60))"
        values[0].stringValue=bytes(mount["bytesToServer"] as? Double ?? 0);values[1].stringValue=bytes(mount["bytesToLocal"] as? Double ?? 0);values[2].stringValue="\(mount["completedFiles"] as? Int ?? 0) 次"
        statusItem.button?.toolTip="Safe Code · "+stateText.stringValue
        web.evaluateJavaScript("window.safeCodeMountStatus?.("+json(mount)+")",completionHandler:nil)
    }
    private func bytes(_ n:Double)->String {if n<1024{return "\(Int(n)) B"};if n<1048576{return String(format:"%.1f KB",n/1024)};if n<1073741824{return String(format:"%.1f MB",n/1048576)};return String(format:"%.1f GB",n/1073741824)}
    private func fail(_ message:String){if let smoke=smokeOutput {try? Data(json(["success":false,"error":message]).utf8).write(to:smoke);return};let alert=NSAlert();alert.messageText="Safe Code";alert.informativeText=message;alert.alertStyle = .warning;alert.runModal()}
    func webView(_ webView:WKWebView,decidePolicyFor action:WKNavigationAction,decisionHandler:@escaping(WKNavigationActionPolicy)->Void) {
        guard let url=action.request.url else{decisionHandler(.cancel);return}
        let expected=webView===gatewayWeb ? gatewayPage : index
        if url.isFileURL && url.standardizedFileURL.path==expected.path {decisionHandler(.allow)}else{decisionHandler(.cancel);if action.navigationType == .linkActivated{openURL(url.absoluteString)}}
    }
    func webView(_ webView:WKWebView,createWebViewWith configuration:WKWebViewConfiguration,for navigationAction:WKNavigationAction,windowFeatures:WKWindowFeatures)->WKWebView?{if let url=navigationAction.request.url{openURL(url.absoluteString)};return nil}
    func webView(_ webView:WKWebView,didStartProvisionalNavigation navigation:WKNavigation!) {
        if webView===web {
            requestedDialogs=[]
            reconcileDialogs()
        }
    }
    func webView(_ webView:WKWebView,didFinish navigation:WKNavigation!) {
        guard webView===web,smokeOutput != nil,!didRunSmoke else{return};didRunSmoke=true
        DispatchQueue.main.asyncAfter(deadline:.now()+0.5){[weak self] in self?.runSmoke()}
    }
    private func runSmoke() {
        let script="""
        if(!window.safeCodeNativeReady)throw Error('Native bridge did not load');
        const rpc=(op,args={})=>new Promise((resolve,reject)=>{const id=crypto.randomUUID();const original=window.safeCodeNativeResponse;window.safeCodeNativeResponse=m=>{if(m.id===id){window.safeCodeNativeResponse=original;m.error?reject(Error(m.error)):resolve(m.result)}else original(m)};chrome.webview.postMessage({id,op,args})});
        const title=document.querySelector('.window>.titlebar');
        if(getComputedStyle(title).display!=='none')throw Error('Duplicate macOS title shown');
        if(await rpc('login.restoreAvailable')||await rpc('login.restoreSaved'))throw Error('Isolated preview accessed saved login');
        const prefs=await rpc('services.preferences');
        if(prefs.enabled||prefs.diagnosticsEnabled||prefs.serviceAvailable)throw Error('Unexpected first-run preferences');
        document.querySelector('#notice-screen [data-open-services]').click();
        if(document.querySelector('#services-dialog').open)throw Error('Services opened inside main window');
        const mount=await rpc('mount.status');if(mount.count!==0)throw Error('Unexpected mount');
        const bridge=await rpc('bridge.status');if(bridge.running)throw Error('Adapter unexpectedly running');
        let denied=false;try{await rpc('inspect')}catch{denied=true}if(!denied)throw Error('Unauthenticated operation allowed');
        await rpc('window.resize',{wide:true});
        const waitFor=async (predicate, label)=>{for(let i=0;i<100;i++){if(await predicate())return;await new Promise(resolve=>setTimeout(resolve,40));}throw Error('Native dialog did not reach the expected state: '+label);};
        const waitPanel=async(id,count)=>waitFor(async()=>{
          const state=await rpc('window.smoke.dialog');
          return state.id===id&&state.count===count&&state.native===(count>0)&&state.visible &&
            (!id||document.querySelector('#'+id).classList.contains('native-dialog-front'));
        },id||'main window');
        showScreen('login');document.querySelector('#desktop-key-login').click();
        await waitPanel('desktop-key-dialog',1);
        const keyPanel=await rpc('window.smoke.dialog');
        if(keyPanel.title!=='Safe Code API · Key 接入')throw Error('Incorrect API Key window title');
        const keyDialog=document.querySelector('#desktop-key-dialog');
        if(getComputedStyle(keyDialog.querySelector('.titlebar')).display!=='none')throw Error('Duplicate dialog title shown');
        await waitFor(async()=>{
          const state=await rpc('window.smoke.dialog');
          const form=document.querySelector('#desktop-key-form');
          return Math.abs(state.contentHeight-Math.max(120,Math.ceil(form.getBoundingClientRect().height)))<=1;
        },'API Key window content height');
        const keyField=document.querySelector('#desktop-key-input');
        keyField.value='fixture';
        const confirmation=showAppMessage('离线确认测试',{cancel:'取消'});
        await waitPanel('app-message-dialog',2);
        const message=document.querySelector('#app-message-dialog');
        message.addEventListener('cancel',event=>event.preventDefault(),{once:true});
        await rpc('window.smoke.closeDialog');
        if(!message.open)throw Error('Native close ignored cancellation guard');
        await rpc('window.smoke.closeDialog');
        if(await confirmation)throw Error('Native close confirmed instead of cancelling');
        await waitPanel('desktop-key-dialog',1);
        if(keyField.value!=='fixture')throw Error('Parent form state was lost');
        keyField.value='';
        await rpc('window.smoke.closeDialog');
        await waitPanel('',0);

        // Guides and services already have dedicated windows. Exercise all remaining dialog layouts.
        const all=[...document.querySelectorAll('dialog')].filter(d=>!['services-dialog','desktop-key-dialog','app-message-dialog'].includes(d.id));
        for(const dialog of all){
          dialog.showModal();
          await waitPanel(dialog.id,1);
          const state=await rpc('window.smoke.dialog');
          if(state.title!==dialog.querySelector('.titlebar .app-name').textContent.trim()&&state.title!=='Safe Code')throw Error('Wrong title: '+dialog.id);
          await rpc('window.smoke.closeDialog');
          await waitPanel('',0);
        }
        document.querySelector('#claude-desktop-dialog').showModal();
        await waitPanel('claude-desktop-dialog',1);
        await rpc('window.gateway.open');
        await waitFor(async()=>{const state=await rpc('window.smoke.gateway');return state.ready&&state.visible;},'gateway window');
        const gateway=await rpc('window.smoke.gateway',{move:true});
        if(!gateway.native||!gateway.landscape||!gateway.moved||!gateway.parentUnmoved)throw Error('Gateway window is not independently movable');
        if(!await rpc('window.smoke.gateway',{close:true}))throw Error('Gateway window did not close');
        await waitPanel('claude-desktop-dialog',1);
        await rpc('window.smoke.closeDialog');
        await waitPanel('',0);
        showScreen('login');document.querySelector('#ip').value='127.0.0.1';document.querySelector('#ip').dispatchEvent(new Event('input'));document.querySelector('#username').value='';document.querySelector('#password').value='';await document.querySelector('#login-form').onsubmit({preventDefault(){}});switchMode('maintenance');
        if(!document.querySelector('#developer-preview'))throw Error('Preview was not explicitly labeled');
        if(!document.querySelector('#codex-launch').disabled||!document.querySelector('#bridge-start').disabled)throw Error('Windows adapter enabled on Mac');
        if(document.querySelector('.host-risk small').textContent.includes('Windows'))throw Error('Windows permission copy retained');
        if(document.body.innerText.includes('客户端核心模块加载失败'))throw Error('Startup fallback displayed');
        return {success:true,nativeBridge:true,readOnlyStartup:true,unauthenticatedRejected:denied,nativeDialogWindows:all.length+2,nestedNativeDialogs:true,gatewayWindow:true,windowsAdapterDisabled:true,platform:window.__safeCodePlatform};
        """
        web.callAsyncJavaScript(script,arguments:[:],in:nil,in:.page){[weak self] result in
            guard let self=self,let output=self.smokeOutput else{return}
            switch result {
            case .success(let value):
                guard let view=self.servicesWeb,self.servicesWindow?.title=="更新与反馈" else{
                    self.fail("Services native window did not open");self.quit();return
                }
                let check="""
                await window.safeCodeRefreshPreferences();
                const dialog=document.querySelector('#services-dialog');
                if(!dialog.open||getComputedStyle(dialog.querySelector('.titlebar')).display!=='none')throw Error('Services native title layout failed');
                if(document.querySelector('#settings-usage-consent').checked||document.querySelector('#settings-diagnostics-consent').checked)throw Error('Unexpected default checkmarks');
                if(!document.querySelector('#usage-recipient').textContent.includes('目前不会上传'))throw Error('Recipient status unavailable');
                if(!document.querySelector('#feedback-send').disabled)throw Error('Isolated feedback must remain disabled');
                return true;
                """
                view.callAsyncJavaScript(check,arguments:[:],in:nil,in:.page){[weak self] result in
                    guard let self=self else{return}
                    if case .failure(let error)=result {self.fail(error.localizedDescription);self.quit();return}
                    var report=value as? [String:Any] ?? [:];report["servicesNativeWindow"]=true
                    try? Data(json(report).utf8).write(to:output)
                    self.web.takeSnapshot(with:nil){image,error in if let image=image,let tiff=image.tiffRepresentation,let bitmap=NSBitmapImageRep(data:tiff),let data=bitmap.representation(using:.png,properties:[:]){try? data.write(to:output.appendingPathExtension("png"))};self.quit()}
                }
            case .failure(let error):
                let detail = (error as NSError).userInfo["WKJavaScriptExceptionMessage"] as? String
                self.fail(detail ?? error.localizedDescription)
                self.quitting=true;self.backend?.terminate();NSApp.terminate(nil)
            }
        }
    }
}
@main
struct SafeCodeApp {
    @MainActor static func main() {
        let app=NSApplication.shared
        let delegate=AppDelegate()
        app.setActivationPolicy(.regular)
        app.delegate=delegate
        withExtendedLifetime(delegate) {app.run()}
    }
}
