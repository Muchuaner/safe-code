using System.IO;
using SafeCode.Core;
using System.Diagnostics;

namespace SafeCode.Desktop;

public sealed class WindowsClients : IDisposable
{
    private readonly List<Process> terminals = [];
    private readonly List<RdpDesktop> desktops=[];
    public async Task OpenDesktopAsync(SshSession session,CancellationToken ct,DesktopResolution? resolution=null)=>await LaunchDesktopAsync(session,ct,resolution);
    private async Task<RdpDesktop> LaunchDesktopAsync(SshSession session,CancellationToken ct,DesktopResolution? resolution=null)
    {
        if(session.Server?.DesktopReady!=true)throw new InvalidOperationException("请先安装虚拟桌面。");
        var account=await session.RequestAsync<AccountInfo>("account_info",null,ct);
        uint port=await session.ForwardAsync(3389,ct);
        var window=new RdpDesktop();desktops.Add(window);
        window.FormClosed+=(_,_)=>desktops.Remove(window);
        try{window.Show();window.Connect("127.0.0.1",port,account.Username,account.Password,resolution);return window;}catch{window.Close();window.Dispose();throw;}
    }
    public async Task<bool> VerifyDesktopLoginAsync(SshSession session,CancellationToken ct)
    {
        var before=await session.RequestAsync<System.Text.Json.JsonElement>("desktop_login_probe",null,ct);
        long offset=before.GetProperty("offset").GetInt64();
        using var desktop=await LaunchDesktopAsync(session,ct);
        try{for(int i=0;i<30;i++){await Task.Delay(1000,ct);var state=await session.RequestAsync<System.Text.Json.JsonElement>("desktop_login_probe",new{offset},ct);if(state.GetProperty("loggedIn").GetBoolean())return true;}throw new IOException("远程桌面未在 30 秒内完成自动登录。");}
        finally{desktop.Close();}
    }
    private static string NativeSshPath()
    {
        string path=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),"OpenSSH","ssh.exe");
        return File.Exists(path)?path:"ssh.exe";
    }
    private static string[] SshOptions(SshSession session,string username,bool interactive)
    {
        string config=Path.Combine(Path.GetDirectoryName(session.KnownHostsPath)!,"ssh-client-config");
        File.WriteAllText(config,"# Safe Code 单次连接不加载用户的通配主机配置。\n");
        return [interactive?"-t":"-T","-F",config,"-i",session.KeyPath,"-p",session.Port.ToString(),"-o","BatchMode=yes","-o","ForwardAgent=no","-o","IdentitiesOnly=yes","-o","StrictHostKeyChecking=yes","-o","UserKnownHostsFile="+session.KnownHostsPath,username+"@"+session.Host];
    }
    public async Task<bool> VerifyNativeSshAsync(SshSession session,CancellationToken ct)
    {
        var account=await session.RequestAsync<AccountInfo>("account_info",null,ct);session.CreateWorkspaceKey();
        var start=new ProcessStartInfo(NativeSshPath()){UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true};
        foreach(string arg in SshOptions(session,account.Username,false))start.ArgumentList.Add(arg);
        start.ArgumentList.Add("test -x .local/bin/safe-code-terminal && printf SAFE_CODE_NATIVE_SSH_OK");
        using var process=Process.Start(start)??throw new IOException("无法启动 OpenSSH 客户端。");
        var output=process.StandardOutput.ReadToEndAsync(ct);var error=process.StandardError.ReadToEndAsync(ct);
        using var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct);timeout.CancelAfter(TimeSpan.FromSeconds(30));
        try{await process.WaitForExitAsync(timeout.Token);}catch{if(!process.HasExited)process.Kill(true);throw;}
        if(process.ExitCode!=0||!(await output).Contains("SAFE_CODE_NATIVE_SSH_OK"))throw new IOException("Windows OpenSSH 验证失败："+await error);
        return true;
    }
    public async Task OpenTerminalAsync(SshSession session, CancellationToken ct)
    {
        if (session.Server?.Statuses.GetValueOrDefault("account") != true) throw new InvalidOperationException("请先部署工作用户。");
        await session.DeployAsync("ssh", null, ct: ct); await session.VerifyWorkspaceKeyAsync(ct);
        var account = await session.RequestAsync<AccountInfo>("account_info", null, ct);
        string ssh=NativeSshPath();
        string[] args=[..SshOptions(session,account.Username,true),".local/bin/safe-code-terminal"];
        var start=new ProcessStartInfo(ssh){UseShellExecute=true};
        foreach(string arg in args)start.ArgumentList.Add(arg);
        var process=Process.Start(start)??throw new IOException("无法打开 Windows 终端。");terminals.Add(process);
    }
    public static void OpenUrl(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) || (uri.Scheme != "https" && !(uri.Scheme == "http" && uri.IsLoopback)))
            throw new InvalidOperationException("拒绝打开不受支持的地址。");
        Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
    }
    public void Dispose()
    {
        foreach(var desktop in desktops.ToArray()){try{desktop.Close();desktop.Dispose();}catch{}}
        foreach(var terminal in terminals){try{if(!terminal.HasExited)terminal.Kill();terminal.Dispose();}catch{}}
    }
}
