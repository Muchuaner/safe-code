import AppKit
import WebKit
import Security

private let bundleID = "org.safecode.desktop"
private let safeCodeVersion = "Beta-0.4.4.2"
private enum AppError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case .message(let text) = self { return text }; return nil }
}
private func json(_ value: Any) -> String {
    guard JSONSerialization.isValidJSONObject(value), let data = try? JSONSerialization.data(withJSONObject: value, options: [.fragmentsAllowed, .sortedKeys]), let string = String(data: data, encoding: .utf8) else { return "null" }; return string
}
private func jsString(_ value: String) -> String { String(json([value]).dropFirst().dropLast()) }
private func keychainSecret() throws -> Data {
    let query: [String: Any] = [kSecClass as String:kSecClassGenericPassword, kSecAttrService as String:bundleID, kSecAttrAccount as String:"local-encryption-v1", kSecReturnData as String:true, kSecMatchLimit as String:kSecMatchLimitOne]
    var result: CFTypeRef?; let status = SecItemCopyMatching(query as CFDictionary, &result)
    if status == errSecSuccess, let data = result as? Data, data.count == 32 { return data }
    guard status == errSecItemNotFound else { throw AppError.message("无法读取 macOS 钥匙串（\(status)）。请解锁登录钥匙串后重试。") }
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
    private var guide: NSWindow?
    private var guideWeb: WKWebView?
    private var guideReply: String?
    private var didRunSmoke = false
    private var encryptionKey = Data()
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
            var secret=Data(count:32)
            if !isolated { secret=try keychainSecret() } else { _=secret.withUnsafeMutableBytes{SecRandomCopyBytes(kSecRandomDefault,32,$0.baseAddress!)} }
            encryptionKey=secret;try startBackend(secret:secret)
            web.loadFileURL(index,allowingReadAccessTo:resources.appendingPathComponent("Web",isDirectory:true))
            window.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true)
        } catch { fail(error.localizedDescription); NSApp.terminate(nil) }
    }
    private func makeMenu() {
        let main=NSMenu();let app=NSMenu();let root=NSMenuItem();root.submenu=app;main.addItem(root)
        app.addItem(withTitle:"关于 Safe Code",action:#selector(about),keyEquivalent:"")
        app.addItem(.separator());app.addItem(withTitle:"显示主界面",action:#selector(showMain),keyEquivalent:"0")
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
        document.addEventListener('click',e=>{if(!location.pathname.endsWith('/guide.html')&&!window.safeCodeNativeReady&&!e.target.closest?.('[data-safe-code-startup-close]')){e.preventDefault();e.stopImmediatePropagation();}},true);
        document.addEventListener('DOMContentLoaded',()=>{if(!window.safeCodeNativeReady&&!document.querySelector('#guide-done-btn')&&!location.pathname.endsWith('guide.html')){document.body.innerHTML='<p style="padding:32px;font:14px system-ui">客户端界面未能加载，请检查应用是否完整。</p>';}});
        """ }
    private func configuration(guide: Bool=false) -> WKWebViewConfiguration {
        let config=WKWebViewConfiguration();config.websiteDataStore = .nonPersistent()
        config.preferences.javaScriptCanOpenWindowsAutomatically=false
        config.userContentController.add(self,name:"native")
        config.userContentController.addUserScript(WKUserScript(source:shim,injectionTime:.atDocumentStart,forMainFrameOnly:true))
        if guide, let html=try? String(contentsOf:index,encoding:.utf8) { config.userContentController.addUserScript(WKUserScript(source:"window.__safeCodeGuideHTML="+jsString(html),injectionTime:.atDocumentStart,forMainFrameOnly:true)) }
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
    private func startBackend(secret:Data) throws {
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
                do {try self.startBackend(secret:self.encryptionKey)} catch {self.fail(error.localizedDescription)}
            }
            self.fail("连接核心已退出，加密通道已断开。已尝试恢复核心并撤销残留本机控制授权；请重新连接服务器，检查挂载状态。")
        }}
        try process.run();backend=process
        try stdin?.write(contentsOf:Data((json(["key":secret.base64EncodedString(),"offline":isolated])+"\n").utf8))
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
            case "updateProgress":web.evaluateJavaScript("window.safeCodeUpdateProgress?.(\(message["done"] ?? 0),\(message["total"] ?? 0))",completionHandler:nil)
            case "openUrl":if let raw=message["url"] as? String{openURL(raw)}
            case "openFile":if let raw=message["path"] as? String{openFile(raw)}
            default:break
            }
        }
    }
    private func answer(_ id:String,_ result:Any=NSNull(),error:String?=nil) {
        let response:[String:Any]=["id":id,"result":result,"error":error ?? NSNull()]
        web.evaluateJavaScript("window.safeCodeNativeResponse?.("+json(response)+")",completionHandler:nil)
    }
    func userContentController(_ userContentController: WKUserContentController,didReceive message:WKScriptMessage) {
        guard message.frameInfo.isMainFrame,let source=message.frameInfo.request.url,source.isFileURL,let body=message.body as? [String:Any] else{return}
        if source.standardizedFileURL.path==resources.appendingPathComponent("Web/guide.html").path {
            guard message.webView===guideWeb,let confirmed=body["confirmed"] as? Bool else{return};finishGuide(confirmed);return
        }
        guard message.webView===web,source.standardizedFileURL.path==index.path,let id=body["id"] as? String,let op=body["op"] as? String,let args=body["args"] as? [String:Any] else{return}
        switch op {
        case "window.resize":let wide=args["wide"] as? Bool ?? false;window.setContentSize(NSSize(width:wide ? 740:360,height:wide ? 634:536));window.center();answer(id,true)
        case "window.drag":if let event=NSApp.currentEvent,event.type == .leftMouseDown {window.performDrag(with:event)};answer(id,true)
        case "window.minimize":window.miniaturize(nil);answer(id,true)
        case "window.maximize":window.zoom(nil);answer(id,true)
        case "window.close":answer(id,true);window.performClose(nil)
        case "copy":NSPasteboard.general.clearContents();NSPasteboard.general.setString(args["text"] as? String ?? "",forType:.string);answer(id,true)
        case "pickFolder":let picker=NSOpenPanel();picker.title="选择要挂载到 VPS 的本机目录";picker.canChooseFiles=false;picker.canChooseDirectories=true;picker.allowsMultipleSelection=false;picker.beginSheetModal(for:window){[weak self] result in self?.answer(id,result == .OK ? picker.url?.path ?? "":NSNull())}
        case "guide.open":openGuide(id:id,connecting:args["connecting"] as? Bool ?? false)
        case "mount.status.open":toggleStatus();answer(id,true)
        case "open":openURL(args["url"] as? String ?? "");answer(id,true)
        default:send(body)
        }
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
        if let client=explicitClient {
            workspace.open([document],withApplicationAt:client,configuration:NSWorkspace.OpenConfiguration()){[weak self] _,error in
                if error != nil {DispatchQueue.main.async {self?.fail("无法打开 Windows App，请确认应用仍可运行。")}}
            }
        }else if let handler=workspace.urlForApplication(toOpen:document),Bundle(url:handler) != nil {
            if !workspace.open(document) {fail("无法打开远程桌面配置，请检查默认 RDP 客户端。")}
        }else {
            fail("请先安装支持 RDP 的 macOS 客户端（例如 Windows App），再打开远程桌面。工作用户密码需在远程桌面客户端自行输入。")
        }
    }
    private func openGuide(id:String,connecting:Bool) {
        if guide != nil {guide?.makeKeyAndOrderFront(nil);answer(id,false);return}
        guideReply=id;let g=NSWindow(contentRect:NSRect(x:0,y:0,width:560,height:620),styleMask:[.titled,.closable,.resizable],backing:.buffered,defer:false)
        g.title="Claude 防封与安全避坑指南";g.minSize=NSSize(width:340,height:340);g.delegate=self;g.isReleasedWhenClosed=false
        let w=WKWebView(frame:.zero,configuration:configuration(guide:true));w.navigationDelegate=self;guideWeb=w;g.contentView=w;guide=g
        let url=resources.appendingPathComponent("Web/guide.html");w.loadFileURL(url,allowingReadAccessTo:resources.appendingPathComponent("Web",isDirectory:true));g.center();g.makeKeyAndOrderFront(nil)
        _=connecting // The query controls the existing guide's cancel button.
        if connecting {w.loadFileURL(URL(string:url.absoluteString+"?connecting=1")!,allowingReadAccessTo:resources.appendingPathComponent("Web",isDirectory:true))}
    }
    private func finishGuide(_ confirmed:Bool) {let id=guideReply;guideReply=nil;guide?.orderOut(nil);guideWeb?.configuration.userContentController.removeScriptMessageHandler(forName:"native");guideWeb=nil;guide=nil;if let id=id{answer(id,confirmed)}}
    func windowShouldClose(_ sender:NSWindow) -> Bool {
        if sender===guide {finishGuide(false);return true}
        if guarding || busy {window.orderOut(nil);return false}
        quit();return false
    }
    func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool)->Bool{showMain();return true}
    func applicationShouldTerminate(_ sender:NSApplication)->NSApplication.TerminateReply {
        if quitting{return .terminateNow};quit();return .terminateCancel
    }
    @objc private func showMain(){popover.performClose(nil);window.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true)}
    @objc private func toggleStatus(){guard let button=statusItem.button else{return};if popover.isShown{popover.performClose(nil)}else{popover.show(relativeTo:button.bounds,of:button,preferredEdge:.minY)}}
    @objc private func reconnect(){request("mount.reconnect"){[weak self] result in if case .failure(let error)=result{self?.fail(error.localizedDescription)}}}
    @objc private func about(){NSApp.orderFrontStandardAboutPanel(options:[.applicationName:"Safe Code",.applicationVersion:safeCodeVersion,.credits:NSAttributedString(string:"macOS 客户端 · SSH、部署与目录挂载\n第三方适配器保持关闭")])}
    @objc private func quit(){
        guard !quitting else{return}
        request("app.shutdown"){[weak self] result in guard let self=self else{return};switch result{case .success:self.quitting=true;self.stdin?.closeFile();self.backend?.terminate();NSApp.terminate(nil);case .failure(let error):self.showMain();self.fail("尚未安全退出："+error.localizedDescription)}}
    }
    private func updateStatus(_ message:[String:Any]) {
        busy=message["busy"] as? Bool ?? false;let mount=message["mount"] as? [String:Any] ?? [:];let host=message["host"] as? [String:Any] ?? [:]
        let count=mount["count"] as? Int ?? 0;guarding=count>0 || (host["enabled"] as? Bool ?? false)
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
        let expected=webView===guideWeb ? resources.appendingPathComponent("Web/guide.html"):index
        if url.isFileURL && url.standardizedFileURL.path==expected.path {decisionHandler(.allow)}else{decisionHandler(.cancel);if action.navigationType == .linkActivated{openURL(url.absoluteString)}}
    }
    func webView(_ webView:WKWebView,createWebViewWith configuration:WKWebViewConfiguration,for navigationAction:WKNavigationAction,windowFeatures:WKWindowFeatures)->WKWebView?{if let url=navigationAction.request.url{openURL(url.absoluteString)};return nil}
    func webView(_ webView:WKWebView,didFinish navigation:WKNavigation!) {
        if webView===guideWeb,smokeOutput != nil {DispatchQueue.main.asyncAfter(deadline:.now()+0.6){[weak self] in self?.guideWeb?.evaluateJavaScript("document.querySelector('#guide-done-btn').click()",completionHandler:nil)};return}
        guard webView===web,smokeOutput != nil,!didRunSmoke else{return};didRunSmoke=true
        DispatchQueue.main.asyncAfter(deadline:.now()+0.5){[weak self] in self?.runSmoke()}
    }
    private func runSmoke() {
        let script="""
        if(!window.safeCodeNativeReady)throw Error('Native bridge did not load');
        const rpc=(op,args={})=>new Promise((resolve,reject)=>{const id=crypto.randomUUID();const original=window.safeCodeNativeResponse;window.safeCodeNativeResponse=m=>{if(m.id===id){window.safeCodeNativeResponse=original;m.error?reject(Error(m.error)):resolve(m.result)}else original(m)};chrome.webview.postMessage({id,op,args})});
        const mount=await rpc('mount.status');if(mount.count!==0)throw Error('Unexpected mount');
        const bridge=await rpc('bridge.status');if(bridge.running)throw Error('Adapter unexpectedly running');
        let denied=false;try{await rpc('inspect')}catch{denied=true}if(!denied)throw Error('Unauthenticated operation allowed');
        await rpc('window.resize',{wide:true});const guide=await rpc('guide.open',{connecting:true});if(!guide)throw Error('Native guide failed');
        showScreen('login');document.querySelector('#ip').value='127.0.0.1';document.querySelector('#ip').dispatchEvent(new Event('input'));document.querySelector('#username').value='';document.querySelector('#password').value='';await document.querySelector('#login-form').onsubmit({preventDefault(){}});switchMode('maintenance');
        if(!document.querySelector('#developer-preview'))throw Error('Preview was not explicitly labeled');
        if(!document.querySelector('#bridge-manage').disabled)throw Error('Windows adapter enabled on Mac');
        if(document.querySelector('.host-risk small').textContent.includes('Windows'))throw Error('Windows permission copy retained');
        if(document.body.innerText.includes('客户端核心模块加载失败'))throw Error('Startup fallback displayed');
        return {success:true,nativeBridge:true,readOnlyStartup:true,unauthenticatedRejected:denied,guideWindow:true,windowsAdapterDisabled:true,platform:window.__safeCodePlatform};
        """
        web.callAsyncJavaScript(script,arguments:[:],in:nil,in:.page){[weak self] result in
            guard let self=self,let output=self.smokeOutput else{return}
            switch result {
            case .success(let value):
                try? Data(json(value).utf8).write(to:output)
                self.web.takeSnapshot(with:nil){image,error in if let image=image,let tiff=image.tiffRepresentation,let bitmap=NSBitmapImageRep(data:tiff),let data=bitmap.representation(using:.png,properties:[:]){try? data.write(to:output.appendingPathExtension("png"))};self.quit()}
            case .failure(let error):self.fail(error.localizedDescription);self.quitting=true;self.backend?.terminate();NSApp.terminate(nil)
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
