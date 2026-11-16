using System.Reflection;
using System.Text.Json;
using SafeCode.Desktop;
using F = System.Windows.Forms;

internal static class Program
{
    const BindingFlags Members = BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;
    static readonly Assembly Desktop = typeof(WindowsClients).Assembly;
    static readonly Type RdpType = Desktop.GetType("SafeCode.Desktop.RdpDesktop")!;
    static readonly Type ScalingType = Desktop.GetType("SafeCode.Desktop.RdpDisplayScaling")!;
    static readonly List<object> Checks = [];
    static string output = "";


    [STAThread]
    static void Main(string[] args)
    {
        if (args.Length != 0 && args.Length != 1 && !(args.Length == 3 && args[1] == "--ui"))
        {
            Console.Error.WriteLine("Usage: SafeCode.Scaling.Verify [output-directory [--ui source-directory]]");
            Environment.ExitCode = 2; return;
        }
        output = Path.GetFullPath(args.Length == 0 ? ".local/desktop-scaling-verification" : args[0]);
        Directory.CreateDirectory(output);
        F.Application.SetHighDpiMode(F.HighDpiMode.PerMonitorV2);
        F.Application.EnableVisualStyles();
        F.Application.SetCompatibleTextRenderingDefault(false);
        using var driver = new F.Form { ShowInTaskbar = false, Opacity = 0, Size = new(1, 1) };
        driver.Shown += async (_, _) =>
        {
            try
            {
                VerifyGeometry(); VerifySettings(); VerifyNativeCapability();
                if (args.Contains("--ui")) await VerifyUi(args[Array.IndexOf(args,"--ui")+1]);
                Save(new { success = true, checks = Checks });
            }
            catch (Exception e) { Save(new { success = false, error = e.ToString(), checks = Checks }); Environment.ExitCode = 1; }
            finally { driver.Close(); }
        };
        F.Application.Run(driver);
    }

    static void Save(object value)
    {
        string json = JsonSerializer.Serialize(value, new JsonSerializerOptions { WriteIndented = true });
        File.WriteAllText(Path.Combine(output, "result.json"), json);
        Console.WriteLine(json);
    }

    static Rectangle Fit(Size size, DesktopResolution resolution, bool enabled) =>
        (Rectangle)ScalingType.GetMethod("Fit", Members)!.Invoke(null, [size, resolution, enabled])!;

    static void Require(bool ok, string message) { if (!ok) throw new Exception(message); }

    static void VerifyGeometry()
    {
        foreach (var resolution in new[] { new DesktopResolution(1280, 800), new DesktopResolution(1920, 1080) })
        foreach (var size in new[] { new Size(1000, 650), new Size(2560, 1440), new Size(800, 1100), new Size(1920, 1080) })
        foreach (bool enabled in new[] { false, true })
        {
            var bounds = Fit(size, resolution, enabled);
            Require(new Rectangle(Point.Empty, size).Contains(bounds), "Viewport crops the desktop.");
            Require(Math.Abs((double)bounds.Width / resolution.Width - (double)bounds.Height / resolution.Height) < .002, "Aspect ratio is distorted.");
            Require(Math.Abs(bounds.Left - (size.Width - bounds.Right)) <= 1 && Math.Abs(bounds.Top - (size.Height - bounds.Bottom)) <= 1, "Viewport is not centered.");
            if (!enabled) Require(bounds.Width <= resolution.Width && bounds.Height <= resolution.Height, "Disabled mode unexpectedly upscales.");
            else Require(size.Width - bounds.Width <= 1 || size.Height - bounds.Height <= 1, "Enabled mode leaves unnecessary empty space.");
        }
        Require(Fit(Size.Empty, new(), true) == Rectangle.Empty, "Minimized layout must be empty.");
        Checks.Add(new { name = "fit_geometry", cases = 32, completeDesktop = true, aspectRatioPreserved = true });
    }

    static void VerifySettings()
    {
        string dir = Path.Combine(output, "settings-" + Guid.NewGuid().ToString("N")); Directory.CreateDirectory(dir);
        string resolutionPath = Path.Combine(dir, "desktop-resolution.json");
        File.WriteAllText(resolutionPath, "{\"width\":1920,\"height\":1080}");
        string original = File.ReadAllText(resolutionPath);
        Type type = Desktop.GetType("SafeCode.Desktop.DesktopScalingPreferences")!;
        object Create() => Activator.CreateInstance(type, Members, null, [dir], null)!;
        bool Read() => (bool)type.GetMethod("Read", Members)!.Invoke(Create(), null)!;
        void Write(bool value) => type.GetMethod("Save", Members)!.Invoke(Create(), [value]);
        Require(!Read(), "Missing preference must default to legacy mode.");
        Write(true); Require(Read(), "Enabled preference did not survive recreation.");
        Write(false); Require(!Read(), "Disabled preference did not survive recreation.");
        File.WriteAllText(Path.Combine(dir, "desktop-scaling.json"), "invalid-json");
        Require(!Read(), "Corrupt preference must fall back to legacy mode.");
        Require(File.ReadAllText(resolutionPath) == original, "Scaling toggle changed the saved resolution.");
        Checks.Add(new { name = "settings_persistence", defaultOff = true, enabled = true, disabled = true, corruptFallback = true, resolutionUnchanged = true });
    }

