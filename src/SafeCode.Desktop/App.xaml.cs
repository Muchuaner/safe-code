using System.Windows;
namespace SafeCode.Desktop;
public partial class App : Application
{
    private System.Threading.Mutex? singleInstance;
    protected override async void OnStartup(StartupEventArgs e)
    {
        if(e.Args.Length==2&&e.Args[0]=="--host-bridge-install"){ShutdownMode=ShutdownMode.OnExplicitShutdown;int code=await HostBridgeInstaller.RunAsync(e.Args[1]);Shutdown(code);return;}
        DispatcherUnhandledException += (_, exception) => {
            string dir=System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode");System.IO.Directory.CreateDirectory(dir);
            System.IO.File.WriteAllText(System.IO.Path.Combine(dir,"last-error.txt"),exception.Exception.ToString());
        };
        singleInstance=new System.Threading.Mutex(true,"Local\\SafeCode-"+((e.Args.Contains("--acceptance")||e.Args.Contains("--services-acceptance")||e.Args.Contains("--connection-acceptance")||e.Args.Contains("--maintenance-acceptance")||e.Args.Contains("--ip-acceptance")||e.Args.Contains("--updates-acceptance"))?"Acceptance-":"")+System.Security.Principal.WindowsIdentity.GetCurrent().User?.Value,out bool created);
        if(!created){MessageBox.Show("Safe Code 已在运行，可从右下角托盘显示主界面。","Safe Code");Shutdown();return;}
        base.OnStartup(e);
        if(e.Args.Length>=3&&e.Args[0]=="--acceptance")System.IO.File.WriteAllText(e.Args[2]+".stage.log","启动\n");
        var window = new MainWindow(e.Args); MainWindow = window; window.Show();
    }
    protected override void OnExit(ExitEventArgs e)
    {
        try{singleInstance?.ReleaseMutex();}catch{}singleInstance?.Dispose();base.OnExit(e);
    }
}
