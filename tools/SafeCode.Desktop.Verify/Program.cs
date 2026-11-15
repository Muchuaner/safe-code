using System.Reflection;
using System.Runtime.InteropServices;
using SafeCode.Desktop;

// Interactive Windows desktop required. No SSH, credentials, or server access.
// Real WinForms/ActiveX layout; a recording client exercises display-update failures.
internal static class Program
{
    private const BindingFlags Members = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
    private static readonly Type DesktopType = typeof(WindowsClients).Assembly.GetType("SafeCode.Desktop.RdpDesktop")!;

    [STAThread]
    private static int Main()
    {
        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        try { Verify(); Console.WriteLine("DESKTOP_LAYOUT_AND_DISPLAY_UPDATES_OK"); return 0; }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }

    private static object Field(Form window, string name) => DesktopType.GetField(name, Members)!.GetValue(window)!;
    private static object? Call(Form window, string name, params object[] args) => DesktopType.GetMethod(name, Members)!.Invoke(window, args);
    private static void Check(bool condition, string message) { if (!condition) throw new InvalidOperationException(message); }

    private static void Verify()
    {
        using var window = (Form)Activator.CreateInstance(DesktopType, Members, null, [new DesktopResolution(1024, 768)], null)!;
        window.Show();
        Application.DoEvents();
        var viewport = (Control)Field(window, "control");
        var toolbar = (ToolStrip)Field(window, "toolbar");
        var button = (ToolStripButton)Field(window, "fullscreenButton");
        var timer = (System.Windows.Forms.Timer)Field(window, "displayTimer");
        timer.Stop(); // Advance ticks deterministically below.
        var client = new RecordingClient(viewport.ClientSize);
        DesktopType.GetField("client", Members)!.SetValue(window, client);
        void Tick() => Call(window, "UpdateDisplay");
        void Match(string name)
        {
            Tick();
            Check(client.LastSize == viewport.ClientSize, name + ": remote size differs from viewport");
            Console.WriteLine("PASS " + name + " " + viewport.ClientSize);
        }

        Match("initial_window");
        window.ClientSize = new Size(900, 650);
        Match("manual_resize");
        int stableCalls = client.Attempts;
        for (int i = 0; i < 8; i++) Tick();
        Check(client.Attempts == stableCalls, "Idle window repeatedly updates the display");

        Rectangle normalBounds = window.Bounds;
        button.PerformClick();
        Rectangle screen = Screen.FromControl(window).Bounds;
        Check(window.Bounds == screen && viewport.ClientSize == screen.Size, "Fullscreen does not fill the monitor");
        Match("fullscreen_from_normal");
        button.PerformClick();
        Check(window.Bounds == normalBounds && window.WindowState == FormWindowState.Normal, "Normal bounds were not restored");
        Check(toolbar.Visible && viewport.Top >= toolbar.Bottom, "Toolbar overlaps the restored viewport");
        Match("restore_normal");

        window.WindowState = FormWindowState.Maximized;
        Application.DoEvents();
        Match("maximized");
        Size maximizedSize = viewport.ClientSize;
        button.PerformClick();
        Match("fullscreen_from_maximized");
        button.PerformClick();
        Check(window.WindowState == FormWindowState.Maximized && viewport.ClientSize == maximizedSize, "Maximized layout was not restored");
        Match("restore_maximized");

        window.WindowState = FormWindowState.Minimized;
        Application.DoEvents();
        int beforeMinimized = client.Attempts;
        Tick();
        Check(client.Attempts == beforeMinimized, "Minimizing updated the remote display");
        window.WindowState = FormWindowState.Normal;
        Application.DoEvents();
        Call(window, "ApplyResolution", new DesktopResolution(2560, 1600));
        Check(Screen.FromControl(window).WorkingArea.Contains(window.Bounds), "Large preset exceeds working area");
        Match("large_preset");

        client.Fail = true;
        window.ClientSize = new Size(800, 600);
        int beforeFailure = client.Attempts;
        for (int i = 0; i < 30; i++) Tick();
        Check(client.Attempts - beforeFailure == 20, "Failed display updates did not stop after 20 attempts");
        client.Fail = false;
        window.ClientSize = new Size(820, 620);
        Match("recover_after_resize");

        client.Connected = 0;
        Tick();
        window.ClientSize = new Size(840, 640);
        int beforeReconnect = client.Attempts;
        Tick();
        Check(client.Attempts == beforeReconnect, "Disconnected client received a display update");
        client.Connected = 1;
        Match("reconnect");

        window.Close();
        Check(client.Disconnected && !timer.Enabled, "Closing did not disconnect and stop timer");
        Check(!(bool)Field(window, "hotkeyRegistered"), "Closing leaked the fullscreen hotkey");
    }
}

// Public because the production code invokes the COM-shaped API through dynamic.
public sealed class RecordingClient(Size initialSize)
{
    public int Connected { get; set; } = 1;
    public bool Fail { get; set; }
    public int Attempts { get; private set; }
    public Size LastSize { get; private set; } = initialSize;
    public bool Disconnected { get; private set; }

    public void UpdateSessionDisplaySettings(uint width, uint height, uint physicalWidth, uint physicalHeight,
        uint orientation, uint desktopScale, uint deviceScale)
    {
        Attempts++;
        if (Fail) throw new COMException("Simulated unsupported display update");
        if (physicalWidth == 0 || physicalHeight == 0 || desktopScale != 100 || deviceScale != 100)
            throw new InvalidOperationException("Invalid display-update parameters");
        LastSize = new Size((int)width, (int)height);
    }

    public void Disconnect() { Connected = 0; Disconnected = true; }
}
