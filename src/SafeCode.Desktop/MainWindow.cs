using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;
using Microsoft.Win32;
using SafeCode.Core;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;

namespace SafeCode.Desktop;

public sealed class MainWindow : Window
{
    private readonly WebView2 browser = new();
    private readonly WindowsClients clients = new();
    private readonly ClientServices services;
    private readonly ClientPreferences preferences;
    private DownloadedInstaller? downloadedUpdate;
    private bool installingUpdate;
    private int maintenanceOperations;
    private readonly CancellationTokenSource lifetime = new();
    private SshSession? session;
    private CancellationTokenSource? connecting;
    private bool connectionPending;
    private (string Token,HostKeyInfo Key)? pendingHostTrust;
    private readonly object logLock=new();
    private readonly Queue<(string Step,string Line)> logQueue=new();
    private readonly Dictionary<string,DiagnosticTrace> diagnosticTraces=new();
    private CancellationTokenSource? browsing;
    private readonly System.Windows.Threading.DispatcherTimer logTimer=new(){Interval=TimeSpan.FromMilliseconds(120)};
    private readonly System.Windows.Threading.DispatcherTimer integrationTimer=new(){Interval=TimeSpan.FromSeconds(5)};
    private bool integrationSampling;
    private string activeStep = "";
    private bool deploying;
    private GatewayWindow? gatewayWindow;
    private (SshSession Session,int Generation,bool Enabled,string Outcome)? gatewayObservation;
    private bool closingApproved;
    private bool exitRequested,exitInProgress;
    private MountTray? mountTray;
    private DesktopKeyAccess? keyAccess;
    private WindowsHostControl? hostControl;
    private ClaudeCodexBridge? claudeBridge;
    private ClaudeDesktopBridge? claudeDesktop;
    private string? acceptanceFingerprint;
    private readonly string[] arguments;
    private readonly ConnectionSettings? supportSettings;
    private readonly string? supportFingerprint;
    private bool IsAcceptance=>arguments.Length>=3&&(arguments[0]=="--acceptance"||arguments[0]=="--services-acceptance"||arguments[0]=="--connection-acceptance"||arguments[0]=="--maintenance-acceptance"||arguments[0]=="--ip-acceptance"||arguments[0]=="--updates-acceptance");
    private bool CanReportDiagnostics=>!IsAcceptance;
    private readonly string dataDirectory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SafeCode");
    public MainWindow(string[] arguments)
    {
        this.arguments=arguments;
        if(arguments.Contains("--acceptance")||arguments.Contains("--services-acceptance"))dataDirectory=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode-Acceptance");
        if(arguments.Contains("--connection-acceptance")){dataDirectory=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode-Connection-Acceptance");LocalSecrets.DirectoryOverride=dataDirectory;}
        if(arguments.Contains("--maintenance-acceptance")){
            using var testConfig=JsonDocument.Parse(File.ReadAllText(arguments[1]));var target=testConfig.RootElement;supportSettings=LocalSecrets.Load();
            if(supportSettings==null||supportSettings.Host!=target.GetProperty("allowedHost").GetString()||!target.GetProperty("allowMaintenance").GetBoolean())throw new InvalidOperationException("维护验收需要明确指定已授权的测试目标。");
            supportFingerprint=new HostTrustStore(dataDirectory).Describe(supportSettings.Host,supportSettings.Port,"").PreviousFingerprint;
            if(string.IsNullOrEmpty(supportFingerprint)||supportFingerprint!=target.GetProperty("expectedFingerprint").GetString())throw new InvalidOperationException("验收目标指纹未匹配已确认记录。");
            dataDirectory=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode-Maintenance-Acceptance");LocalSecrets.DirectoryOverride=dataDirectory;
        }
        if(arguments.Length>=1&&arguments[0] is "--ip-acceptance" or "--ip-manual")
        {
            dataDirectory=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode-IP-Acceptance");LocalSecrets.DirectoryOverride=dataDirectory;
            if(arguments[0]=="--ip-acceptance")
            {
                using var cfg=JsonDocument.Parse(File.ReadAllText(arguments[1]));
                if(!cfg.RootElement.GetProperty("allowNetworkChanges").GetBoolean()||cfg.RootElement.GetProperty("host").GetString()!=cfg.RootElement.GetProperty("ingressIp").GetString())throw new InvalidOperationException("IP 验收需要明确授权并指定目标入口。");
                acceptanceFingerprint=cfg.RootElement.GetProperty("fingerprint").GetString();
            }
        }
        if(arguments.Length>=3&&arguments[0]=="--updates-acceptance"){dataDirectory=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode-Updates-Acceptance");LocalSecrets.DirectoryOverride=dataDirectory;}
        services=new ClientServices(dataDirectory);preferences=new ClientPreferences(dataDirectory);
        clients.ResizePreferences=new RdpResizePreferences(dataDirectory);
        if(CanReportDiagnostics)clients.DisplayOperationCompleted=error=>
        {
            var trace=services.BeginDiagnostic("desktop_display");
            if(trace!=null)_=services.SendDiagnosticAsync(trace,error,lifetime.Token);
        };
        Title = "Safe Code"; Width = 360; Height = 536;
        MinWidth = 360; MinHeight = 500; WindowStartupLocation = WindowStartupLocation.CenterScreen;
        WindowStyle = WindowStyle.None; ResizeMode = ResizeMode.CanResize; Background = Brushes.White;
        Icon = System.Windows.Media.Imaging.BitmapFrame.Create(new Uri("pack://application:,,,/Icons/safe-code-shortcut.ico"));
        // Remove the native caption strip as well as the HTML caption; keep edge resizing.
        System.Windows.Shell.WindowChrome.SetWindowChrome(this, new System.Windows.Shell.WindowChrome
        {
            CaptionHeight = 0, ResizeBorderThickness = new Thickness(5), GlassFrameThickness = new Thickness(0),
            CornerRadius = new CornerRadius(0), UseAeroCaptionButtons = false, NonClientFrameEdges = System.Windows.Shell.NonClientFrameEdges.None
        });
        Content = browser;
        logTimer.Tick+=async(_,_)=>await FlushLogsAsync();logTimer.Start();
        Loaded += async (_, _) => await InitializeAsync();
        if(!IsAcceptance)
        {
            keyAccess=new DesktopKeyAccess(dataDirectory,LocalSecrets.Protect,LocalSecrets.Unprotect,diagnostics:CanReportDiagnostics?services:null);
            claudeBridge=new ClaudeCodexBridge(dataDirectory);claudeDesktop=new ClaudeDesktopBridge(dataDirectory);
            integrationTimer.Tick+=async(_,_)=>
            {
                if(integrationSampling)return;integrationSampling=true;
                try{await claudeBridge.RefreshAsync(lifetime.Token);await claudeBridge.TickAsync(lifetime.Token);if(claudeDesktop!=null)await claudeDesktop.TickAsync(lifetime.Token);}
                catch(OperationCanceledException){}
                catch(Exception){}
                finally{integrationSampling=false;}
            };
            integrationTimer.Start();
            mountTray=new MountTray(this,()=>session?.GetMountStatus()??new(0,"idle","未挂载目录",0,0,0,0,0),ShowMainWindow,ReconnectMountsFromTrayAsync,()=>{exitRequested=true;Close();},PublishMountStatus,()=>hostControl?.Status,()=>claudeBridge?.Status,()=>claudeDesktop?.Status);
            StateChanged+=(_,_)=>{if(WindowState==WindowState.Minimized){ShowInTaskbar=false;Hide();}};
        }
        Closing += async (_, e) =>
        {
            if(!closingApproved&&(exitInProgress||installingUpdate)){e.Cancel=true;return;}
            if(!closingApproved&&!IsAcceptance&&!exitRequested&&(session?.HasMounts==true||hostControl?.Enabled==true||claudeBridge?.Running==true||claudeDesktop?.Status.Managed==true||keyAccess?.Running==true||deploying))
            {e.Cancel=true;ShowInTaskbar=false;Hide();mountTray?.NotifyHidden();return;}
            if(!closingApproved&&(connectionPending||deploying||maintenanceOperations>0||gatewayWindow?.Busy==true))
            {e.Cancel=true;exitRequested=false;ShowMainWindow();MessageBox.Show(this,"任务正在执行，请等待完成后退出。","Safe Code");return;}
            if(!closingApproved)
            {
                e.Cancel=true;
                exitInProgress=true;
                mountTray?.SetExiting(true);
                integrationTimer.Stop();
                try
                {
                    await System.Windows.Threading.Dispatcher.Yield();
                    // Confirm synchronization before releasing the authenticated file channel.
                    if(session is {HasMounts:true} mounted)await Task.Run(()=>mounted.UnmountAsync(lifetime.Token));
                    if(claudeBridge?.Running==true)await claudeBridge.StopAsync(lifetime.Token,suspend:true);
                    if(claudeDesktop?.Status.Managed==true)await claudeDesktop.StopAsync(lifetime.Token,suspend:true);
                    if(hostControl?.Enabled==true)await hostControl.DisableAsync(lifetime.Token);
                    if(keyAccess!=null)await keyAccess.StopAsync();
                    closingApproved=true;
                }
                catch(Exception error)
                {
                    exitRequested=false;
                    ShowMainWindow();
                    MessageBox.Show(this,error.Message,"暂未退出",MessageBoxButton.OK,MessageBoxImage.Warning);
                }
                finally
                {
                    exitInProgress=false;
                    if(!closingApproved){mountTray?.SetExiting(false);if(!IsAcceptance)integrationTimer.Start();}
                }
                if(closingApproved)Close();
                return;
            }
            claudeBridge?.Dispose();claudeDesktop?.Dispose();hostControl?.Dispose();mountTray?.Dispose();integrationTimer.Stop();logTimer.Stop();lifetime.Cancel();services.Dispose();clients.Dispose();session?.Dispose();browser.Dispose();
        };
    }
    private void ShowMainWindow(){ShowInTaskbar=true;Show();WindowState=WindowState.Normal;Activate();}
    private async Task ReconnectMountsFromTrayAsync()
    {
        if(exitInProgress||session is not {HasMounts:true} target)return;
        var trace=CanReportDiagnostics?services.BeginDiagnostic("mount_reconnect"):null;
        Exception? failure=null;
        try
        {
            await Task.Run(()=>target.RecoverMountsAsync(lifetime.Token),lifetime.Token);
            var status=target.GetMountStatus();
            if(status.State is "reconnecting" or "attention")failure=status.AuthenticationRequired
                ? new Renci.SshNet.Common.SshAuthenticationException("Mount authentication required.") : new IOException();
        }
        catch(Exception error){failure=error;MessageBox.Show(this,error.Message,"挂载守护");}
        finally{if(trace!=null)_=services.SendDiagnosticAsync(trace,failure,lifetime.Token);}
    }

    private Task<T> RunWithDiagnosticAsync<T>(string operation,Func<Task<T>> run,bool background=false)
        =>CanReportDiagnostics?services.WithDiagnosticAsync(operation,run,lifetime.Token,background):run();

    private async Task<JsonElement> ReadGatewayStatusAsync(SshSession target)
    {
        var previous=gatewayObservation;
        bool recovering=previous is {Enabled:true,Outcome:not "none"} saved&&ReferenceEquals(saved.Session,target);
        var trace=CanReportDiagnostics?services.BeginDiagnostic(recovering?"gateway_reconnect":"gateway_status",true):null;
        bool enabled=false;
        Exception? failure=null;
        try
        {
            var status=await target.ReadPublicGatewayAsync(true,lifetime.Token);
            enabled=status.TryGetProperty("enabled",out var field)&&field.ValueKind==JsonValueKind.True;
            return status;
        }
        catch(Exception error){failure=error;throw;}
        finally
        {
            if(trace!=null&&ReferenceEquals(session,target))
            {
                bool knownEnabled=failure!=null&&previous is {Enabled:true} prior&&ReferenceEquals(prior.Session,target);
                var next=(Session:target,Generation:trace.Generation,Enabled:enabled||knownEnabled,Outcome:DiagnosticTrace.ErrorKind(failure));
                bool changed=gatewayObservation!=next;
                gatewayObservation=next;
                if(changed)_=services.SendDiagnosticAsync(trace,failure,lifetime.Token);
            }
        }
    }
    private void PublishMountStatus(MountStatus status)
    {
        if(browser.CoreWebView2!=null&&!lifetime.IsCancellationRequested)_=Publish();
        async Task Publish(){try{await browser.CoreWebView2.ExecuteScriptAsync("window.safeCodeMountStatus?.("+JsonSerializer.Serialize(status,JsonDefaults.Options)+")");}catch{}}
    }
    private async Task InitializeAsync()
    {
        try
        {
            if(IsAcceptance)File.AppendAllText(arguments[2]+".stage.log","窗口已加载\n");
            Directory.CreateDirectory(dataDirectory);
            var options = new CoreWebView2EnvironmentOptions();
            if(IsAcceptance)options.AdditionalBrowserArguments="--disable-gpu";
            var environment = await CoreWebView2Environment.CreateAsync(null, Path.Combine(dataDirectory, "WebView"), options);
            await browser.EnsureCoreWebView2Async(environment);
            if(IsAcceptance)File.AppendAllText(arguments[2]+".stage.log","WebView 已就绪\n");
            if (arguments.Length>=3&&arguments[0]=="--acceptance")
            { using var cfg = JsonDocument.Parse(File.ReadAllText(arguments[1])); acceptanceFingerprint = cfg.RootElement.GetProperty("fingerprint").GetString(); }
            browser.CoreWebView2.SetVirtualHostNameToFolderMapping("safe-code.local", Path.Combine(AppContext.BaseDirectory, "Web"), CoreWebView2HostResourceAccessKind.DenyCors);
            await browser.CoreWebView2.Profile.ClearBrowsingDataAsync(CoreWebView2BrowsingDataKinds.DiskCache);
            if(IsAcceptance)await browser.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync("window.__safeCodeAcceptance=true;window.__safeCodeConnectionAcceptance="+(arguments[0]=="--connection-acceptance"?"true":"false")+";");
            await browser.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync("""
                window.__safeCodeDesktop=true;
                for(const name of ['click','keydown'])document.addEventListener(name,e=>{if(!window.safeCodeNativeReady&&!e.target.closest?.('[data-safe-code-startup-close]')){e.preventDefault();e.stopImmediatePropagation();}},true);
                document.addEventListener('DOMContentLoaded',()=>{
                  if(window.safeCodeNativeReady)return;
                  document.querySelector('main')?.setAttribute('inert','');
                  const panel=document.createElement('div');panel.style.cssText='position:fixed;inset:0;background:white;display:grid;place-content:center;padding:35px;font:13px sans-serif;color:#4b6654;text-align:center';
                  const message=document.createElement('p');message.textContent='客户端核心模块加载失败，已停止运行。请检查软件包是否完整。';
                  const close=document.createElement('button');close.setAttribute('data-safe-code-startup-close','');close.textContent='退出';close.onclick=()=>window.chrome.webview.postMessage({id:'failed-startup-close',op:'window.close',args:{}});panel.append(message,close);document.body.append(panel);
                });
                """);
            browser.CoreWebView2.Settings.IsPasswordAutosaveEnabled=false;
            browser.CoreWebView2.Settings.IsGeneralAutofillEnabled=false;
            browser.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
            browser.CoreWebView2.Settings.IsStatusBarEnabled = false;
            browser.CoreWebView2.Settings.AreDevToolsEnabled = false;
            browser.CoreWebView2.NavigationStarting += (_, e) =>
            {
                if (e.Uri.StartsWith("https://safe-code.local/", StringComparison.Ordinal)) return;
                e.Cancel = true;
                if (Uri.TryCreate(e.Uri, UriKind.Absolute, out var uri) && (uri.Scheme == "https" || uri.Scheme == "http")) WindowsClients.OpenUrl(e.Uri);
            };
            browser.CoreWebView2.NewWindowRequested += (_, e) => { e.Handled = true; try { WindowsClients.OpenUrl(e.Uri); } catch { } };
            browser.CoreWebView2.WebMessageReceived += OnMessage;
            browser.CoreWebView2.NavigationCompleted += async (_, e) =>
            {
                if(e.IsSuccess&&IsAcceptance){if(arguments[0]=="--updates-acceptance")await browser.CoreWebView2.ExecuteScriptAsync("window.safeCodeRunUpdateAcceptance()");else if(arguments[0]=="--ip-acceptance"){using var cfg=JsonDocument.Parse(File.ReadAllText(arguments[1]));await browser.CoreWebView2.ExecuteScriptAsync("window.safeCodeRunIpAcceptance("+cfg.RootElement.GetRawText()+")");}else if(arguments[0]=="--maintenance-acceptance")await browser.CoreWebView2.ExecuteScriptAsync("window.safeCodeRunMaintenanceAcceptance()");else if(arguments[0]=="--connection-acceptance"){using var cfg=JsonDocument.Parse(File.ReadAllText(arguments[1]));await browser.CoreWebView2.ExecuteScriptAsync("window.safeCodeRunConnectionAcceptance("+cfg.RootElement.GetRawText()+")");}else if(arguments[0]=="--services-acceptance")await browser.CoreWebView2.ExecuteScriptAsync("window.safeCodeRunServicesAcceptance()");else await RunAcceptanceAsync(arguments[1],arguments[2]);}
            };
            browser.Source = new Uri("https://safe-code.local/index.html");
        }
        catch (Exception error)
        {
            if(IsAcceptance)File.WriteAllText(arguments[2]+".error.txt",error.ToString());
            MessageBox.Show(this, "客户端初始化失败。请安装 Microsoft Edge WebView2 Runtime。\n\n" + error.Message, "Safe Code", MessageBoxButton.OK, MessageBoxImage.Warning); Close();
        }
    }
    private async void OnMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        if (!e.Source.StartsWith("https://safe-code.local/", StringComparison.Ordinal)) return;
        string id = "",operation="";
        DiagnosticTrace? diagnostic=null;Exception? diagnosticError=null;
        try
        {
            using var document = JsonDocument.Parse(e.WebMessageAsJson); var root = document.RootElement;
            id = root.GetProperty("id").GetString() ?? ""; operation = root.GetProperty("op").GetString() ?? "";
            if(IsAcceptance)File.AppendAllText(arguments[2]+".stage.log","RPC "+operation+"\n");
            var args = root.TryGetProperty("args", out var value) ? value : default;
            if(exitInProgress)throw new InvalidOperationException("正在安全退出，请稍候。");
            if(installingUpdate)throw new InvalidOperationException("正在准备安装更新，请稍候。");
            bool maintenance=operation is "cloud.install" or "cloud.expose" or "mount.install" or "mount.apply" or "ip.apply" or "terminal" or "host.enable" or "host.disable" or "bridge.start" or "bridge.stop" or "claudeDesktop.start" or "claudeDesktop.stop" or "desktopKey.enable" or "desktopKey.disable" or "connection.reauthenticate";
            if(maintenance)maintenanceOperations++;
            if(CanReportDiagnostics)
            {
                string? diagnosticOperation=DiagnosticTrace.OperationFor(operation,args);
                if(diagnosticOperation!=null)diagnostic=services.BeginDiagnostic(diagnosticOperation,DiagnosticTrace.IsBackgroundOperation(diagnosticOperation,args));
                if(diagnostic!=null)lock(logLock)diagnosticTraces[diagnostic.Operation]=diagnostic;
            }
            object? answer;try{answer=await HandleAsync(operation,args,error=>diagnosticError=error);}finally{if(maintenance)maintenanceOperations--;}

            await FlushLogsAsync();
            await RespondAsync(id, answer, null);
        }
        catch (Exception error) { diagnosticError=error;string? issue=diagnostic==null?null:services.RecordFailure(diagnostic,error);await RespondAsync(id, null, ConnectionErrors.DescribeOperation(error)+(issue==null?"":"\n问题编号："+issue)); }
        finally
        {
            if(diagnostic!=null)
            {
                lock(logLock){if(diagnosticTraces.GetValueOrDefault(diagnostic.Operation)==diagnostic)diagnosticTraces.Remove(diagnostic.Operation);}
                if(operation!="connect"||pendingHostTrust==null||diagnosticError!=null)_=services.SendDiagnosticAsync(diagnostic,diagnosticError,lifetime.Token);
            }
        }
    }
    private async Task<object?> HandleAsync(string operation,JsonElement args,Action<Exception>? observeFailure=null)
    {
        string Read(string key, string fallback = "") => args.ValueKind == JsonValueKind.Object && args.TryGetProperty(key, out var value) ? value.GetString() ?? fallback : fallback;
        switch (operation)
        {
            case "bridge.status": claudeBridge??=new ClaudeCodexBridge(dataDirectory);return await claudeBridge.RefreshAsync(lifetime.Token);
            case "bridge.start":
                if(!args.TryGetProperty("thirdPartyAccepted",out var accepted)||!accepted.GetBoolean())throw new InvalidOperationException("请先确认第三方适配器和数据处理说明。");
                if(deploying)throw new InvalidOperationException("请等待当前操作完成。");
                claudeBridge??=new ClaudeCodexBridge(dataDirectory);deploying=true;activeStep="codex_bridge";
                try{return await claudeBridge.StartAsync(RequireSession(),args.GetProperty("port").GetInt32(),Read("model").Trim(),args.TryGetProperty("writable",out var writable)&&writable.GetBoolean(),lifetime.Token);}finally{deploying=false;activeStep="";}
            case "bridge.stop":
                if(deploying)throw new InvalidOperationException("请等待当前操作完成。");deploying=true;activeStep="codex_bridge";
                try{return claudeBridge==null?new ClaudeCodexStatus(false,false,false,18797,"sonnet",false,"未启动"):await claudeBridge.StopAsync(lifetime.Token);}finally{deploying=false;activeStep="";}
            case "desktopKey.status":
                return await (keyAccess??=new DesktopKeyAccess(dataDirectory,LocalSecrets.Protect,LocalSecrets.Unprotect,diagnostics:CanReportDiagnostics?services:null))
                    .RestoreAsync(lifetime.Token,interactive:args.TryGetProperty("interactive",out var interactive)&&interactive.ValueKind==JsonValueKind.True);
            case "desktopKey.enable":
                claudeDesktop??=new ClaudeDesktopBridge(dataDirectory);
                if((await claudeDesktop.RefreshAsync(lifetime.Token)).Managed)throw new InvalidOperationException("请先停止本机的 SSH Desktop 桥接再使用 Key。");
                return await (keyAccess??=new DesktopKeyAccess(dataDirectory,LocalSecrets.Protect,LocalSecrets.Unprotect,diagnostics:CanReportDiagnostics?services:null))
                    .EnableAsync(Read("key"),lifetime.Token,
                        replace:args.TryGetProperty("replace",out var replaceKey)&&replaceKey.ValueKind==JsonValueKind.True,
                        useSaved:args.TryGetProperty("useSaved",out var useSavedKey)&&useSavedKey.ValueKind==JsonValueKind.True);
            case "desktopKey.disable": return await (keyAccess??=new DesktopKeyAccess(dataDirectory,LocalSecrets.Protect,LocalSecrets.Unprotect,diagnostics:CanReportDiagnostics?services:null)).DisableAsync(lifetime.Token);
            case "safeCodeApi.publicStatus":
                return await ReadGatewayStatusAsync(RequireGatewaySession());
            case "safeCodeApi.enablePublic":
                if (!args.TryGetProperty("accepted", out var publicAccepted) || publicAccepted.ValueKind != JsonValueKind.True)
                    throw new InvalidOperationException("请先确认「保证自用」。");
                return await RequireGatewaySession().EnablePublicGatewayAsync(lifetime.Token);
            case "window.gateway.open":
                var gatewaySession=RequireGatewaySession();
                await gatewaySession.RequirePublicGatewayAsync(lifetime.Token);
                    if(exitInProgress||installingUpdate||!ReferenceEquals(gatewaySession,RequireGatewaySession()))
                    throw new InvalidOperationException("SSH 会话已变更，请重新打开网关管理。");
                if(gatewayWindow!=null){gatewayWindow.Activate();return true;}
                var gateway=new GatewayWindow(browser.CoreWebView2.Environment, options=>
                {
                    Task<JsonElement> ManageAsync()
                    {
                        if(installingUpdate||!ReferenceEquals(gatewaySession,RequireGatewaySession()))
                            throw new InvalidOperationException("SSH 会话已变更，请重新打开网关管理。");
                        var request=options.Deserialize<Dictionary<string,JsonElement>>()!;
                        request.Remove("background");
                        return gatewaySession.RequestGatewayAsync(request,lifetime.Token);
                    }
                    var diagnostic=DiagnosticTrace.GatewayOperation(options);
                    return diagnostic==null?ManageAsync():RunWithDiagnosticAsync(diagnostic,ManageAsync,DiagnosticTrace.IsBackgroundOperation(diagnostic,options));
                }){Owner=this,Icon=Icon};
                gatewayWindow=gateway;
                gateway.Closed+=(_,_)=>gatewayWindow=null;
                gateway.Show();
                return true;
            case "claudeDesktop.status": claudeDesktop??=new ClaudeDesktopBridge(dataDirectory);return await claudeDesktop.RefreshAsync(lifetime.Token);
            case "claudeDesktop.start":
                if(keyAccess?.Configured==true)throw new InvalidOperationException("请先停用 API Key 接入。");
                if(!args.TryGetProperty("accepted",out var claudeAccepted)||!claudeAccepted.GetBoolean())throw new InvalidOperationException("请先确认通过 VPS 原版 Claude Code 接入。");
                if(deploying)throw new InvalidOperationException("请等待当前操作完成。");
                claudeDesktop??=new ClaudeDesktopBridge(dataDirectory);deploying=true;activeStep="claude_desktop";
                try{return await claudeDesktop.StartAsync(RequireSession(),true,lifetime.Token);}finally{deploying=false;activeStep="";}
            case "claudeDesktop.stop":
                if(deploying)throw new InvalidOperationException("请等待当前操作完成。");
                deploying=true;activeStep="claude_desktop";
                try{return claudeDesktop==null?null:await claudeDesktop.StopAsync(lifetime.Token);}finally{deploying=false;activeStep="";}
            case "claudeDesktop.open": claudeDesktop??=new ClaudeDesktopBridge(dataDirectory);return claudeDesktop.OpenClient();
            case "codex.open": return WindowsClients.OpenCodex();
            case "bridge.copy-config": if(claudeBridge==null)throw new InvalidOperationException("请先启动适配器。");Clipboard.SetText(claudeBridge.Configuration);return true;
            case "bridge.copy-key": if(claudeBridge==null)throw new InvalidOperationException("请先启动适配器。");Clipboard.SetText(claudeBridge.Token);return true;
            case "host.status": hostControl??=new WindowsHostControl(dataDirectory);return hostControl.Status;
            case "host.enable":
                if(!args.TryGetProperty("riskAccepted",out var risk)||!risk.GetBoolean())throw new InvalidOperationException("请先阅读并确认本机远程控制权限说明。");
                if(deploying)throw new InvalidOperationException("请等待当前操作完成。");
                hostControl??=new WindowsHostControl(dataDirectory);deploying=true;activeStep="host_enable";
                try{return await hostControl.EnableAsync(RequireSession(),Read("alias"),lifetime.Token);}finally{deploying=false;activeStep="";}
            case "host.disable":
                if(deploying)throw new InvalidOperationException("请等待当前操作完成。");
                return hostControl==null?new HostControlStatus(false,"disabled","","","未启用"):await hostControl.DisableAsync(lifetime.Token);
            case "preview.begin":
                if(connectionPending||deploying||maintenanceOperations>0||session?.HasMounts==true||hostControl?.Enabled==true||claudeBridge?.Running==true||claudeDesktop?.Status.Managed==true)throw new InvalidOperationException("请先结束当前操作并卸载目录，再进入开发预览。");
                if(gatewayWindow?.Busy==true)throw new InvalidOperationException("请等待当前密钥操作完成。");
                gatewayWindow?.Close();
                connecting?.Cancel();pendingHostTrust=null;clients.Dispose();session?.Dispose();session=null;return true;
            case "window.resize":
                Width = args.GetProperty("wide").GetBoolean() ? 740 : 360; Height = args.GetProperty("wide").GetBoolean() ? 634 : 536;
                Left = Math.Max(0, (SystemParameters.WorkArea.Width - Width) / 2); Top = Math.Max(0, (SystemParameters.WorkArea.Height - Height) / 2); return true;
            case "window.drag":
                ReleaseCapture(); SendMessage(new System.Windows.Interop.WindowInteropHelper(this).Handle, 0xA1, (IntPtr)2, IntPtr.Zero); return true;
            case "window.minimize": WindowState = WindowState.Minimized; return true;
            case "window.maximize": WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized; return true;
            case "window.close": Close(); return true;
            case "copy": Clipboard.SetText(Read("text")); return true;
            case "services.preferences": return services.Preferences();
            case "services.confirm": return await services.ConfirmConsentAsync();
            case "services.consent": return await services.SetConsentAsync(args.GetProperty("enabled").GetBoolean(),args.TryGetProperty("confirmed",out var consent)&&consent.GetBoolean());
            case "diagnostics.consent": return services.SetDiagnosticConsent(args.GetProperty("enabled").GetBoolean());
            case "feedback.send": await services.SendFeedbackAsync(Read("message"),lifetime.Token);return true;
            case "updates.check": return await services.CheckUpdateAsync(args.TryGetProperty("force",out var force)&&force.GetBoolean(),lifetime.Token);
            case "updates.download":
                downloadedUpdate=await services.DownloadUpdateAsync((done,total)=>{_ = Dispatcher.BeginInvoke(async()=>{if(browser.CoreWebView2!=null&&!lifetime.IsCancellationRequested)await browser.CoreWebView2.ExecuteScriptAsync("window.safeCodeUpdateProgress?.("+done+","+total+")");});},lifetime.Token);return true;
            case "updates.install":
                if(deploying||maintenanceOperations>0)throw new InvalidOperationException("请等待当前操作完成后安装更新。");
                if(downloadedUpdate==null||!File.Exists(downloadedUpdate.Path))throw new InvalidOperationException("请先下载并校验安装包。");
                installingUpdate=true;
                try{await ClientServices.VerifyInstallerAsync(downloadedUpdate,lifetime.Token);if(claudeBridge?.Running==true)await claudeBridge.StopAsync(suspend:true);if(claudeDesktop?.Status.Managed==true)await claudeDesktop.StopAsync(suspend:true);if(hostControl?.Enabled==true)await hostControl.DisableAsync();if(session?.HasMounts==true)await session.UnmountAsync();if(keyAccess!=null)await keyAccess.StopAsync();
                Process.Start(new ProcessStartInfo(downloadedUpdate.Path){UseShellExecute=true});closingApproved=true;Close();return true;}
                catch{installingUpdate=false;throw;}
            case "gateways.resume-settings":
                if(IsAcceptance)return null;
                var remembered=LocalSecrets.Load();
                return remembered!=null&&(claudeDesktop?.Preference.Matches(remembered)==true||claudeBridge?.Preference.Matches(remembered)==true||SshSession.CanResumePublicGateway(dataDirectory,remembered))?remembered:null;
            case "login.load": return LocalSecrets.Load();
            case "connection.info": return session==null?null:new{host=session.Host,port=session.Port,username=session.LoginUsername};
            case "connection.reauthenticate":
                if(deploying||connectionPending||maintenanceOperations>1)throw new InvalidOperationException("请等待当前操作完成后更新登录密码。");
                var recovering=session??throw new InvalidOperationException("请先登录服务器。");deploying=true;
                try
                {
                    using var recoveryTimeout=CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);recoveryTimeout.CancelAfter(TimeSpan.FromSeconds(90));
                    await Task.Run(()=>recovering.ReauthenticateAsync(Read("password"),recoveryTimeout.Token));
                    try{LocalSecrets.Save(new(recovering.Host,recovering.LoginUsername,Read("password"),recovering.Port));}catch{QueueLog("connection","新密码已验证，但登录记忆未能保存，下次请重新填写。");}
                    return new{ready=true,mount=recovering.GetMountStatus()};
                }
                catch(Exception error){throw new InvalidOperationException(ConnectionErrors.Describe(error),error);}
                finally{deploying=false;}
            case "pickFolder": var dialog = new OpenFolderDialog { Title = "选择要挂载到 VPS 的本地目录", InitialDirectory=Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments) }; return dialog.ShowDialog(this) == true ? dialog.FolderName : null;
            case "acceptance.capture":
                if(!IsAcceptance)throw new InvalidOperationException("未启用验收模式。");
                string tag=Read("tag");if(tag!="login"&&tag!="notice"&&tag!="deployment"&&tag!="mount"&&tag!="custom"&&tag!="services"&&tag!="loading"&&tag!="error"&&tag!="advanced"&&tag!="trust"&&tag!="resolution"&&tag!="draft"&&tag!="install"&&tag!="folder"&&tag!="maintenance"&&tag!="ip"&&tag!="update")throw new ArgumentException("截图标识无效。");
                await Task.Delay(200);using(var image=File.Create(arguments[2]+"."+tag+".png"))await browser.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,image);return true;
            case "acceptance.window-sizes":
                if(!IsAcceptance)throw new InvalidOperationException("未启用验收模式。");
                using(var window=new RdpDesktop(new(1024,768))){window.Show();await Task.Delay(150);var small=new{width=window.Width,height=window.Height};window.ApplyResolution(new(1600,900));var large=new{width=window.Width,height=window.Height};if(large.width<=small.width)throw new InvalidOperationException("分辨率增加后窗口未变大。");window.ApplyResolution(new(1024,768));if(window.Width!=small.width||window.Height!=small.height)throw new InvalidOperationException("分辨率减小后窗口未还原。");window.ApplyResolution(new(1920,1080));var area=System.Windows.Forms.Screen.FromControl(window).WorkingArea;if(!area.Contains(window.Bounds))throw new InvalidOperationException("桌面窗口超出工作区。");window.Close();return new{success=true,small,large,screenBoundsRespected=true};}
            case "acceptance.connect-current":
                if(!IsAcceptance||arguments[0]!="--maintenance-acceptance"||supportSettings==null)throw new InvalidOperationException("未启用维护验收。");
                session=new SshSession(dataDirectory);session.Log+=text=>QueueLog(activeStep,text);session.DirectoryLog+=text=>QueueLog("folder",text);
                var inspected=await Task.Run(()=>session.ConnectAsync(supportSettings,key=>key.Fingerprint==supportFingerprint,lifetime.Token));return new{server=inspected,username=supportSettings.Username};
            case "acceptance.hostname":
                if(!IsAcceptance||arguments[0]!="--maintenance-acceptance")throw new InvalidOperationException("未启用维护验收。");return (await RequireSession().ExecuteTextAsync("hostname",lifetime.Token)).Trim();
            case "acceptance.cloud-http":
                if(!IsAcceptance||arguments[0]!="--maintenance-acceptance")throw new InvalidOperationException("未启用维护验收。");uint preview=await RequireSession().ForwardAsync(3001,lifetime.Token);using(var http=new System.Net.Http.HttpClient{Timeout=TimeSpan.FromSeconds(15)}){using var reply=await http.GetAsync("http://127.0.0.1:"+preview,lifetime.Token);return new{status=(int)reply.StatusCode};}
            case "client.verify":
                if(!IsAcceptance)throw new InvalidOperationException("未启用验收模式。");
                return new{nativeSsh=await clients.VerifyNativeSshAsync(RequireSession(),lifetime.Token),desktopLogin=await clients.VerifyDesktopLoginAsync(RequireSession(),lifetime.Token),savedLogin=LocalSecrets.Load()?.Host==RequireSession().Host};
            case "acceptance.done":
                if (!IsAcceptance) throw new InvalidOperationException("当前未启用验收模式。");
                string output = arguments[2]; Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(output))!);
                File.WriteAllText(output + ".json", args.GetRawText());
                await Task.Delay(400);
                using (var file = File.Create(output + ".png")) await browser.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, file);
                Environment.ExitCode = args.GetProperty("success").GetBoolean() ? 0 : 1;
                _ = Dispatcher.BeginInvoke(() => Close()); return true;
            case "connection.cancel": connecting?.Cancel();pendingHostTrust=null;return true;
            case "host.trust":
                if(pendingHostTrust is not {} challenge||challenge.Token!=Read("token"))throw new InvalidOperationException("服务器身份确认已失效，请重新连接。");
                new HostTrustStore(dataDirectory).Accept(challenge.Key);pendingHostTrust=null;return true;
            case "notice.state":
                string noticePath=Path.Combine(dataDirectory,"notice-accepted-v2");bool noticeAccepted=File.Exists(noticePath)||services.NoticeWasConfirmed;if(noticeAccepted&&!File.Exists(noticePath))File.WriteAllText(noticePath,DateTimeOffset.UtcNow.ToString("O"));return new{accepted=noticeAccepted};
            case "notice.accept": File.WriteAllText(Path.Combine(dataDirectory,"notice-accepted-v2"),DateTimeOffset.UtcNow.ToString("O"));return true;
            case "notice.claudeDesktop.state": return preferences.ClaudeDesktopWarningAccepted;
            case "notice.claudeDesktop.accept": preferences.AcceptClaudeDesktopWarning();return true;
            case "connect":
                if(session?.HasMounts==true||hostControl?.Enabled==true||claudeBridge?.Running==true||claudeDesktop?.Running==true)throw new InvalidOperationException("正在保护挂载目录或本机控制通道，请先停用并安全卸载再更换连接。");
                if(connectionPending)throw new InvalidOperationException("连接正在处理中，请稍候。");
                if(gatewayWindow?.Busy==true)throw new InvalidOperationException("请等待当前密钥操作完成。");
                gatewayWindow?.Close();
                connectionPending=true;try{connecting?.Dispose();connecting=CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);connecting.CancelAfter(TimeSpan.FromSeconds(90));
                session?.Dispose(); session = new SshSession(dataDirectory);
                session.Log += text => {if(IsAcceptance)File.AppendAllText(arguments[2]+".stage.log","SSH "+text+"\n");QueueLog(activeStep,text);};
                session.DirectoryLog+=text=>QueueLog("folder",text);
                var settings = new ConnectionSettings(Read("host"), Read("username"), Read("password"), args.TryGetProperty("port", out var port) ? port.GetInt32() : 22);
                    var current=session;var server=await Task.Run(()=>current.ConnectAsync(settings,host=>acceptanceFingerprint!=null&&acceptanceFingerprint==host.Fingerprint,connecting.Token),connecting.Token);
                    pendingHostTrust=null;LocalSecrets.Save(settings);
                    var gatewayWarnings=new List<string>();
                    if(!IsAcceptance)
                    {
                        deploying=true;
                        try
                        {
                            if(keyAccess?.Configured!=true && claudeDesktop?.Preference.Matches(current)==true)
                                try{await RunWithDiagnosticAsync("claude_desktop_restore",()=>claudeDesktop.StartAsync(current,true,connecting.Token),background:true);}catch(Exception error){gatewayWarnings.Add("Claude 网关恢复失败："+error.Message);}
                            if(claudeBridge?.Preference is {} bridgePreference&&bridgePreference.Matches(current))
                                try{await RunWithDiagnosticAsync("codex_bridge_restore",()=>claudeBridge.StartAsync(current,bridgePreference.LocalPort,bridgePreference.Model,bridgePreference.Writable,connecting.Token),background:true);}catch(Exception error){gatewayWarnings.Add("Codex 网关恢复失败："+error.Message);}
                        }
                        finally{deploying=false;}
                    }
                    return new{ready=true,server,gatewayWarnings};
                }catch(HostTrustRequiredException error){string token=Guid.NewGuid().ToString("N");pendingHostTrust=(token,error.Key);return new{requiresTrust=true,token,key=error.Key};}
                catch(Exception error){
                    pendingHostTrust=null;
                    if(args.TryGetProperty("automatic",out var automatic)&&automatic.ValueKind==JsonValueKind.True&&ConnectionErrors.CanRetry(error)){
                        observeFailure?.Invoke(error);
                        return new{retryable=true,message=ConnectionErrors.Describe(error)};
                    }
                    throw new InvalidOperationException(ConnectionErrors.Describe(error),error);
                }
                finally{connectionPending=false;}

            case "job.resume":
                if(deploying)throw new InvalidOperationException("已有任务正在监控。");deploying=true;activeStep=Read("operation");
                try{return await RequireSession().ResumeJobAsync(Read("jobId"),lifetime.Token);}finally{deploying=false;activeStep="";}
            case "inspect": return await RequireSession().RequestAsync<ServerInfo>("inspect", null, lifetime.Token);
            case "ip.info": return await IpIntelligenceService.QueryAsync(Read("ip"),lifetime.Token);
            case "metrics": return await RequireSession().MeasureNetworkAsync(lifetime.Token);
            case "resources": return await RequireSession().ReadResourceStatsAsync(lifetime.Token);
            case "environment.assess": return await RequireSession().AssessEnvironmentAsync(lifetime.Token);
            case "deploy":
                if(Read("step")=="account"&&(claudeBridge?.Running==true||claudeDesktop?.Status.Managed==true))throw new InvalidOperationException("请先停止模型适配器和 Desktop 桥接，再更新工作账户。");
                if (deploying) throw new InvalidOperationException("另一个任务正在执行，请等待。");
                deploying = true; activeStep = Read("step");
                try
                {
                    var target = RequireSession();
                    var locale = preferences.RequireDeploymentChoices(target);
                    var draft = activeStep=="account" ? preferences.ReadDraft(target) ?? throw new InvalidOperationException("请先保存工作账户设置。") : null;
                    var result = await target.DeployAsync(activeStep, locale, draft?.Username??"workspace", lifetime.Token, draft?.Password??"", draft?.Hostname??"");
                    if(activeStep=="account"&&result.Statuses.GetValueOrDefault("account"))preferences.ClearDraft(target);
                    if (activeStep == "ssh") await target.VerifyWorkspaceKeyAsync(lifetime.Token);
                    return result;
                }
                finally { deploying = false; activeStep = ""; }
            case "folder.create": return await RequireSession().CreateDirectoryAsync(Read("path"),Read("name"),lifetime.Token);
            case "folder.cancel": try{browsing?.Cancel();}catch(ObjectDisposedException){}return true;
            case "folder.list":
                try{browsing?.Cancel();}catch(ObjectDisposedException){}browsing=CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
                using(var timeout=browsing){timeout.CancelAfter(TimeSpan.FromSeconds(12));try{return await RequireSession().ListDirectoriesAsync(Read("path"),timeout.Token);}catch(Exception error)when(error is OperationCanceledException or Renci.SshNet.Common.SshOperationTimeoutException){throw new TimeoutException("读取目录超时，请检查网络或挂载目录，稍后重试。",error);}}
            case "account.draft.get": return preferences.ReadDraft(RequireSession());
            case "account.change.status": return new{requiresConfirmation=preferences.RequiresAccountConfirmation(RequireSession())};
            case "account.draft.save": preferences.SaveDraft(RequireSession(),new(Read("username"),Read("hostname"),Read("password")));return true;
            case "deployment.settings.get": return preferences.ReadDeploymentChoices(RequireSession());
            case "deployment.locale.save": return preferences.SaveDeploymentLocale(RequireSession(),Read("region"));
            case "desktop.resolution.get": return preferences.ReadResolution();
            case "desktop.resolution.save": var resolution=new DesktopResolution(args.GetProperty("width").GetInt32(),args.GetProperty("height").GetInt32());preferences.SaveResolution(resolution);return clients.ResizeOpenDesktops(resolution);
            case "account": return await RequireSession().RequestAsync<AccountInfo>("account_info", null, lifetime.Token);
            case "desktop": await clients.OpenDesktopAsync(RequireSession(), lifetime.Token,preferences.ReadResolution()); return true;
            case "terminal": await clients.OpenTerminalAsync(RequireSession(), lifetime.Token); return RequireSession().Server;
            case "cloud.install":
                if(deploying)throw new InvalidOperationException("已有任务正在执行，请等待完成。");deploying=true;activeStep="cloud";
                try{return await Task.Run(()=>RequireSession().RequestAsync<ServerInfo>("cloud",null,lifetime.Token));}finally{deploying=false;activeStep="";}
            case "cloud.open":
                if (RequireSession().Server?.CloudReady != true) throw new InvalidOperationException("请先安装 CloudCLI。");
                if(deploying)throw new InvalidOperationException("请等待当前部署完成后打开 CloudCLI。");
                await RequireSession().RequestAsync<ServerInfo>("cloud_setup",null,lifetime.Token);
                uint cloudPort = await RequireSession().ForwardAsync(3001, lifetime.Token); WindowsClients.OpenUrl("http://127.0.0.1:" + cloudPort); return true;
            case "cloud.expose":
                if (RequireSession().Server?.CloudReady != true) throw new InvalidOperationException("请先安装 CloudCLI。");
                if (deploying) throw new InvalidOperationException("已有任务正在执行，请等待完成。");
                deploying = true; activeStep = "cloud_public";
                try
                {
                    string host = RequireSession().Host;
                    return await Task.Run(() => RequireSession().RequestAsync<JsonElement>("cloud_public", new { host }, lifetime.Token));
                }
                finally { deploying = false; activeStep = ""; }
            case "mount.settings": return RequireSession().ReadMountMappings();
            case "mount.install":
                if(deploying)throw new InvalidOperationException("已有任务正在执行，请等待完成。");deploying=true;activeStep="mount_install";
                try{return await Task.Run(()=>RequireSession().RequestAsync<ServerInfo>("mount_install",null,lifetime.Token));}finally{deploying=false;activeStep="";}
            case "mount.status": return session?.GetMountStatus()??new MountStatus(0,"idle","未挂载目录",0,0,0,0,0);
            case "mount.status.open": mountTray?.Open(); return true;
            case "mount.apply":
                var mappings=JsonSerializer.Deserialize<List<MountMapping>>(args.GetProperty("mappings"), JsonDefaults.Options) ?? [];
                if(deploying)throw new InvalidOperationException("已有任务正在执行，请等待完成。");deploying=true;activeStep="mount_apply";
                try{return await Task.Run(()=>RequireSession().ApplyMountsAsync(mappings,lifetime.Token));}finally{deploying=false;activeStep="";}
            case "ip.apply":
                if(deploying)throw new InvalidOperationException("已有任务正在执行，请等待完成。");deploying=true;activeStep="ip_policy";
                try
                {
                    var changed=await RequireSession().ApplyIpPolicyAsync(JsonSerializer.Deserialize<IpPolicySettings>(args,JsonDefaults.Options)??throw new ArgumentException("缺少 IP 设置。"),lifetime.Token);
                    try{var saved=LocalSecrets.Load();if(saved!=null)LocalSecrets.Save(saved with{Host=RequireSession().Host,Port=RequireSession().Port});}catch(Exception){QueueLog(activeStep,"服务器设置已保存；本机连接记忆未能更新，下次请填写新入口。");}
                    return changed;
                }
                finally{deploying=false;activeStep="";}
            default: throw new InvalidOperationException("未知客户端操作。");
        }
    }
    private async Task FlushLogsAsync()
    {
        List<object> lines=[];lock(logLock){while(logQueue.Count>0){var item=logQueue.Dequeue();lines.Add(new{step=item.Step,line=item.Line});}}
        if(lines.Count>0&&!lifetime.IsCancellationRequested&&browser.CoreWebView2!=null)try{await browser.CoreWebView2.ExecuteScriptAsync("window.safeCodeNativeLogs?.("+JsonSerializer.Serialize(lines,JsonDefaults.Options)+")");}catch{}
    }
    private void QueueLog(string step,string line)
    {
        lock(logLock)
        {
            string operation=step=="folder"?"directory_list":string.IsNullOrEmpty(step)?"connection":step;
            if(diagnosticTraces.TryGetValue(operation,out var diagnostic)||
                (operation=="connection"&&diagnosticTraces.TryGetValue("gateway_reconnect",out diagnostic)))diagnostic.Observe(line);
            logQueue.Enqueue((step,line));
            while(logQueue.Count>1000)logQueue.Dequeue();
        }
    }
    private SshSession RequireGatewaySession() => !connectionPending && session?.Server != null
        ? session : throw new InvalidOperationException("请先连接服务器。");
    private SshSession RequireSession() => !connectionPending&&session?.IsConnected == true&&session.Server!=null ? session : throw new InvalidOperationException("SSH 连接已断开，请重新连接。");
    private async Task RespondAsync(string id, object? result, string? error)
    {
        if (lifetime.IsCancellationRequested || browser.CoreWebView2 == null) return;
        string json = JsonSerializer.Serialize(new { id, result, error }, JsonDefaults.Options);
        await browser.CoreWebView2.ExecuteScriptAsync("window.safeCodeNativeResponse(" + json + ")");
    }
    private async Task SendLogAsync(string step, string line)
    {
        if(lifetime.IsCancellationRequested||browser.CoreWebView2==null)return;
        try{await browser.CoreWebView2.ExecuteScriptAsync("window.safeCodeNativeLog?.("+JsonSerializer.Serialize(step)+","+JsonSerializer.Serialize(line)+")");}catch{}
    }
    private async Task RunAcceptanceAsync(string configPath, string output)
    {
        try
        {
            string ready=await browser.CoreWebView2.ExecuteScriptAsync("Boolean(window.safeCodeNativeReady)");
            if(ready!="true")throw new InvalidOperationException("核心模块未加载，已阻止概念稿模拟逻辑。");
            var config = JsonDocument.Parse(File.ReadAllText(configPath));
            string result = await browser.CoreWebView2.ExecuteScriptAsync("window.safeCodeRunAcceptance(" + config.RootElement.GetRawText() + ")");
            File.WriteAllText(output + ".bootstrap.json", result);
            // 页面通过后续消息完成连接，截图由命令驱动的验收代码触发，凭据不写入图片或结果。
        }
        catch (Exception error) { File.WriteAllText(output + ".error.txt", error.Message); Environment.ExitCode=1; Close(); }
    }
    [DllImport("user32.dll")] private static extern bool ReleaseCapture();
    [DllImport("user32.dll")] private static extern IntPtr SendMessage(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam);
}