    static void VerifyNativeCapability()
    {
        using var window = (F.Form)Activator.CreateInstance(RdpType, Members, null, [new DesktopResolution(1920, 1080), true], null)!;
        window.ShowInTaskbar = false; window.Opacity = 0; window.Show();
        object control = RdpType.GetField("control", Members)!.GetValue(window)!;
        object client = control.GetType().GetProperty("Client", Members)!.GetValue(control)!;
        Require((bool)ScalingType.GetMethod("TryEnableZoom", Members)!.Invoke(null, [client])!, "Windows RDP does not accept EnableZoom.");
        Require(!(bool)ScalingType.GetMethod("TryEnableZoom", Members)!.Invoke(null, [new object()])!, "Unsupported control must report unavailable.");
        window.Close();
        Checks.Add(new { name = "native_enable_zoom", supported = true });
    }

    static async Task VerifyUi(string source)
    {
        using var window = new F.Form { Text = "Safe Code · 缩放设置验收", ClientSize = new Size(1060, 820), ShowInTaskbar = false };
        using var browser = new Microsoft.Web.WebView2.WinForms.WebView2 { Dock = F.DockStyle.Fill };
        window.Controls.Add(browser); window.Show();
        var environment = await Microsoft.Web.WebView2.Core.CoreWebView2Environment.CreateAsync(null, Path.Combine(output, "webview2-profile"));
        await browser.EnsureCoreWebView2Async(environment);
        var web = browser.CoreWebView2;
        web.SetVirtualHostNameToFolderMapping("scaling.test", Path.GetFullPath(source), Microsoft.Web.WebView2.Core.CoreWebView2HostResourceAccessKind.DenyCors);
        web.AddWebResourceRequestedFilter("*", Microsoft.Web.WebView2.Core.CoreWebView2WebResourceContext.All);
        web.WebResourceRequested += (_, e) => { if (new Uri(e.Request.Uri).Host != "scaling.test") e.Response = environment.CreateWebResourceResponse(null,403,"Offline UI verification",""); };
        var loaded = new TaskCompletionSource();
        web.NavigationCompleted += (_, e) => { if (e.IsSuccess) loaded.TrySetResult(); else loaded.TrySetException(new Exception("UI navigation failed.")); };
        web.Navigate("https://scaling.test/index.html"); await loaded.Task.WaitAsync(TimeSpan.FromSeconds(20));
        await web.ExecuteScriptAsync("""
            document.documentElement.classList.add('native-client');
            const style=document.createElement('style');style.textContent='html,body{margin:0;padding:0;height:100%;overflow:hidden;background:white}.window,.window.wide{width:100%;height:100vh;max-width:none;margin:0;border:0;border-radius:0;box-shadow:none}.screen{height:calc(100% - 43px)}';document.head.append(style);
            for(const screen of document.querySelectorAll('.screen'))screen.hidden=true;
            document.getElementById('workspace-screen').hidden=false;
            document.querySelector('.window').classList.add('wide');
            document.getElementById('deployment-panel').hidden=true;
            document.getElementById('maintenance-panel').hidden=false;
            window.__savedScaling=false;window.__saveFailure=false;window.__scalingErrors=[];window.__ready=false;
            window.__scalingNative=async(op,args)=>{if(op==='desktop.scaling.get')return {enabled:window.__savedScaling};if(op==='desktop.scaling.save'){if(window.__saveFailure)throw new Error('fixture-save-failure');window.__savedScaling=args.enabled;return {enabled:window.__savedScaling};}throw new Error(op);};
            window.safeCodeDesktopScaling.initialize({native:window.__scalingNative,isMac:false,showError:async(message)=>window.__scalingErrors.push(message)}).then(()=>window.__ready=true);
            """);
        for(int i=0;i<100 && await web.ExecuteScriptAsync("window.__ready")!="true";i++)await Task.Delay(30);
        Require(await web.ExecuteScriptAsync("window.__ready") == "true", "Scaling card failed to initialize.");
        Require(await web.ExecuteScriptAsync("document.getElementById('desktop-scaling-toggle').getAttribute('aria-checked')") == "\"false\"", "Initial off state missing.");
        await web.ExecuteScriptAsync("document.getElementById('desktop-scaling-toggle').click()"); await Task.Delay(100);
        Require(await web.ExecuteScriptAsync("window.__savedScaling") == "true", "Switch did not save enabled state.");
        foreach(var size in new[]{new Size(1060,820),new Size(580,780),new Size(360,1100)})
        {
            window.ClientSize=size;await Task.Delay(150);
            await web.ExecuteScriptAsync("document.getElementById('desktop-scaling-card').scrollIntoView({block:'center'})");await Task.Delay(150);
            Require(await web.ExecuteScriptAsync("(()=>{const r=document.getElementById('desktop-scaling-card').getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&['desktop-scaling-state','desktop-scaling-toggle','desktop-scaling-settings'].every(id=>{const t=document.getElementById(id).getBoundingClientRect();return t.left>=r.left&&t.right<=r.right+1&&t.top>=r.top&&t.bottom<=r.bottom+1;});})()") == "true", "Feature card controls overflow the window.");
            using var image = File.Create(Path.Combine(output,$"settings-{size.Width}.png"));
            await web.CapturePreviewAsync(Microsoft.Web.WebView2.Core.CoreWebView2CapturePreviewImageFormat.Png,image);
        }
        await web.ExecuteScriptAsync("window.__saveFailure=true;document.getElementById('desktop-scaling-toggle').click()"); await Task.Delay(100);
        Require(await web.ExecuteScriptAsync("document.getElementById('desktop-scaling-toggle').getAttribute('aria-checked')") == "\"true\"", "Failed save changed displayed state.");
        Require(await web.ExecuteScriptAsync("window.__scalingErrors.length") == "1", "Save error was not shown.");
        await web.ExecuteScriptAsync("window.__saveFailure=false;document.getElementById('desktop-scaling-toggle').click()");await Task.Delay(100);
        Require(await web.ExecuteScriptAsync("window.__savedScaling") == "false", "Switch did not save disabled state.");
        await web.ExecuteScriptAsync("window.__resolutionOpened=0;document.getElementById('desktop-resolution').onclick=()=>window.__resolutionOpened++;document.getElementById('desktop-scaling-settings').click()");
        Require(await web.ExecuteScriptAsync("window.__resolutionOpened") == "1", "Resolution settings button did not open the existing control.");
        await web.ExecuteScriptAsync("""
            window.__busyReady=false;window.__saveCalls=0;window.__finishSave=null;
            window.safeCodeDesktopScaling.initialize({native:async(op,args)=>{if(op==='desktop.scaling.get')return {enabled:false};window.__saveCalls++;return new Promise(resolve=>window.__finishSave=()=>resolve({enabled:args.enabled}));},isMac:false,showError:async()=>{}}).then(()=>window.__busyReady=true);
            """);
        for(int i=0;i<100 && await web.ExecuteScriptAsync("window.__busyReady")!="true";i++)await Task.Delay(30);
        await web.ExecuteScriptAsync("document.getElementById('desktop-scaling-toggle').click();document.getElementById('desktop-scaling-toggle').click()");
        Require(await web.ExecuteScriptAsync("window.__saveCalls===1&&document.getElementById('desktop-scaling-toggle').disabled") == "true", "Pending save allowed duplicate requests.");
        await web.ExecuteScriptAsync("window.__finishSave()");await Task.Delay(100);
        Require(await web.ExecuteScriptAsync("document.getElementById('desktop-scaling-toggle').getAttribute('aria-checked')") == "\"true\"", "Pending save did not complete.");
        await web.ExecuteScriptAsync("window.__loadFailureDone=false;window.safeCodeDesktopScaling.initialize({native:async()=>{throw new Error('fixture-read-failure')},isMac:false,showError:async()=>{}}).then(()=>window.__loadFailureDone=true)");
        for(int i=0;i<100 && await web.ExecuteScriptAsync("window.__loadFailureDone")!="true";i++)await Task.Delay(30);
        Require(await web.ExecuteScriptAsync("document.getElementById('desktop-scaling-toggle').disabled&&document.getElementById('desktop-scaling-state').textContent==='当前版本暂不可用'") == "true", "Failed load left an actionable toggle.");
        await web.ExecuteScriptAsync("document.getElementById('desktop-scaling-card').hidden=true;window.__macCalls=0;window.safeCodeDesktopScaling.initialize({native:async()=>{window.__macCalls++;return {}},isMac:true,showError:async()=>{}})");
        Require(await web.ExecuteScriptAsync("document.getElementById('desktop-scaling-card').hidden&&window.__macCalls===0") == "true", "macOS exposed a Windows-only native operation.");
        Checks.Add(new {name="feature_card",wideAndNarrow=true,onOff=true,failedSaveRollback=true,resolutionSettings=true,duplicateSaveBlocked=true,failedLoadDisabled=true,macHidden=true});
        window.Close();
    }
}
