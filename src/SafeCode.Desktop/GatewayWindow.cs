using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;
using SafeCode.Core;
using System.IO;
using System.Text.Json;
using System.Windows;

namespace SafeCode.Desktop;

internal sealed class GatewayWindow : Window
{
    private const string Page = "https://safe-code.local/web/gateway.html";
    private const string CreatorChannel = "https://space.bilibili.com/49985881";
    private readonly WebView2 browser = new();
    private readonly Func<JsonElement, Task<JsonElement>> manage;
    private bool closed;
    public bool Busy { get; private set; }

    public GatewayWindow(CoreWebView2Environment environment, Func<JsonElement, Task<JsonElement>> manage)
    {
        this.manage = manage;
        Title = "Safe Code API · 网关管理";
        UseLayoutRounding = SnapsToDevicePixels = true;
        Width = Math.Min(1100, SystemParameters.WorkArea.Width);
        Height = Math.Min(690, Math.Min(SystemParameters.WorkArea.Height, Width * 0.72));
        MinWidth = Math.Min(740, Width);
        MinHeight = Math.Min(420, Height);
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        ResizeMode = ResizeMode.CanResize;
        ShowInTaskbar = false;
        Content = browser;
        Loaded += async (_, _) => await InitializeAsync(environment);
        Closing += (_, e) => { if (Busy) e.Cancel = true; };
        Closed += (_, _) => { closed = true; browser.Dispose(); };
    }

    private async Task InitializeAsync(CoreWebView2Environment environment)
    {
        try
        {
            await browser.EnsureCoreWebView2Async(environment);
            if (closed) return;
            var view = browser.CoreWebView2;
            view.SetVirtualHostNameToFolderMapping("safe-code.local", Path.Combine(AppContext.BaseDirectory, "Web"), CoreWebView2HostResourceAccessKind.DenyCors);
            view.Settings.AreDefaultContextMenusEnabled = false;
            view.Settings.AreDevToolsEnabled = false;
            view.Settings.IsStatusBarEnabled = false;
            view.Settings.IsPasswordAutosaveEnabled = false;
            view.Settings.IsGeneralAutofillEnabled = false;
            view.NavigationStarting += (_, e) =>
            {
                if (e.Uri == Page) return;
                e.Cancel = true;
                if (e.IsUserInitiated && e.Uri == CreatorChannel)
                {
                    try { WindowsClients.OpenUrl(e.Uri); } catch { }
                }
            };
            view.NewWindowRequested += (_, e) => { e.Handled = true; };
            view.DownloadStarting += (_, e) => { e.Cancel = true; };
            view.WebMessageReceived += OnMessage;
            browser.Source = new Uri(Page);
        }
        catch (Exception error)
        {
            if (closed) return;
            MessageBox.Show(this, error.Message, Title, MessageBoxButton.OK, MessageBoxImage.Warning);
            Close();
        }
    }

    private async void OnMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        if (closed || e.Source != Page) return;
        string id = "";
        object? result = null;
        string? error = null;
        bool mutate = false;
        try
        {
            using var document = JsonDocument.Parse(e.WebMessageAsJson);
            var message = document.RootElement;
            id = message.GetProperty("id").GetString() ?? "";
            var args = message.GetProperty("args");
            switch (message.GetProperty("op").GetString())
            {
                case "safeCodeApi.manage":
                    if (Busy) throw new InvalidOperationException("请等待当前网关操作完成。");
                    mutate = args.GetProperty("action").GetString() is "issue" or "update" or "revoke" or "delete" or "set_port" or "set_origin" or "set_concurrency" or "refresh_ticket";
                    Busy = mutate;
                    result = await manage(args);
                    break;
                case "copy":
                    Clipboard.SetText(args.GetProperty("text").GetString() ?? "");
                    result = true;
                    break;
                default:
                    throw new InvalidOperationException("未知网关管理操作。");
            }
        }
        catch (Exception exception) { error = ConnectionErrors.DescribeOperation(exception); }
        finally { if (mutate) Busy = false; }

        if (closed || id.Length == 0) return;
        try
        {
            await browser.CoreWebView2.ExecuteScriptAsync(
                "window.safeCodeNativeResponse?.(" + JsonSerializer.Serialize(new { id, result, error }, JsonDefaults.Options) + ")");
        }
        catch (Exception exception) when (closed || exception is InvalidOperationException or System.Runtime.InteropServices.COMException) { }
    }
}
