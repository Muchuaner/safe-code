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
    private readonly CancellationTokenSource lifetime = new();
    private SshSession? session;
    private readonly object logLock=new();
    private (string Step,string Line)? latestLog;
    private readonly System.Windows.Threading.DispatcherTimer logTimer=new(){Interval=TimeSpan.FromMilliseconds(120)};
    private string activeStep = "";
    private bool deploying;
    private bool closingApproved;
    private string? acceptanceFingerprint;
    private readonly string[] arguments;
    private readonly string dataDirectory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SafeCode");
    public MainWindow(string[] arguments)
    {
        this.arguments = arguments; Title = "Safe Code"; Width = 360; Height = 536;
        MinWidth = 360; MinHeight = 500; WindowStartupLocation = WindowStartupLocation.CenterScreen;
        WindowStyle = WindowStyle.None; ResizeMode = ResizeMode.CanResize; Background = Brushes.White;
        Content = browser;
        logTimer.Tick+=async(_,_)=>{(string Step,string Line)? item;lock(logLock){item=latestLog;latestLog=null;}if(item is {} value)await SendLogAsync(value.Step,value.Line);};logTimer.Start();
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
            logTimer.Stop();lifetime.Cancel(); clients.Dispose(); session?.Dispose(); browser.Dispose();
        };
    }
    private async Task InitializeAsync()
    {
        try
        {
            if(arguments.Length>=3&&arguments[0]=="--acceptance")File.AppendAllText(arguments[2]+".stage.log","窗口已加载\n");
            Directory.CreateDirectory(dataDirectory);
            var options = new CoreWebView2EnvironmentOptions();
            if(arguments.Length>=3&&arguments[0]=="--acceptance")options.AdditionalBrowserArguments="--disable-gpu";
            var environment = await CoreWebView2Environment.CreateAsync(null, Path.Combine(dataDirectory, "WebView"), options);
            await browser.EnsureCoreWebView2Async(environment);
            if(arguments.Length>=3&&arguments[0]=="--acceptance")File.AppendAllText(arguments[2]+".stage.log","WebView 已就绪\n");
            if (arguments.Length >= 3 && arguments[0] == "--acceptance")
            { using var cfg = JsonDocument.Parse(File.ReadAllText(arguments[1])); acceptanceFingerprint = cfg.RootElement.GetProperty("fingerprint").GetString(); }
            browser.CoreWebView2.SetVirtualHostNameToFolderMapping("safe-code.local", Path.Combine(AppContext.BaseDirectory, "Web"), CoreWebView2HostResourceAccessKind.DenyCors);
            await browser.CoreWebView2.Profile.ClearBrowsingDataAsync(CoreWebView2BrowsingDataKinds.DiskCache);
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
                if (e.IsSuccess && arguments.Length >= 3 && arguments[0] == "--acceptance") await RunAcceptanceAsync(arguments[1], arguments[2]);
            };
            browser.Source = new Uri("https://safe-code.local/index.html");
        }
        catch (Exception error)
        {
            if(arguments.Length>=3&&arguments[0]=="--acceptance")File.WriteAllText(arguments[2]+".error.txt",error.ToString());
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
            var args = root.TryGetProperty("args", out var value) ? value : default;
            var answer = await HandleAsync(operation, args);
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
            case "pickFolder": var dialog = new OpenFolderDialog { Title = "选择要挂载到 VPS 的本地目录" }; return dialog.ShowDialog(this) == true ? dialog.FolderName : null;
            case "acceptance.capture":
                if(arguments.Length<3||arguments[0]!="--acceptance")throw new InvalidOperationException("未启用验收模式。");
                string tag=Read("tag");if(tag!="login"&&tag!="notice")throw new ArgumentException("截图标识无效。");
                await Task.Delay(200);using(var image=File.Create(arguments[2]+"."+tag+".png"))await browser.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,image);return true;
            case "client.verify":
                if(arguments.Length<3||arguments[0]!="--acceptance")throw new InvalidOperationException("未启用验收模式。");
                return new{nativeSsh=await clients.VerifyNativeSshAsync(RequireSession(),lifetime.Token),credentialStore=await clients.VerifyCredentialStoreAsync(RequireSession(),lifetime.Token)};
            case "acceptance.done":
                if (arguments.Length < 3 || arguments[0] != "--acceptance") throw new InvalidOperationException("当前未启用验收模式。");
                string output = arguments[2]; Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(output))!);
                File.WriteAllText(output + ".json", args.GetRawText());
                await Task.Delay(400);
                using (var file = File.Create(output + ".png")) await browser.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, file);
                Environment.ExitCode = args.GetProperty("success").GetBoolean() ? 0 : 1;
                _ = Dispatcher.BeginInvoke(() => Close()); return true;
            case "connect":
                session?.Dispose(); session = new SshSession(dataDirectory);
                session.Log += text => {lock(logLock)latestLog=(activeStep,text);};
                var settings = new ConnectionSettings(Read("host"), Read("username"), Read("password"), args.TryGetProperty("port", out var port) ? port.GetInt32() : 22);
                var server = await session.ConnectAsync(settings, host => acceptanceFingerprint != null ? acceptanceFingerprint == host.Fingerprint : Dispatcher.Invoke(() =>
                    MessageBox.Show(this, "首次连接此服务器。请核对主机指纹：\n\n" + host.Host + ":" + host.Port + "\n" + host.Fingerprint + "\n\n是否信任并继续？", "核对 SSH 主机密钥", MessageBoxButton.YesNo, MessageBoxImage.Question) == MessageBoxResult.Yes), lifetime.Token);
                return server;
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
                    var result = await RequireSession().DeployAsync(activeStep, locale, Read("username", "workspace"), lifetime.Token);
                    if (activeStep == "ssh") await RequireSession().VerifyWorkspaceKeyAsync(lifetime.Token);
                    return result;
                }
                finally { deploying = false; activeStep = ""; }
            case "account": return await RequireSession().RequestAsync<AccountInfo>("account_info", null, lifetime.Token);
            case "desktop": await clients.OpenDesktopAsync(RequireSession(), lifetime.Token); return true;
            case "terminal": await clients.OpenTerminalAsync(RequireSession(), lifetime.Token); return RequireSession().Server;
            case "cloud.install": return await RequireSession().RequestAsync<ServerInfo>("cloud", null, lifetime.Token);
            case "cloud.open":
                if (RequireSession().Server?.CloudReady != true) throw new InvalidOperationException("请先安装 CloudCLI。");
                uint cloudPort = await RequireSession().ForwardAsync(3001, lifetime.Token); WindowsClients.OpenUrl("http://127.0.0.1:" + cloudPort); return true;
            case "mount.settings": return RequireSession().ReadMountMappings();
            case "mount.install": return await RequireSession().RequestAsync<ServerInfo>("mount_install", null, lifetime.Token);
            case "mount.apply": return await RequireSession().ApplyMountsAsync(JsonSerializer.Deserialize<List<MountMapping>>(args.GetProperty("mappings"), JsonDefaults.Options) ?? [], lifetime.Token);
            case "ip.apply": return await RequireSession().ApplyIpPolicyAsync(JsonSerializer.Deserialize<IpPolicySettings>(args, JsonDefaults.Options) ?? throw new ArgumentException("缺少 IP 设置。"), lifetime.Token);
            default: throw new InvalidOperationException("未知客户端操作。");
        }
    }
    private SshSession RequireSession() => session?.IsConnected == true ? session : throw new InvalidOperationException("SSH 连接已断开，请重新连接。");
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
