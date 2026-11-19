using System.ComponentModel;
using System.Drawing;
using System.Runtime.InteropServices;
using F = System.Windows.Forms;

namespace SafeCode.Desktop;

// Keep authentication in memory and use the existing, host-key-verified SSH tunnel.
internal sealed class RdpDesktop : F.Form
{
    private const int FullscreenHotkey = 0x5343;
    private readonly RdpControl control = new();
    private readonly F.Panel viewport = new() { Dock = F.DockStyle.Fill, BackColor = Color.Black };
    private readonly F.ToolStrip toolbar = new() { GripStyle = F.ToolStripGripStyle.Hidden, Dock = F.DockStyle.Top };
    private readonly F.ToolStripButton fullscreenButton = new("全屏") { ToolTipText = "全屏 / 退出全屏（Ctrl+Alt+Enter）" };
    private readonly F.ToolStripDropDownButton resizeMenu = new("显示模式");
    private readonly F.ToolStripLabel displayStatus = new("正在连接…");
    private readonly F.Timer displayTimer = new() { Interval = 250 };
    private dynamic? client;
    private DesktopResolution currentResolution;
    private DesktopResolution presetResolution;
    private RdpResizeMode resizeMode;
    private readonly RdpResizePreferences? resizePreferences;
    private readonly Action<Exception?>? displayOperationCompleted;
    private bool zoomSupported;
    private bool connectionConfigured;
    private Rectangle windowedBounds;
    private F.FormWindowState windowedState;
    private bool fullscreen;
    private bool hotkeyRegistered;
    private bool connected;
    private bool resizePending;
    private int resizeAttempts;
    private DateTime toolbarVisibleUntil;

    internal RdpDesktop(DesktopResolution? resolution = null, RdpResizePreferences? resizePreferences = null, Action<Exception?>? displayOperationCompleted = null)
    {
        this.resizePreferences = resizePreferences;
        this.displayOperationCompleted = displayOperationCompleted;
        resizeMode = resizePreferences?.Read() ?? RdpResizeMode.Original;
        currentResolution = presetResolution = resolution ?? new();
        Text = "Safe Code · 远程桌面";
        AutoScaleMode = F.AutoScaleMode.None;
        StartPosition = F.FormStartPosition.Manual;
        MinimumSize = new Size(480, 320);
        KeyPreview = true;
        var minimize = new F.ToolStripButton("最小化");
        var disconnect = new F.ToolStripButton("断开");
        fullscreenButton.Click += (_, _) => ApplyDisplayChange(ToggleFullscreen);
        minimize.Click += (_, _) => WindowState = F.FormWindowState.Minimized;
        disconnect.Click += (_, _) => Close();
        foreach (var (mode, label) in new[]
        {
            (RdpResizeMode.Original, "固定分辨率 · 原始大小"),
            (RdpResizeMode.Scale, "固定分辨率 · 等比缩放"),
            (RdpResizeMode.Dynamic, "动态分辨率 · 随窗口变化")
        })
        {
            var item = new F.ToolStripMenuItem(label) { Tag = mode };
            item.Click += (_, _) => ChangeResizeMode(mode);
            resizeMenu.DropDownItems.Add(item);
        }
        toolbar.Items.AddRange([fullscreenButton, minimize, disconnect, resizeMenu, new F.ToolStripSeparator(), displayStatus]);
        ((ISupportInitialize)control).BeginInit();
        viewport.Controls.Add(control);
        Controls.Add(viewport);
        Controls.Add(toolbar);
        ((ISupportInitialize)control).EndInit();
        ResizeForResolution(currentResolution);
        viewport.SizeChanged += (_, _) => { LayoutViewport(); if (resizeMode == RdpResizeMode.Dynamic) QueueDisplayUpdate(); };
        DpiChanged += (_, _) => { LayoutViewport(); if (resizeMode == RdpResizeMode.Dynamic) QueueDisplayUpdate(); };
        UpdateResizeMenu();
        displayTimer.Tick += (_, _) => UpdateDisplay();
        displayTimer.Start();
    }

    public void Connect(string host, uint port, string username, string password, DesktopResolution? resolution = null)
    {
        client = control.Client;
        client.Server = host;
        client.UserName = username;
        zoomSupported = RdpDisplayScaling.TryEnableZoom((object)client);
        connectionConfigured = true;
        currentResolution = resizeMode == RdpResizeMode.Dynamic ? ViewportResolution() : presetResolution;
        LayoutViewport();
        UpdateResizeMenu();
        client.DesktopWidth = currentResolution.Width;
        client.DesktopHeight = currentResolution.Height;
        dynamic advanced = client.AdvancedSettings7;
        advanced.RDPPort = (int)port;
        advanced.EnableCredSspSupport = false;
        // The outer SSH tunnel authenticates the remote server.
        advanced.AuthenticationLevel = 0;
        advanced.ClearTextPassword = password;
        advanced.SmartSizing = true;
        advanced.RedirectClipboard = true;
        client.Connect();
        ShowResolution();
        QueueDisplayUpdate();
    }

