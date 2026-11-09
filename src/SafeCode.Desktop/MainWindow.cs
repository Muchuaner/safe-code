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
    private CancellationTokenSource? browsing;
    private readonly System.Windows.Threading.DispatcherTimer logTimer=new(){Interval=TimeSpan.FromMilliseconds(120)};
    private string activeStep = "";
    private bool deploying;
    private bool closingApproved;
    private string? acceptanceFingerprint;
    private readonly string[] arguments;
    private readonly ConnectionSettings? supportSettings;
    private readonly string? supportFingerprint;
    private bool IsAcceptance=>arguments.Length>=3&&(arguments[0]=="--acceptance"||arguments[0]=="--services-acceptance"||arguments[0]=="--connection-acceptance"||arguments[0]=="--maintenance-acceptance");
    private readonly string dataDirectory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SafeCode");
    public MainWindow(string[] arguments)
    {
        if(arguments.Contains("--acceptance")||arguments.Contains("--services-acceptance"))dataDirectory=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode-Acceptance");
        if(arguments.Contains("--connection-acceptance")){dataDirectory=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode-Connection-Acceptance");LocalSecrets.DirectoryOverride=dataDirectory;}
        if(arguments.Contains("--maintenance-acceptance")){
            using var testConfig=JsonDocument.Parse(File.ReadAllText(arguments[1]));var target=testConfig.RootElement;supportSettings=LocalSecrets.Load();
            if(supportSettings==null||supportSettings.Host!=target.GetProperty("allowedHost").GetString()||!target.GetProperty("allowMaintenance").GetBoolean())throw new InvalidOperationException("维护验收需要明确指定已授权的测试目标。");
            supportFingerprint=new HostTrustStore(dataDirectory).Describe(supportSettings.Host,supportSettings.Port,"").PreviousFingerprint;
            if(string.IsNullOrEmpty(supportFingerprint)||supportFingerprint!=target.GetProperty("expectedFingerprint").GetString())throw new InvalidOperationException("验收目标指纹未匹配已确认记录。");
            dataDirectory=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode-Maintenance-Acceptance");LocalSecrets.DirectoryOverride=dataDirectory;
        }
        services=new ClientServices(dataDirectory);preferences=new ClientPreferences(dataDirectory);
        this.arguments = arguments; Title = "Safe Code"; Width = 360; Height = 536;
        MinWidth = 360; MinHeight = 500; WindowStartupLocation = WindowStartupLocation.CenterScreen;
        WindowStyle = WindowStyle.None; ResizeMode = ResizeMode.CanResize; Background = Brushes.White;
        Content = browser;
        logTimer.Tick+=async(_,_)=>await FlushLogsAsync();logTimer.Start();
        Loaded += async (_, _) => await InitializeAsync();
        Closing += async (_, e) =>
        {
            if (!closingApproved && session?.HasMounts == true)
            {
                e.Cancel = true;
                try { await session.UnmountAsync(); closingApproved = true; Close(); }
                catch (Exception error) { MessageBox.Show(this,"本地目录仍在同步，窗口暂未关闭。请关闭远端正在使用的文件后重试。\n"+error.Message,"目录卸载失败",MessageBoxButton.OK,MessageBoxImage.Warning); }
                return;
            }
            logTimer.Stop();lifetime.Cancel(); services.Dispose();clients.Dispose(); session?.Dispose(); browser.Dispose();
        };
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
                if (Uri.TryCreate(e.Uri, UriKind.Absolute, out var uri) && uri.Scheme == "https") WindowsClients.OpenUrl(e.Uri);
            };
            browser.CoreWebView2.NewWindowRequested += (_, e) => { e.Handled = true; try { WindowsClients.OpenUrl(e.Uri); } catch { } };
            browser.CoreWebView2.WebMessageReceived += OnMessage;
            browser.CoreWebView2.NavigationCompleted += async (_, e) =>
            {
                if(e.IsSuccess&&IsAcceptance){if(arguments[0]=="--maintenance-acceptance")await browser.CoreWebView2.ExecuteScriptAsync("window.safeCodeRunMaintenanceAcceptance()");else if(arguments[0]=="--connection-acceptance"){using var cfg=JsonDocument.Parse(File.ReadAllText(arguments[1]));await browser.CoreWebView2.ExecuteScriptAsync("window.safeCodeRunConnectionAcceptance("+cfg.RootElement.GetRawText()+")");}else if(arguments[0]=="--services-acceptance")await browser.CoreWebView2.ExecuteScriptAsync("window.safeCodeRunServicesAcceptance()");else await RunAcceptanceAsync(arguments[1],arguments[2]);}
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
        string id = "";
        try
        {
            using var document = JsonDocument.Parse(e.WebMessageAsJson); var root = document.RootElement;
            id = root.GetProperty("id").GetString() ?? ""; string operation = root.GetProperty("op").GetString() ?? "";
            if(IsAcceptance)File.AppendAllText(arguments[2]+".stage.log","RPC "+operation+"\n");
            var args = root.TryGetProperty("args", out var value) ? value : default;
            if(installingUpdate)throw new InvalidOperationException("正在准备安装更新，请稍候。");
            bool maintenance=operation is "cloud.install" or "mount.install" or "mount.apply" or "ip.apply" or "terminal";
            if(maintenance)maintenanceOperations++;
            object? answer;try{answer=await HandleAsync(operation,args);}finally{if(maintenance)maintenanceOperations--;}

            await FlushLogsAsync();
            await RespondAsync(id, answer, null);
        }
        catch (Exception error) { await RespondAsync(id, null, error.Message); }
    }
    private async Task<object?> HandleAsync(string operation, JsonElement args)
    {
        string Read(string key, string fallback = "") => args.ValueKind == JsonValueKind.Object && args.TryGetProperty(key, out var value) ? value.GetString() ?? fallback : fallback;
        switch (operation)
        {
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
            case "services.consent": return await services.SetConsentAsync(args.GetProperty("enabled").GetBoolean(),args.TryGetProperty("confirmed",out var consent)&&consent.GetBoolean());
            case "feedback.send": await services.SendFeedbackAsync(Read("message"),lifetime.Token);return true;
            case "updates.check": return await services.CheckUpdateAsync(args.TryGetProperty("force",out var force)&&force.GetBoolean(),lifetime.Token);
            case "updates.download":
                downloadedUpdate=await services.DownloadUpdateAsync((done,total)=>{_ = Dispatcher.BeginInvoke(async()=>{if(browser.CoreWebView2!=null&&!lifetime.IsCancellationRequested)await browser.CoreWebView2.ExecuteScriptAsync("window.safeCodeUpdateProgress?.("+done+","+total+")");});},lifetime.Token);return true;
            case "updates.install":
                if(deploying||maintenanceOperations>0)throw new InvalidOperationException("请等待当前操作完成后安装更新。");
                if(downloadedUpdate==null||!File.Exists(downloadedUpdate.Path))throw new InvalidOperationException("请先下载并校验安装包。");
                installingUpdate=true;
                try{await ClientServices.VerifyInstallerAsync(downloadedUpdate,lifetime.Token);if(session?.HasMounts==true)await session.UnmountAsync();
                Process.Start(new ProcessStartInfo(downloadedUpdate.Path){UseShellExecute=true});closingApproved=true;Close();return true;}
                catch{installingUpdate=false;throw;}
            case "login.load": return LocalSecrets.Load();
            case "pickFolder": var dialog = new OpenFolderDialog { Title = "选择要挂载到 VPS 的本地目录", InitialDirectory=Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments) }; return dialog.ShowDialog(this) == true ? dialog.FolderName : null;
            case "acceptance.capture":
                if(!IsAcceptance)throw new InvalidOperationException("未启用验收模式。");
                string tag=Read("tag");if(tag!="login"&&tag!="notice"&&tag!="deployment"&&tag!="mount"&&tag!="custom"&&tag!="services"&&tag!="loading"&&tag!="error"&&tag!="advanced"&&tag!="trust"&&tag!="resolution"&&tag!="draft"&&tag!="install"&&tag!="folder"&&tag!="maintenance")throw new ArgumentException("截图标识无效。");
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
                string noticePath=Path.Combine(dataDirectory,"notice-accepted-v1");bool noticeAccepted=File.Exists(noticePath)||services.NoticeWasConfirmed;if(noticeAccepted&&!File.Exists(noticePath))File.WriteAllText(noticePath,DateTimeOffset.UtcNow.ToString("O"));return new{accepted=noticeAccepted};
            case "notice.accept": File.WriteAllText(Path.Combine(dataDirectory,"notice-accepted-v1"),DateTimeOffset.UtcNow.ToString("O"));return true;
            case "connect":
                if(connectionPending)throw new InvalidOperationException("连接正在处理中，请稍候。");
                connectionPending=true;try{connecting?.Dispose();connecting=CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);connecting.CancelAfter(TimeSpan.FromSeconds(90));
                session?.Dispose(); session = new SshSession(dataDirectory);
                session.Log += text => {if(IsAcceptance)File.AppendAllText(arguments[2]+".stage.log","SSH "+text+"\n");QueueLog(activeStep,text);};
                session.DirectoryLog+=text=>QueueLog("folder",text);
                var settings = new ConnectionSettings(Read("host"), Read("username"), Read("password"), args.TryGetProperty("port", out var port) ? port.GetInt32() : 22);
                    var current=session;var server=await Task.Run(()=>current.ConnectAsync(settings,host=>acceptanceFingerprint!=null&&acceptanceFingerprint==host.Fingerprint,connecting.Token),connecting.Token);
                    pendingHostTrust=null;LocalSecrets.Save(settings);return new{ready=true,server};
                }catch(HostTrustRequiredException error){string token=Guid.NewGuid().ToString("N");pendingHostTrust=(token,error.Key);return new{requiresTrust=true,token,key=error.Key};}
                catch(Exception error){pendingHostTrust=null;throw new InvalidOperationException(ConnectionErrors.Describe(error));}
                finally{connectionPending=false;}

            case "job.resume":
                if(deploying)throw new InvalidOperationException("已有任务正在监控。");deploying=true;activeStep=Read("operation");
                try{return await RequireSession().ResumeJobAsync(Read("jobId"),lifetime.Token);}finally{deploying=false;activeStep="";}
            case "inspect": return await RequireSession().RequestAsync<ServerInfo>("inspect", null, lifetime.Token);
            case "ip.info": return await IpIntelligenceService.QueryAsync(Read("ip"),lifetime.Token);
            case "metrics": return await RequireSession().MeasureNetworkAsync(lifetime.Token);
            case "deploy":
                if (deploying) throw new InvalidOperationException("另一个任务正在执行，请等待。");
                deploying = true; activeStep = Read("step");
                try
                {
                    var locale = args.TryGetProperty("locale", out var region) && region.ValueKind != JsonValueKind.Null ? JsonSerializer.Deserialize<LocaleSettings>(region, JsonDefaults.Options) : null;
                    var draft=activeStep=="account"?preferences.ReadDraft(RequireSession()):null;
                    var result = await RequireSession().DeployAsync(activeStep, locale, draft?.Username??Read("username", "workspace"), lifetime.Token, draft?.Password??Read("password"), draft?.Hostname??Read("hostname"));
                    if(activeStep=="account"&&result.Statuses.GetValueOrDefault("account"))preferences.ClearDraft(RequireSession());
                    if (activeStep == "ssh") await RequireSession().VerifyWorkspaceKeyAsync(lifetime.Token);
                    return result;
                }
                finally { deploying = false; activeStep = ""; }
            case "folder.cancel": try{browsing?.Cancel();}catch(ObjectDisposedException){}return true;
            case "folder.list":
                try{browsing?.Cancel();}catch(ObjectDisposedException){}browsing=CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
                using(var timeout=browsing){timeout.CancelAfter(TimeSpan.FromSeconds(12));try{return await RequireSession().ListDirectoriesAsync(Read("path"),timeout.Token);}catch(Exception error)when(error is OperationCanceledException or Renci.SshNet.Common.SshOperationTimeoutException){throw new InvalidOperationException("读取目录超时，请检查网络或挂载目录，稍后重试。");}}
            case "account.draft.get": return preferences.ReadDraft(RequireSession());
            case "account.draft.save": preferences.SaveDraft(RequireSession(),new(Read("username"),Read("hostname"),Read("password")));return true;
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
                uint cloudPort = await RequireSession().ForwardAsync(3001, lifetime.Token); WindowsClients.OpenUrl("http://127.0.0.1:" + cloudPort); return true;
            case "mount.settings": return RequireSession().ReadMountMappings();
            case "mount.install":
                if(deploying)throw new InvalidOperationException("已有任务正在执行，请等待完成。");deploying=true;activeStep="mount_install";
                try{return await Task.Run(()=>RequireSession().RequestAsync<ServerInfo>("mount_install",null,lifetime.Token));}finally{deploying=false;activeStep="";}
            case "mount.apply":
                var mappings=JsonSerializer.Deserialize<List<MountMapping>>(args.GetProperty("mappings"), JsonDefaults.Options) ?? [];
                if(deploying)throw new InvalidOperationException("已有任务正在执行，请等待完成。");deploying=true;activeStep="mount_apply";
                try{return await Task.Run(()=>RequireSession().ApplyMountsAsync(mappings,lifetime.Token));}finally{deploying=false;activeStep="";}
            case "ip.apply": return await RequireSession().ApplyIpPolicyAsync(JsonSerializer.Deserialize<IpPolicySettings>(args, JsonDefaults.Options) ?? throw new ArgumentException("缺少 IP 设置。"), lifetime.Token);
            default: throw new InvalidOperationException("未知客户端操作。");
        }
    }
    private async Task FlushLogsAsync()
    {
        List<object> lines=[];lock(logLock){while(logQueue.Count>0){var item=logQueue.Dequeue();lines.Add(new{step=item.Step,line=item.Line});}}
        if(lines.Count>0&&!lifetime.IsCancellationRequested&&browser.CoreWebView2!=null)try{await browser.CoreWebView2.ExecuteScriptAsync("window.safeCodeNativeLogs?.("+JsonSerializer.Serialize(lines,JsonDefaults.Options)+")");}catch{}
    }
    private void QueueLog(string step,string line){lock(logLock){logQueue.Enqueue((step,line));while(logQueue.Count>1000)logQueue.Dequeue();}}
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
