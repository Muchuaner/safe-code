using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;
using System.IO;
using System.Text.Json;
using System.Windows;

namespace SafeCode.Desktop;

// 使用系统窗口框架，拖动、关闭和缩放不依赖主界面的 HTML 对话框。
public sealed class GuideWindow : Window
{
    private readonly WebView2 browser = new();
    private readonly TaskCompletionSource<bool> completion = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private bool confirmed;

    public GuideWindow(CoreWebView2Environment environment, bool connecting)
    {
        Title = "Claude 防封与安全避坑指南";
        Width = Math.Min(560, SystemParameters.WorkArea.Width);
        Height = Math.Min(670, SystemParameters.WorkArea.Height);
        MinWidth = 320; MinHeight = 300;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        Content = browser;
        Closed += (_, _) => { browser.Dispose(); completion.TrySetResult(confirmed); };
        Loaded += async (_, _) =>
        {
            try
            {
                await browser.EnsureCoreWebView2Async(environment);
                if (!IsVisible) return;
                var core = browser.CoreWebView2;
                core.SetVirtualHostNameToFolderMapping("safe-code.local", Path.Combine(AppContext.BaseDirectory, "Web"), CoreWebView2HostResourceAccessKind.DenyCors);
                core.Settings.AreDevToolsEnabled = false;
                core.Settings.AreDefaultContextMenusEnabled = false;
                core.Settings.IsStatusBarEnabled = false;
                core.NavigationStarting += (_, e) => { if (!e.Uri.StartsWith("https://safe-code.local/guide.html", StringComparison.Ordinal)) e.Cancel = true; };
                core.NewWindowRequested += (_, e) => e.Handled = true;
                core.WebMessageReceived += (_, e) =>
                {
                    if (!e.Source.StartsWith("https://safe-code.local/guide.html", StringComparison.Ordinal)) return;
                    try { using var json = JsonDocument.Parse(e.WebMessageAsJson); confirmed = json.RootElement.GetProperty("confirmed").GetBoolean(); Close(); }
                    catch (JsonException) { }
                };
                browser.Source = new Uri("https://safe-code.local/guide.html?connecting=" + (connecting ? "1" : "0"));
            }
            catch (Exception error)
            {
                if (!IsVisible) return;
                MessageBox.Show(this, "指南加载失败。\n" + error.Message, "Safe Code", MessageBoxButton.OK, MessageBoxImage.Warning);
                Close();
            }
        };
    }

    internal async Task VerifyContentAsync(string action)
    {
        var deadline = DateTime.UtcNow.AddSeconds(15);
        while (browser.CoreWebView2 == null || await browser.CoreWebView2.ExecuteScriptAsync("!!document.querySelector('#guide-done-btn')") != "true")
        {
            if (DateTime.UtcNow > deadline) throw new TimeoutException("指南内容加载超时。");
            await Task.Delay(50);
        }
        var visible = await browser.CoreWebView2.ExecuteScriptAsync("(()=>{const b=document.querySelector('#guide-done-btn').getBoundingClientRect();return b.bottom<=innerHeight&&b.top>=0})()");
        if (visible != "true") throw new InvalidOperationException("指南确认按钮超出窗口。");
        if (action == "close") Close();
        else await browser.CoreWebView2.ExecuteScriptAsync("document.querySelector('#guide-" + (action == "confirm" ? "done" : "cancel") + "-btn').click()");
    }

    public Task<bool> OpenAsync() { Show(); return completion.Task; }
}