    private DesktopResolution ViewportResolution() => new(
        Math.Clamp(viewport.ClientSize.Width, 200, 8192),
        Math.Clamp(viewport.ClientSize.Height, 200, 8192));

    private void LayoutViewport()
    {
        if (WindowState == F.FormWindowState.Minimized) return;
        control.Bounds = resizeMode == RdpResizeMode.Dynamic
            ? viewport.ClientRectangle
            : RdpDisplayScaling.Fit(viewport.ClientSize, currentResolution, resizeMode == RdpResizeMode.Scale && zoomSupported);
    }

    private void ChangeResizeMode(RdpResizeMode selected)
    {
        if (resizeMode == selected) return;
        try { resizePreferences?.Save(selected); }
        catch (Exception error) when (error is System.IO.IOException or UnauthorizedAccessException)
        {
            displayOperationCompleted?.Invoke(error);
            F.MessageBox.Show(this, "无法保存显示模式，请检查本机应用数据目录。", Text);
            return;
        }
        ApplyDisplayChange(() =>
        {
            var previous = resizeMode;
            resizeMode = selected;
            UpdateResizeMenu();
            LayoutViewport();
            if (selected == RdpResizeMode.Dynamic || previous == RdpResizeMode.Dynamic)
                QueueDisplayUpdate();
            else
                ShowResolution();
        });
    }

    private void ApplyDisplayChange(Action change)
    {
        Exception? failure = null;
        try { change(); }
        catch (Exception error) { failure = error; throw; }
        finally { displayOperationCompleted?.Invoke(failure); }
    }

    private void ShowResolution()
    {
        displayStatus.Text = resizeMode == RdpResizeMode.Scale && connectionConfigured && !zoomSupported
            ? "当前组件仅支持缩小" : $"{currentResolution.Width} × {currentResolution.Height}";
    }

    private void UpdateResizeMenu()
    {
        foreach (F.ToolStripMenuItem item in resizeMenu.DropDownItems)
        {
            var mode = (RdpResizeMode)item.Tag!;
            item.Checked = mode == resizeMode;
            item.Enabled = mode != RdpResizeMode.Scale || !connectionConfigured || zoomSupported;
        }
    }

    private void QueueDisplayUpdate()
    {
        resizePending = true;
        resizeAttempts = 0;
    }

    private void UpdateDisplay()
    {
        if (IsDisposed || WindowState == F.FormWindowState.Minimized) return;
        if (fullscreen)
        {
            var point = PointToClient(F.Cursor.Position);
            bool atTop = ClientRectangle.Contains(point) && point.Y <= 5;
            if (atTop || resizeMenu.DropDown.Visible || (toolbar.Visible && toolbar.Bounds.Contains(point)))
                toolbarVisibleUntil = DateTime.UtcNow.AddSeconds(2);
            toolbar.Visible = DateTime.UtcNow < toolbarVisibleUntil;
            if (toolbar.Visible) toolbar.BringToFront();
        }
        if (client is null) return;
        try
        {
            bool nowConnected = client.Connected == 1;
            if (nowConnected && !connected) QueueDisplayUpdate();
            connected = nowConnected;
            if (!connected || !resizePending || resizeAttempts >= 20) return;
            resizeAttempts++;
            SynchronizeDisplay();
        }
        catch (COMException) { }
        catch (Microsoft.CSharp.RuntimeBinder.RuntimeBinderException) { }
    }

    private bool SynchronizeDisplay()
    {
        if (client is null || WindowState == F.FormWindowState.Minimized) return false;
        var size = resizeMode == RdpResizeMode.Dynamic ? ViewportResolution() : presetResolution;
        try
        {
            if (client.Connected != 1) return false;
            if (size != currentResolution)
            {
                uint physicalWidth = (uint)Math.Max(10, Math.Round(size.Width * 25.4 / Math.Max(96, DeviceDpi)));
                uint physicalHeight = (uint)Math.Max(10, Math.Round(size.Height * 25.4 / Math.Max(96, DeviceDpi)));
                client.UpdateSessionDisplaySettings((uint)size.Width, (uint)size.Height,
                    physicalWidth, physicalHeight, 0u, 100u, 100u);
                currentResolution = size;
                LayoutViewport();
            }
            resizePending = false;
            ShowResolution();
            return true;
        }
        catch (COMException) { displayStatus.Text = "分辨率调整未完成"; }
        catch (Microsoft.CSharp.RuntimeBinder.RuntimeBinderException) { displayStatus.Text = "服务器不支持自动调整"; }
        return false;
    }

