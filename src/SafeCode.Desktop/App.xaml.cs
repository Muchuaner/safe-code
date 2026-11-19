using System.Windows;
namespace SafeCode.Desktop;
public partial class App : Application
{
    // All UI modes and Windows sessions share one machine-wide instance.
    private const string SingleInstanceMutexName = @"Global\SafeCode.Desktop.SingleInstance";
    private System.Threading.Mutex? singleInstance;
    private bool ownsSingleInstance;
    protected override async void OnStartup(StartupEventArgs e)
    {
        if(e.Args.Length==2&&e.Args[0]=="--host-bridge-install"){ShutdownMode=ShutdownMode.OnExplicitShutdown;int code=await HostBridgeInstaller.RunAsync(e.Args[1]);Shutdown(code);return;}
        DispatcherUnhandledException += (_, exception) => {
            string dir=System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode");System.IO.Directory.CreateDirectory(dir);
            System.IO.File.WriteAllText(System.IO.Path.Combine(dir,"last-error.txt"),exception.Exception.ToString());
        };
        if (!TryAcquireSingleInstance())
        {
            MessageBox.Show("Safe Code 已在运行，请使用已打开的程序，或从其所在 Windows 会话的右下角托盘显示主界面。", "Safe Code");
            Shutdown();
            return;
        }
        base.OnStartup(e);
        if(e.Args.Length>=3&&e.Args[0]=="--acceptance")System.IO.File.WriteAllText(e.Args[2]+".stage.log","启动\n");
        var window = new MainWindow(e.Args); MainWindow = window; window.Show();
    }
    protected override void OnExit(ExitEventArgs e)
    {
        try
        {
            if (ownsSingleInstance) singleInstance?.ReleaseMutex();
        }
        finally
        {
            ownsSingleInstance = false;
            singleInstance?.Dispose();
            singleInstance = null;
            base.OnExit(e);
        }
    }

    private bool TryAcquireSingleInstance()
    {
        try
        {
            singleInstance = new System.Threading.Mutex(false, SingleInstanceMutexName);
            try
            {
                ownsSingleInstance = singleInstance.WaitOne(0);
            }
            catch (System.Threading.AbandonedMutexException)
            {
                // Windows grants ownership when the previous owner exited unexpectedly.
                ownsSingleInstance = true;
            }
        }
        catch (UnauthorizedAccessException)
        {
            // Another Windows account or elevated instance owns the global mutex.
            // Do not bypass it by creating a different lock.
            ownsSingleInstance = false;
        }

        if (!ownsSingleInstance)
        {
            singleInstance?.Dispose();
            singleInstance = null;
        }
        return ownsSingleInstance;
    }
}
