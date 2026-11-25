import AppKit
import WebKit
import Security
import UserNotifications

private let bundleID = "org.safecode.desktop"
private let safeCodeVersion = "0.4.16"
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
    private let popover = NSPopover()
    private var trayWeb: WKWebView!
    private var gatewayWeb: WKWebView?
    private var gatewayPage: URL { resources.appendingPathComponent("Web/web/gateway.html").absoluteURL }
    private var capturingTray = false
    private var trayReady = false
    private var trayState: [String:Any] = ["apiMode":false]
    private var trayPage: URL { resources.appendingPathComponent("Web/web/tray.html") }
    private var quitting = false, shutdownPending = false, ready = false
    private let notifications = DesktopNotifications()
    private var directory = URL(fileURLWithPath:NSHomeDirectory()).appendingPathComponent("Library/Application Support/Safe Code", isDirectory:true)
    private var resources: URL { Bundle.main.resourceURL! }
    private var index: URL { resources.appendingPathComponent("Web/web/index.html") }
    private func isAppPage(_ url: URL) -> Bool {
        url.isFileURL && url.standardizedFileURL.path == index.standardizedFileURL.path
    }
    private var smokeOutput: URL?
    private var captureSmokeScreenshots: Bool { ProcessInfo.processInfo.arguments.contains("--smoke-screenshots") }
    private var servicesWindow: NSWindow?
    private var servicesWeb: WKWebView?
    private var gatewayWindow: NSWindow?
    private var gatewayPanel: GatewayPanel?
    private var gatewayContext: String?
    private var gatewayMutationPending = false
    private var gatewayOpening = false
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
    private var hasSavedLogin: Bool { hasLocalFile("login.aes") || hasLocalFile("login.aes.previous") }
    private var hasDesktopKey: Bool { hasLocalFile("desktop-key.state") }
    private var hasEncryptedData: Bool {
        if hasSavedLogin || hasDesktopKey || hasLocalFile("desktop-key-list.state") || hasLocalFile("desktop-ssh.aes") { return true }
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
            for event in [NSWorkspace.willSleepNotification,NSWorkspace.didWakeNotification] {
                NSWorkspace.shared.notificationCenter.addObserver(forName:event,object:nil,queue:.main) { [weak self] _ in
                    Task { @MainActor in self?.request("desktopKey.invalidate") { _ in } }
                }
            }
            makeMenu(); makeWindow(); makeStatus()
            if !isolated { notifications.configure { [weak self] in self?.showMain() } }
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
        app.addItem(.separator());app.addItem(withTitle:"安全退出",action:#selector(quit),keyEquivalent:"q").target=self
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
    private func configuration(services: Bool=false) -> WKWebViewConfiguration {
        let config=WKWebViewConfiguration();config.websiteDataStore = .nonPersistent()
        config.preferences.javaScriptCanOpenWindowsAutomatically=false
        config.userContentController.add(self,name:"native")
        config.userContentController.addUserScript(WKUserScript(source:shim,injectionTime:.atDocumentStart,forMainFrameOnly:true))
        let flag=services ? "window.__safeCodeServicesWindow=true" : "window.__safeCodeNativeDialogs=true"
        config.userContentController.addUserScript(WKUserScript(source:flag,injectionTime:.atDocumentStart,forMainFrameOnly:true))
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
        if let button = statusItem.button {
            if let icon = NSImage(systemSymbolName:"terminal",accessibilityDescription:"Safe Code") {
                icon.isTemplate = true
                button.image = icon
            } else {
                button.title = "SC"
            }
            button.toolTip = "Safe Code"
            button.setAccessibilityLabel("Safe Code")
            button.target = self
            button.action = #selector(toggleStatus)
            button.sendAction(on:[.leftMouseUp, .rightMouseUp])
        }
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        config.userContentController.add(self,name:"tray")
        trayWeb = WKWebView(frame:NSRect(x:0,y:0,width:388,height:220),configuration:config)
        trayWeb.navigationDelegate = self
        let controller = NSViewController()
        controller.view = trayWeb
        popover.contentViewController = controller
        popover.behavior = .transient
        popover.contentSize = trayWeb.frame.size
        trayWeb.loadFileURL(trayPage,allowingReadAccessTo:trayPage)
    }
    private func renderTray(opened:Bool = false) {
        guard !capturingTray, trayReady, popover.isShown else { return }
        trayWeb.evaluateJavaScript("window.trayStatus("+json(trayState)+","+(opened ? "true":"false")+")")
    }
    private func trayAction(_ args:[String:Any]) {
        switch args["op"] as? String {
        case "ready": trayReady = true; renderTray(opened:true)
        case "size":
            if let h = args["height"] as? CGFloat, h.isFinite {
                popover.contentSize = NSSize(width:388,height:min(max(120,h),NSScreen.main?.visibleFrame.height ?? 655))
            }
        case "home": showMain()
        case "close": popover.performClose(nil)
        case "quit": quit()
        case "reconnect": reconnect()
        case "keys","balance":
            guard !capturingTray, popover.isShown, trayState["apiMode"] as? Bool == true, let generation = args["generation"] as? Int else { return }
            let listing = args["op"] as? String == "keys"
            let index = args["index"] as? Int ?? -1
            let key = args["id"] as? String ?? ""
            let reply: (Result<Any,Error>)->Void = { [weak self] result in
                guard let self else { return }
                let callback = listing ? "trayKeys(\(generation),":"trayBalance(\(generation),\(index),"
                let value: Any
                switch result {
                case .success(let data): value=data
                case .failure: value=listing && self.encryptionKey == nil && !self.isolated
                    ? ["locked":true] as Any : NSNull()
                }
                self.trayWeb.evaluateJavaScript(callback+json(value)+")")
            }
            if listing && args["interactive"] as? Bool == true && !isolated {
                withUnlockedSecrets(diagnostic:.desktopKeyRestore,reply:reply) {
                    self.request("desktopKey.list",completion:$0)
                }
            } else {
                request(listing ? "desktopKey.list":"desktopKey.savedBalance",listing ? [:]:["id":key],completion:reply)
            }
        default: break
        }
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
    private func request(_ op:String,_ args:[String:Any]=[:],deadline:(seconds:TimeInterval,message:String)?=nil,completion:@escaping(Result<Any,Error>)->Void) {
        let id="native-"+UUID().uuidString;completions[id]=completion;send(["id":id,"op":op,"args":args])
        if let deadline {
            DispatchQueue.main.asyncAfter(deadline:.now()+deadline.seconds){[weak self] in
                self?.completions.removeValue(forKey:id)?(.failure(AppError.message(deadline.message)))
            }
        }
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
        if message.webView === trayWeb {
            if message.name == "tray", message.frameInfo.isMainFrame, message.frameInfo.request.url?.standardizedFileURL == trayPage.standardizedFileURL,
               let args = message.body as? [String:Any] { trayAction(args) }
            return
        }
        if let view=message.webView,view===gatewayWeb {
            guard message.frameInfo.isMainFrame,let url=message.frameInfo.request.url,isGatewayPage(url),
                  let body=message.body as? [String:Any],let id=body["id"] as? String,
                  let op=body["op"] as? String,let args=body["args"] as? [String:Any] else{return}
            if op=="copy",let value=args["text"] as? String {
                NSPasteboard.general.clearContents()
                answer(id,NSPasteboard.general.setString(value,forType:.string),in:view)
            } else if op=="safeCodeApi.manage",let action=args["action"] as? String {
                manageGateway(action,args){[weak self,weak view] result in
                    guard let self,let view,view===self.gatewayWeb else{return}
                    switch result {
                    case .success(let value):self.answer(id,value,in:view)
                    case .failure(let error):self.answer(id,error:error.localizedDescription,in:view)
                    }
                }
            }
            return
        }
        guard message.frameInfo.isMainFrame,let source=message.frameInfo.request.url,isAppPage(source),let body=message.body as? [String:Any] else{return}
        guard let id=body["id"] as? String,let op=body["op"] as? String,let args=body["args"] as? [String:Any] else{return}
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
        case "desktopKey.list","desktopKey.save":
            withUnlockedSecrets(replyID:id,diagnostic:.desktopKeyRestore){self.request(op,args,completion:$0)}
        case "account.draft.save","desktopKey.enable":
            withUnlockedSecrets(replyID:id,diagnostic:op=="desktopKey.enable" ? .desktopKeyEnable : .accountCustomization){self.request(op,args,completion:$0)}
        case "desktopKey.status" where smokeOutput != nil:
            answer(id,["configured":false,"running":false])
        case "desktopKey.status","desktopKey.disable":
            if hasDesktopKey && (op=="desktopKey.disable" || args["interactive"] as? Bool == true) {
                withUnlockedSecrets(replyID:id,diagnostic:op=="desktopKey.disable" ? .desktopKeyDisable : .desktopKeyRestore){self.request(op,args,completion:$0)}
            }else{send(body)}
        case "claudeDesktop.open","claudeDesktop.reopen":
            openClaudeDesktop(replyID:id,restart:op=="claudeDesktop.reopen")
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
        case "window.smoke.gatewayTab" where smokeOutput != nil:
            if let index=args["index"] as? Int,(0...2).contains(index) {gatewayPanel?.selectTab(index)}
            answer(id,true)
        case "window.smoke.screenshot" where captureSmokeScreenshots && smokeOutput != nil:
            saveSmokeScreenshot(args["name"] as? String ?? "", replyID:id)
        case "window.smoke.gateway" where smokeOutput != nil:
            inspectGatewayWindow(id,args:args)
        case "window.fitDialog":
            if let active=dialogWindows.last,active.id==args["id"] as? String,
               active.id != "client-window",
               let height=args["height"] as? Double,height.isFinite {
                let current=active.window.contentRect(forFrameRect:active.window.frame)
                let screen=active.window.screen ?? window.screen
                let screenHeight=screen?.visibleFrame.height ?? 720
                let fitted=max(active.window.contentMinSize.height,min(height,screenHeight-70))
                if abs(current.height-fitted)>1 {
                    active.window.setContentSize(NSSize(width:current.width,height:fitted))
                    if let screen {
                        let original=active.window.frame
                        let constrained=active.window.constrainFrameRect(original,to:screen)
                        if constrained.origin != original.origin {active.window.setFrameOrigin(constrained.origin)}
                    }
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
        case "window.resize":
            let wide=args["wide"] as? Bool ?? false
            let api=args["layout"] as? String == "api"
            window.setContentSize(NSSize(width:api ? 660:(wide ? 740:360),height:api ? 495:(wide ? 634:536)))
            window.center();answer(id,true)
        case "window.drag":if let event=NSApp.currentEvent,event.type == .leftMouseDown {window.performDrag(with:event)};answer(id,true)
        case "window.minimize":window.miniaturize(nil);answer(id,true)
        case "window.maximize":window.zoom(nil);answer(id,true)
        case "window.close":answer(id,true);window.performClose(nil)
        case "window.quit":answer(id,true);quit()
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
        let focusGateway=requestedDialogs.isEmpty && dialogWindows.last?.id=="claude-desktop-dialog" && gatewayWindow?.isVisible==true
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
            panel.contentMinSize=NSSize(width:min(size.width,320),height:min(size.height,spec.id=="client-window" ? 220:120))
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
        if focusGateway { gatewayWindow?.makeKeyAndOrderFront(nil) }
        else {
            active.makeKeyAndOrderFront(nil)
            active.makeFirstResponder(web)
        }
    }
    private func openClaudeDesktop(replyID id:String,restart:Bool) {
        let bundleID="com.anthropic.claudefordesktop"
        let workspace=NSWorkspace.shared
        let candidates=[workspace.urlForApplication(withBundleIdentifier:bundleID),
                        URL(fileURLWithPath:"/Applications/Claude.app"),
                        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications/Claude.app")]
        guard let url=candidates.compactMap({$0}).first(where:{Bundle(url:$0)?.bundleIdentifier==bundleID}) else {
            let download=URL(string:"https://claude.com/download")!
            if workspace.open(download) {
                answer(id,["installationRequired":true,"message":"未找到 Claude Desktop，已打开官方下载页。安装后请回到 SafeCode 再次打开。"])
            } else {answer(id,error:"未找到 Claude Desktop，请在浏览器打开 https://claude.com/download")}
            return
        }

        func launch() {
            request("claudeDesktop.prepare",deadline:(30,"检查本机网关超时，请确认 SafeCode 接入状态。")){result in
                guard case .success(let value)=result,let since=value as? NSNumber else {
                    if case .failure(let error)=result {self.answer(id,error:error.localizedDescription)}
                    else {self.answer(id,error:"未能确认 SafeCode 网关状态。")}
                    return
                }
                let ready=Date(timeIntervalSince1970:since.doubleValue/1000)
                let apps=NSRunningApplication.runningApplications(withBundleIdentifier:bundleID).filter{!$0.isTerminated}
                if apps.contains(where:{($0.launchDate ?? .distantPast)<ready}) {
                    self.answer(id,error:"Claude Desktop 在本次网关启用前就已运行。请按 ⌘Q 完全退出，再点击打开以载入当前配置。")
                    return
                }
                workspace.openApplication(at:url,configuration:NSWorkspace.OpenConfiguration()){app,error in
                    DispatchQueue.main.async {
                        guard error==nil,app?.bundleIdentifier==bundleID,app?.isTerminated==false else {
                            self.answer(id,error:"已找到 Claude Desktop，但 macOS 未能启动它。请在 Finder 中检查应用是否可打开。")
                            return
                        }
                        self.answer(id,"已打开 Claude Desktop。")
                    }
                }
            }
        }
        let apps=NSRunningApplication.runningApplications(withBundleIdentifier:bundleID).filter{!$0.isTerminated}
        guard restart && !apps.isEmpty else {launch();return}
        request("claudeDesktop.prepare",deadline:(30,"检查本机网关超时，请确认 SafeCode 接入状态。")){result in
            guard case .success=result else {self.answer(id,with:result);return}
            for app in apps {_=app.terminate()}
            let deadline=Date().addingTimeInterval(15)
            @MainActor func waitForExit() {
                if apps.allSatisfy({$0.isTerminated}) {launch()}
                else if Date()>=deadline {self.answer(id,error:"请按 ⌘Q 完全退出 Claude Desktop，再点击「打开 Claude Desktop」。")}
                else {DispatchQueue.main.asyncAfter(deadline:.now()+0.25){waitForExit()}}
            }
            waitForExit()
        }
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
        withUnlockedSecrets(diagnostic:diagnostic,reply:{ [weak self] in self?.answer(replyID,with:$0) },perform:perform)
    }
    private func withUnlockedSecrets(diagnostic: NativeDiagnosticOperation,reply:@escaping (Result<Any,Error>)->Void,perform: @escaping (@escaping (Result<Any,Error>)->Void)->Void) {
        guard !isolated else {reply(.failure(AppError.message("隔离预览不会访问钥匙串。")));return}
        let replyToUnlockFailure: (Error)->Void = { [weak self] error in
            guard let self=self else{return}
            let cancelled: Bool
            if case .keychainCancelled = error as? AppError {cancelled=true}else{cancelled=false}
            self.reportNativeFailure(diagnostic,cancelled:cancelled){ issue in
                reply(.failure(AppError.message(error.localizedDescription+(issue.map{"\n问题编号："+$0} ?? ""))))
            }
        }
        if encryptionKey != nil {perform(reply);return}
        guard !unlockingSecrets else {reply(.failure(AppError.message("正在等待钥匙串授权。")));return}
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
        guard !gatewayOpening else{answer(id,error:"网关管理正在打开，请稍候。");return}
        if smokeOutput != nil {presentGateway(context:nil);answer(id,true);return}
        gatewayOpening=true
        request("safeCodeApi.context",deadline:(33,"打开网关管理超时。")){[weak self] result in
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
        let size=NSSize(width:width,height:min(670,visible.height-36,width * 0.72))
        let panel=NSWindow(contentRect:NSRect(origin:.zero,size:size),styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false)
        panel.title="Safe Code API · 网关管理"
        panel.contentMinSize=NSSize(width:min(805,size.width),height:min(400,size.height))
        panel.isReleasedWhenClosed=false
        panel.delegate=self
        let content=GatewayPanel(openDirectory:{[weak self] in self?.showGatewayDirectory()}) { [weak self] action,args,reply in
            guard let self else {return}
            self.manageGateway(action,args,reply:reply)
        }
        panel.contentViewController=content
        gatewayContext=context
        gatewayPanel=content
        gatewayWindow=panel
        panel.setFrameOrigin(NSPoint(x:visible.midX-panel.frame.width/2,y:visible.midY-panel.frame.height/2))
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(nil)
        content.start()
    }
    private func isGatewayPage(_ url:URL) -> Bool {
        guard url.isFileURL,url.standardizedFileURL.path==gatewayPage.standardizedFileURL.path,url.query==nil else{return false}
        guard let fragment=url.fragment else{return true}
        return fragment=="directory" || fragment=="gateways" || fragment.range(of:"^gateway=[a-f0-9]{32}$",options:.regularExpression) != nil
    }
    private func showGatewayDirectory() {
        guard let panel=gatewayWindow,!gatewayMutationPending else{return}
        let view=WKWebView(frame:.zero,configuration:configuration())
        view.navigationDelegate=self
        gatewayPanel?.stop()
        gatewayPanel=nil
        gatewayWeb=view
        panel.contentViewController=nil
        panel.contentView=view
        var address=URLComponents(url:gatewayPage,resolvingAgainstBaseURL:true)!
        address.fragment="directory"
        view.loadFileURL(address.url!,allowingReadAccessTo:gatewayPage.deletingLastPathComponent())
    }
    private func manageGateway(_ action:String,_ args:[String:Any],reply:@escaping GatewayPanel.Reply) {
        if smokeOutput != nil {
            switch action {
            case "multi.list":
                reply(.success(["value":["self":["id":String(repeating:"a",count:32),"address":"192.0.2.1"],"children":[]]]))
            case "list":
                let key: [String: Any] = ["id":String(repeating:"a",count:64),"note":"工作笔记本","enabled":true,"rpm":0,
                    "requests":42,"tokens":243810,"cost_nanos":104_300_000,"last_used":1795225500]
                let event: [String: Any] = ["key_id":key["id"] ?? "", "model":"claude-example", "at":1795225500,
                    "outcome":"success", "status":200, "wait_ms":82, "duration_ms":1600, "tokens":18]
                let model: [String: Any] = ["model":"claude-example", "requests":42, "failures":2, "tokens":243810, "cost_nanos":104_300_000]
                var fixture: [String: Any] = ["gatewayRevision":27,"gatewayUpdateAvailable":true,"keys":[key],"key_policy_supported":true,"active_count":0,"waiting":0,"concurrency":0,"concurrency_text":"0",
                    "billing":["cost_nanos":104_300_000],"origin":"https://gateway.example.test","recent":[event],"models":[model]]
                if let period=args["analytics"] as? String {
                    let step=["1h":300,"24h":3600,"7d":86400][period] ?? 300
                    let count=["1h":12,"24h":24,"7d":7][period] ?? 12
                    let timeline=(0..<count).map { index in
                        ["bucket":index,"requests":(index*3+1)%7,"tokens":(index*1783+1500)%9200,
                         "failed":index==3 ? 2:0,"limited":index==5 ? 1:0]
                    }
                    let measure: [String: Any] = ["samples":42,"average_ms":1600,"p95_ms":2250,"histogram":[0,0,3,37,2,0,0]]
                    fixture["analytics"]=["period":period,"count":count,"step":step,"start":1795225500-count*step,"timeline":timeline,
                        "latency":["duration_ms":measure,"wait_ms":measure],"latency_limits":[250,500,1000,2500,5000,10000],
                        "outcomes":[["outcome":"success","status":200,"requests":39],["outcome":"failed","status":502,"requests":2],
                                    ["outcome":"limited","status":429,"requests":1]]]
                }
                reply(.success(fixture))
            case "refresh_subscription": reply(.success(["state":"available","windows":["five_hour":["used":20],"seven_day":["used":36]]]))
            default: reply(.failure(AppError.message("未提供该预览操作。")))
            }
            return
        }
        guard let context=gatewayContext else{reply(.failure(AppError.message("SSH 会话已变更，请重新打开网关管理。")));return}
        guard !gatewayMutationPending else{reply(.failure(AppError.message("请等待当前网关操作完成。")));return}
        let modifies=["issue","update","unbind","revoke","delete","set_port","set_origin","set_concurrency","set_prompt_review","set_key_queue","refresh_ticket","multi.activate","multi.create","multi.claim","multi.remove"].contains(action)
        if modifies {gatewayMutationPending=true}
        var options=args
        options["action"]=action
        options["context"]=context
        request("safeCodeApi.manage",options){[weak self] result in
            if modifies {self?.gatewayMutationPending=false}
            reply(result)
        }
    }
    private func inspectGatewayWindow(_ id:String,args:[String:Any]) {
        guard let panel=gatewayWindow else{answer(id,["visible":false]);return}
        if args["close"] as? Bool == true {panel.performClose(nil);answer(id,gatewayWindow==nil);return}
        if args["directory"] as? Bool == true {showGatewayDirectory();answer(id,true);return}
        if args["bindingChoices"] as? Bool == true, let view=gatewayWeb {
            let check = "document.querySelector('dialog.mg-dialog[open] [data-step=home]:not([hidden])')?.querySelectorAll('[data-next]').length === 2"
            view.evaluateJavaScript(check) { [weak self] value,_ in self?.answer(id,value as? Bool ?? false) }
            return
        }
        guard let content=gatewayPanel else{answer(id,["visible":panel.isVisible]);return}
        if args["createKey"] as? Bool == true {content.createKey()}
        if args["cancelKey"] as? Bool == true,let sheet=panel.attachedSheet {panel.endSheet(sheet,returnCode:.alertSecondButtonReturn)}
        let previous=dialogHost.frame
        let gatewayOrigin=panel.frame.origin
        if args["move"] as? Bool == true {panel.setFrameOrigin(NSPoint(x:panel.frame.minX+20,y:panel.frame.minY))}
        answer(id,["visible":panel.isVisible,"ready":content.ready,"keyEditor":panel.attachedSheet != nil,
                   "native":panel.styleMask.contains(.titled)&&panel.isMovable&&panel.contentViewController===content,
                   "landscape":panel.frame.width>panel.frame.height,"moved":panel.frame.origin != gatewayOrigin,
                   "parentUnmoved":dialogHost.frame==previous])
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
        guard let url=URL(string:raw),url.scheme=="https" || (url.scheme=="http" && ["127.0.0.1","localhost","[::1]","::1"].contains(url.host ?? "")) else{return}
        if url.host?.lowercased()=="claude.ai" && url.path.hasPrefix("/settings/usage"){return}
        NSWorkspace.shared.open(url)
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
        guard sender === window else { return true }
        window.orderOut(nil)
        if !isolated {
            notifications.post(id:"main-window-hidden", title:"Safe Code 已转入后台",
                               body:"点击菜单栏中的 Safe Code 图标，可查看状态或打开主界面。")
        }
        return false
    }
    func windowWillClose(_ notification:Notification) {
        guard let panel=notification.object as? NSWindow,panel===gatewayWindow else{return}
        gatewayPanel?.stop()
        gatewayPanel=nil
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
    @objc private func toggleStatus(){
        guard let button=statusItem.button else{return}
        if popover.isShown { popover.performClose(nil) }
        else {
            popover.show(relativeTo:button.bounds,of:button,preferredEdge:.minY)
            renderTray(opened:true)
        }
    }
    @objc private func reconnect(){request("mount.reconnect"){[weak self] result in if case .failure(let error)=result{self?.fail(error.localizedDescription)}}}
    @objc private func about(){NSApp.orderFrontStandardAboutPanel(options:[.applicationName:"Safe Code",.applicationVersion:safeCodeVersion,.credits:NSAttributedString(string:"macOS 客户端 · SSH、部署与目录挂载\n第三方适配器保持关闭")])}
    @objc private func quit(){
        guard !quitting && !shutdownPending else{return}
        if gatewayMutationPending {gatewayWindow?.makeKeyAndOrderFront(nil);return}
        shutdownPending = true
        request("app.shutdown"){[weak self] result in guard let self=self else{return};self.shutdownPending=false;switch result{case .success:self.quitting=true;self.stdin?.closeFile();self.backend?.terminate();NSApp.terminate(nil);case .failure(let error):self.showMain();self.fail("尚未安全退出："+error.localizedDescription)}}
    }
    private func updateStatus(_ message:[String:Any]) {
        let mount=message["mount"] as? [String:Any] ?? [:]
        let host=message["host"] as? [String:Any] ?? [:]
        let apiMode=message["apiMode"] as? Bool ?? false
        trayState=["mount":mount,"host":["enabled":host["enabled"] as? Bool ?? false],
                  "running":message["desktopKeyRunning"] as? Bool ?? false,"apiMode":apiMode,"connection":message["connection"] ?? NSNull(),
                  "startedAt":message["startedAt"] ?? NSNull(),"exiting":shutdownPending]
        statusItem.button?.toolTip=apiMode ? "Safe Code · API Key 接入":"Safe Code · "+(mount["message"] as? String ?? "未挂载目录")
        renderTray()
        web.evaluateJavaScript("window.safeCodeMountStatus?.("+json(mount)+")",completionHandler:nil)
    }
    private func fail(_ message:String){if let smoke=smokeOutput {try? Data(json(["success":false,"error":message]).utf8).write(to:smoke);return};let alert=NSAlert();alert.messageText="Safe Code";alert.informativeText=message;alert.alertStyle = .warning;alert.runModal()}
    func webView(_ webView:WKWebView,decidePolicyFor action:WKNavigationAction,decisionHandler:@escaping(WKNavigationActionPolicy)->Void) {
        guard let url=action.request.url else{decisionHandler(.cancel);return}
        if webView===gatewayWeb {decisionHandler(isGatewayPage(url) ? .allow:.cancel);return}
        if webView === trayWeb {
            decisionHandler(url.standardizedFileURL == trayPage.standardizedFileURL ? .allow:.cancel)
            return
        }
        if isAppPage(url) {
            decisionHandler(.allow)
        } else {
            decisionHandler(.cancel)
            if action.navigationType == .linkActivated { openURL(url.absoluteString) }
        }
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
    private func saveSmokeScreenshot(_ name: String, replyID: String) {
        let views=["notice","login","deployment","maintenance","homepage","desktop-access","gateway","gateway-analysis","gateway-requests","gateway-binding","status","status-key"]
        guard let output=smokeOutput, views.contains(name) ||
            (name.hasPrefix("dialog-") && name.count <= 48 && name.utf8.allSatisfy { $0 == 45 || (97...122).contains($0) }) else {
            answer(replyID,error:"Invalid smoke screenshot name");return
        }
        let target=output.deletingLastPathComponent().appendingPathComponent(name+".png")
        let save: (NSBitmapImageRep?) -> Void = { [weak self] bitmap in
            guard let self,let data=bitmap?.representation(using:.png,properties:[:]) else {
                self?.answer(replyID,error:"No screenshot pixels");return
            }
            do {try data.write(to:target);self.answer(replyID,true)}
            catch {self.answer(replyID,error:error.localizedDescription)}
        }
        if name == "gateway-binding", let view=gatewayWeb {
            view.takeSnapshot(with:nil) { image,_ in
                save(image?.tiffRepresentation.flatMap(NSBitmapImageRep.init(data:)))
            }
        } else if name.hasPrefix("status") {
            if !popover.isShown { toggleStatus() }
            capturingTray = true
            let fixture=output.deletingLastPathComponent().appendingPathComponent(name+".json")
            let sample=(try? Data(contentsOf:fixture)).flatMap { try? JSONSerialization.jsonObject(with:$0) } as? [String:Any]
            let script = """
                window.trayStatus(\(json(sample?["state"] ?? trayState)),true);
                if(apiMode){
                    window.trayKeys(generation,\(json(sample?["keys"] ?? [])));
                    \(json(sample?["balances"] ?? [])).forEach((b,i)=>window.trayBalance(generation,i,b));
                }
                """
            trayWeb.evaluateJavaScript(script){[weak self] _,err in
                guard let self else {return}
                if let err {self.capturingTray=false;self.answer(replyID,error:err.localizedDescription);return}
                DispatchQueue.main.asyncAfter(deadline:.now()+0.15) {
                    self.trayWeb.takeSnapshot(with:nil){image,_ in
                        self.popover.performClose(nil)
                        self.capturingTray=false
                        save(image?.tiffRepresentation.flatMap(NSBitmapImageRep.init(data:)))
                    }
                }
            }
        } else if name.hasPrefix("gateway"),let window=gatewayWindow,let content=window.contentView {
            let original = window.frame
            defer {window.setFrame(original,display:true)}
            window.setContentSize(NSSize(width:1100,height:name=="gateway-analysis" ? 1040:670))
            content.layoutSubtreeIfNeeded()
            let bitmap=content.bitmapImageRepForCachingDisplay(in:content.bounds)
            if let bitmap {content.cacheDisplay(in:content.bounds,to:bitmap)}
            save(bitmap)
        } else {
            web.takeSnapshot(with:nil) { image,_ in
                save(image?.tiffRepresentation.flatMap(NSBitmapImageRep.init(data:)))
            }
        }
    }
    private func runSmoke() {
        window.performClose(nil)
        guard !window.isVisible, !shutdownPending, backend?.isRunning == true, let button = statusItem.button else {
            fail("关闭主窗口后未保留菜单栏或后台核心。"); quit(); return
        }
        button.performClick(nil)
        guard popover.isShown else { fail("菜单栏面板未打开。"); quit(); return }
        showMain()
        guard window.isVisible else { fail("未能从菜单栏恢复主窗口。"); quit(); return }
        let script="""
        if(!window.safeCodeNativeReady)throw Error('Native bridge did not load');
        const rpc=(op,args={})=>new Promise((resolve,reject)=>{const id=crypto.randomUUID();const original=window.safeCodeNativeResponse;window.safeCodeNativeResponse=m=>{if(m.id===id){window.safeCodeNativeResponse=original;m.error?reject(Error(m.error)):resolve(m.result)}else original(m)};chrome.webview.postMessage({id,op,args})});
        const title=document.querySelector('.window>.titlebar');
        if(getComputedStyle(title).display!=='none')throw Error('Duplicate macOS title shown');
        if(await rpc('login.restoreAvailable')||await rpc('login.restoreSaved'))throw Error('Isolated preview accessed saved login');
        const prefs=await rpc('services.preferences');
        if(prefs.enabled||prefs.diagnosticsEnabled||prefs.serviceAvailable)throw Error('Unexpected first-run preferences');
        if(capture)await rpc('window.smoke.screenshot',{name:'notice'});
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
        showScreen('login');
        await waitFor(()=>innerWidth===360,'login window width');
        if(capture)await rpc('window.smoke.screenshot',{name:'login'});
        document.querySelector('#desktop-key-login').click();
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
        if(capture)await rpc('window.smoke.screenshot',{name:'dialog-desktop-key'});
        const keyField=document.querySelector('#desktop-key-input');
        keyField.value='fixture';
        const confirmation=showAppMessage('离线确认测试',{cancel:'取消'});
        await waitPanel('app-message-dialog',2);
        if(capture)await rpc('window.smoke.screenshot',{name:'dialog-app-message'});
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
          if(capture)await rpc('window.smoke.screenshot',{name:'dialog-'+dialog.id});
          await rpc('window.smoke.closeDialog');
          await waitPanel('',0);
        }
        if(capture) {
          showScreen('workspace');switchMode('maintenance');
          document.querySelector('#server-ingress').textContent='192.0.2.42';
          document.querySelector('#server-address').textContent='198.51.100.27';
          const username=document.querySelector('#server-user');
          username.textContent='demo';
          document.querySelector('.server-metadata').replaceChildren(username,document.createTextNode(' / Ubuntu 24.04 / SSH'));
          document.querySelector('#resource-cpu').textContent='2.6 %';
          document.querySelector('#resource-memory').textContent='1.2 / 1.9 G';
          document.querySelector('#resource-disk').textContent='18.3 / 29.4 G';
          document.querySelector('#update-login-password').hidden=false;
          document.querySelector('#host-state').textContent='已连接';
          document.querySelector('#claude-desktop-state').textContent='已接入';
          document.querySelector('#claude-gateway-status').textContent='Claude 已接入';
          document.querySelector('#claude-gateway-status').classList.remove('inactive');
          await waitFor(()=>innerWidth===740,'home window width');
          if(getComputedStyle(document.querySelector('.window'),'::after').content!=='none')throw Error('Duplicate macOS border shown');
          if(getComputedStyle(document.querySelector('#public-gateway-footer')).display!=='none')throw Error('Extra gateway footer shown');
          await rpc('window.smoke.screenshot',{name:'maintenance'});
          switchMode('deployment');
          await rpc('window.smoke.screenshot',{name:'deployment'});
          switchMode('maintenance');
          const entry=document.querySelector('#claude-desktop-manage');
          entry.disabled=false;
          entry.textContent='网关设置 · 有更新';
          document.querySelector('.maintenance-panel').scrollTop=10000;
          await rpc('window.smoke.screenshot',{name:'homepage'});
        }
        const desktopDialog=document.querySelector('#claude-desktop-dialog');
        const gatewayForm=document.querySelector('#safe-code-api-form');
        const gatewayButtons=[...gatewayForm.querySelectorAll('.config-footer button')];
        if(gatewayButtons.map(button=>button.textContent.trim()).join(',')!=='故障修复,更新网关,启用,管理网关')
          throw Error('Unexpected gateway actions');
        if(capture){
          gatewayForm.querySelector('#claude-desktop-dialog-state').textContent='公网已启用';
          gatewayForm.querySelector('#safe-code-api-progress').textContent='当前客户端包含新版网关。点击「更新网关」更新 VPS，保留已有 API Key。';
          gatewayButtons[1].hidden=false;
          gatewayButtons[1].disabled=false;
          gatewayButtons[2].textContent='已启用';
          gatewayButtons[2].disabled=true;
          gatewayButtons[3].disabled=false;
        }
        desktopDialog.showModal();
        await waitPanel('claude-desktop-dialog',1);
        await waitFor(async()=>{
          const state=await rpc('window.smoke.dialog');
          return Math.abs(state.contentHeight-Math.max(120,Math.ceil(gatewayForm.getBoundingClientRect().height)))<=1;
        },'Public gateway window content height');
        gatewayButtons[0].disabled=false;
        if(capture)await rpc('window.smoke.screenshot',{name:'desktop-access'});
        gatewayButtons[0].click();
        await waitPanel('gateway-repair-dialog',2);
        const cloudRepair=document.querySelector('#gateway-repair-cloudcli');
        if(cloudRepair.checked)throw Error('CloudCLI repair must be opt-in');
        if(capture)await rpc('window.smoke.screenshot',{name:'dialog-gateway-repair'});
        cloudRepair.click();
        if(!cloudRepair.checked)throw Error('CloudCLI repair cannot be selected');
        document.querySelector('#gateway-repair-back').click();
        await waitPanel('claude-desktop-dialog',1);
        gatewayButtons[0].click();
        await waitPanel('gateway-repair-dialog',2);
        if(cloudRepair.checked)throw Error('Repair selection was reused');
        document.querySelector('#gateway-repair-back').click();
        await waitPanel('claude-desktop-dialog',1);
        await rpc('window.gateway.open');
        await waitFor(async()=>{const state=await rpc('window.smoke.gateway');return state.ready&&state.visible;},'gateway window');
        await rpc('window.smoke.gateway',{createKey:true});
        await waitFor(async()=>{const state=await rpc('window.smoke.gateway');return state.keyEditor;},'gateway key editor');
        await rpc('window.smoke.gateway',{cancelKey:true});
        await waitFor(async()=>{const state=await rpc('window.smoke.gateway');return !state.keyEditor;},'gateway key editor closed');
        if(capture){
          await rpc('window.smoke.screenshot',{name:'gateway'});
          await rpc('window.smoke.gatewayTab',{index:1});
          await rpc('window.smoke.screenshot',{name:'gateway-analysis'});
          await rpc('window.smoke.gatewayTab',{index:2});
          await rpc('window.smoke.screenshot',{name:'gateway-requests'});
          await rpc('window.smoke.gatewayTab',{index:0});
        }
        const gateway=await rpc('window.smoke.gateway',{move:true});
        if(!gateway.native||!gateway.landscape||!gateway.moved||!gateway.parentUnmoved)throw Error('Gateway window is not independently movable');
        await rpc('window.smoke.gateway',{directory:true});
        await waitFor(()=>rpc('window.smoke.gateway',{bindingChoices:true}),'both gateway binding actions');
        if(capture)await rpc('window.smoke.screenshot',{name:'gateway-binding'});
        if(!await rpc('window.smoke.gateway',{close:true}))throw Error('Gateway window did not close');
        await waitPanel('claude-desktop-dialog',1);
        await rpc('window.smoke.closeDialog');
        await waitPanel('',0);
        showScreen('login');document.querySelector('#ip').value='127.0.0.1';document.querySelector('#ip').dispatchEvent(new Event('input'));document.querySelector('#username').value='';document.querySelector('#password').value='';await document.querySelector('#login-form').onsubmit({preventDefault(){}});switchMode('maintenance');
        if(!document.querySelector('#developer-preview'))throw Error('Preview was not explicitly labeled');
        if(!document.querySelector('#codex-launch').disabled||!document.querySelector('#bridge-start').disabled)throw Error('Windows adapter enabled on Mac');
        if(document.querySelector('.host-risk small').textContent.includes('Windows'))throw Error('Windows permission copy retained');
        if(document.body.innerText.includes('客户端核心模块加载失败'))throw Error('Startup fallback displayed');
        if(capture){
          await rpc('window.smoke.screenshot',{name:'status'});
          await rpc('window.smoke.screenshot',{name:'status-key'});
        }
        return {success:true,nativeBridge:true,readOnlyStartup:true,unauthenticatedRejected:denied,nativeDialogWindows:all.length+2,nestedNativeDialogs:true,gatewayWindow:true,windowsAdapterDisabled:true,platform:window.__safeCodePlatform};
        """
        web.callAsyncJavaScript(script,arguments:["capture":captureSmokeScreenshots],in:nil,in:.page){[weak self] result in
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
                    let finish = {
                        try? Data(json(report).utf8).write(to:output)
                        self.web.takeSnapshot(with:nil){image,_ in
                            if let image,let tiff=image.tiffRepresentation,let bitmap=NSBitmapImageRep(data:tiff),
                               let data=bitmap.representation(using:.png,properties:[:]){
                                try? data.write(to:output.appendingPathExtension("png"))
                            }
                            self.quit()
                        }
                    }
                    if self.captureSmokeScreenshots {
                        view.takeSnapshot(with:nil){image,_ in
                            if let image,let tiff=image.tiffRepresentation,let bitmap=NSBitmapImageRep(data:tiff),
                               let data=bitmap.representation(using:.png,properties:[:]){
                                try? data.write(to:output.deletingLastPathComponent().appendingPathComponent("services.png"))
                            }
                            finish()
                        }
                    }else{finish()}
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