    internal void ToggleFullscreen()
    {
        if (fullscreen) ExitFullscreen();
        else
        {
            var screen = F.Screen.FromControl(this).Bounds;
            windowedState = WindowState;
            windowedBounds = WindowState == F.FormWindowState.Normal ? Bounds : RestoreBounds;
            SuspendLayout();
            fullscreen = true;
            WindowState = F.FormWindowState.Normal;
            FormBorderStyle = F.FormBorderStyle.None;
            toolbar.Dock = F.DockStyle.None;
            toolbar.AutoSize = false;
            toolbar.Size = new Size(Math.Min(screen.Width, Math.Max(440, toolbar.PreferredSize.Width)), toolbar.PreferredSize.Height);
            Bounds = screen;
            toolbar.Location = new Point((ClientSize.Width - toolbar.Width) / 2, 0);
            fullscreenButton.Text = "退出全屏";
            toolbarVisibleUntil = DateTime.UtcNow.AddSeconds(3);
            toolbar.Visible = true;
            TopMost = true;
            ResumeLayout(true);
            toolbar.BringToFront();
            QueueDisplayUpdate();
        }
    }

    private void ExitFullscreen()
    {
        if (!fullscreen) return;
        SuspendLayout();
        fullscreen = false;
        TopMost = false;
        FormBorderStyle = F.FormBorderStyle.Sizable;
        toolbar.AutoSize = true;
        toolbar.Dock = F.DockStyle.Top;
        toolbar.Visible = true;
        // Docked controls are laid out in reverse z-order. Undo the overlay's BringToFront.
        toolbar.SendToBack();
        fullscreenButton.Text = "全屏";
        Bounds = windowedBounds;
        WindowState = windowedState;
        ResumeLayout(true);
        QueueDisplayUpdate();
    }

    internal void ResizeForResolution(DesktopResolution resolution)
    {
        ExitFullscreen();
        var area = F.Screen.FromPoint(Visible ? new Point(Left + Width / 2, Top + Height / 2) : F.Cursor.Position).WorkingArea;
        var frame = SizeFromClientSize(Size.Empty);
        int availableWidth = Math.Max(1, area.Width - frame.Width - 24);
        int availableHeight = Math.Max(1, area.Height - frame.Height - toolbar.Height - 24);
        double scale = Math.Min(1, Math.Min((double)availableWidth / resolution.Width, (double)availableHeight / resolution.Height));
        WindowState = F.FormWindowState.Normal;
        ClientSize = new Size(Math.Max(1, (int)Math.Floor(resolution.Width * scale)),
            Math.Max(1, (int)Math.Floor(resolution.Height * scale)) + toolbar.Height);
        Location = new Point(area.Left + (area.Width - Width) / 2, area.Top + (area.Height - Height) / 2);
        QueueDisplayUpdate();
    }

    internal bool ApplyResolution(DesktopResolution resolution)
    {
        presetResolution = resolution;
        ResizeForResolution(resolution);
        return SynchronizeDisplay();
    }

    protected override void OnActivated(EventArgs e)
    {
        base.OnActivated(e);
        if (!hotkeyRegistered) hotkeyRegistered = RegisterHotKey(Handle, FullscreenHotkey, 0x4003, 0x0D);
        if (fullscreen) TopMost = true;
    }

    protected override void OnDeactivate(EventArgs e)
    {
        if (hotkeyRegistered) { UnregisterHotKey(Handle, FullscreenHotkey); hotkeyRegistered = false; }
        if (fullscreen) TopMost = false;
        base.OnDeactivate(e);
    }

    protected override void WndProc(ref F.Message message)
    {
        if (message.Msg == 0x0312 && message.WParam.ToInt32() == FullscreenHotkey)
        { ApplyDisplayChange(ToggleFullscreen); return; }
        base.WndProc(ref message);
    }

    protected override void OnFormClosing(F.FormClosingEventArgs e)
    {
        displayTimer.Stop();
        if (hotkeyRegistered) { UnregisterHotKey(Handle, FullscreenHotkey); hotkeyRegistered = false; }
        try { if (client is not null) client.Disconnect(); } catch (COMException) { }
        base.OnFormClosing(e);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            if (hotkeyRegistered && IsHandleCreated) UnregisterHotKey(Handle, FullscreenHotkey);
            hotkeyRegistered = false;
            displayTimer.Dispose();
        }
        base.Dispose(disposing);
    }

    [DllImport("user32.dll")] private static extern bool RegisterHotKey(IntPtr handle, int id, uint modifiers, uint key);
    [DllImport("user32.dll")] private static extern bool UnregisterHotKey(IntPtr handle, int id);

    private sealed class RdpControl : F.AxHost
    {
        public RdpControl() : base("A0C63C30-F08D-4AB4-907C-34905D770C7D") { }
        public object Client => GetOcx() ?? throw new InvalidOperationException("无法创建 Windows 远程桌面组件。");
    }
}
