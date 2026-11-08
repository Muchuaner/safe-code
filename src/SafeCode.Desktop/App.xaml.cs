using System.Windows;
namespace SafeCode.Desktop;
public partial class App : Application
{
    private System.Threading.Mutex? singleInstance;
    protected override void OnStartup(StartupEventArgs e)
    {
        DispatcherUnhandledException += (_, exception) => {
            string dir=System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode");System.IO.Directory.CreateDirectory(dir);
            System.IO.File.WriteAllText(System.IO.Path.Combine(dir,"last-error.txt"),exception.Exception.ToString());
        };
        singleInstance=new System.Threading.Mutex(true,"Local\\SafeCode-"+System.Security.Principal.WindowsIdentity.GetCurrent().User?.Value,out bool created);
        if(!created){MessageBox.Show("Safe Code 已在运行，请切换到已有窗口。","Safe Code");Shutdown();return;}
        base.OnStartup(e);
        if(e.Args.Length>=3&&e.Args[0]=="--acceptance")System.IO.File.WriteAllText(e.Args[2]+".stage.log","启动\n");
        var window = new MainWindow(e.Args); MainWindow = window; window.Show();
    }
    protected override void OnExit(ExitEventArgs e)
    {
        try{singleInstance?.ReleaseMutex();}catch{}singleInstance?.Dispose();base.OnExit(e);
    }
}
