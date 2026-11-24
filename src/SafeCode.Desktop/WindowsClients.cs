using Microsoft.Win32;
using System.Security;
using System.IO;
using SafeCode.Core;
using System.Diagnostics;

namespace SafeCode.Desktop;

public sealed class WindowsClients : IDisposable
{
    private readonly List<Process> terminals = [];
    private readonly List<RdpDesktop> desktops=[];
    internal RdpResizePreferences? ResizePreferences { get; set; }
    internal Action<Exception?>? DisplayOperationCompleted { get; set; }
    public async Task OpenDesktopAsync(SshSession session,CancellationToken ct,DesktopResolution? resolution=null)=>await LaunchDesktopAsync(session,ct,resolution);
    private async Task<RdpDesktop> LaunchDesktopAsync(SshSession session,CancellationToken ct,DesktopResolution? resolution=null)
    {
        if(session.Server?.DesktopReady!=true)throw new InvalidOperationException("请先安装虚拟桌面。");
        var account=await session.RequestAsync<AccountInfo>("account_info",null,ct);
        var connection=await session.PrepareRemoteDesktopAsync(account.Username,false,ct);
        var window=new RdpDesktop(resolution,ResizePreferences,DisplayOperationCompleted);desktops.Add(window);
        window.FormClosed+=(_,_)=>desktops.Remove(window);
        try{window.Show();window.Connect("127.0.0.1",connection.Port,account.Username,account.Password,resolution);return window;}catch{window.Close();window.Dispose();throw;}
    }
    public object ResizeOpenDesktops(DesktopResolution resolution)
    {
        int updated=0,deferred=0;foreach(var desktop in desktops.ToArray()){if(desktop.IsDisposed)continue;if(desktop.ApplyResolution(resolution))updated++;else deferred++;}
        return new{windows=updated+deferred,dynamicUpdated=updated,deferred};
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
        return [interactive?"-t":"-T","-F",config,"-i",session.KeyPath,"-p",session.Port.ToString(),"-o","BatchMode=yes","-o","ForwardAgent=no","-o","IdentitiesOnly=yes","-o","StrictHostKeyChecking=yes","-o",SshClientOptions.UserKnownHostsFile(session.KnownHostsPath),username+"@"+session.Host];
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
    internal static async Task<string> ReopenClaudeAsync(DesktopKeyAccess access,CancellationToken ct)
    {
        using var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(TimeSpan.FromSeconds(15));
        var apps=Process.GetProcessesByName("Claude");
        var desktop=new List<Process>();
        using var self=Process.GetCurrentProcess();
        try
        {
            foreach(var app in apps)
            {
                try
                {
                    if(app.SessionId!=self.SessionId||app.HasExited)continue;
                    var path=app.MainModule?.FileName;
                    var local=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"AnthropicClaude");
                    if(path==null||!path.Contains(@"\WindowsApps\Claude_",StringComparison.OrdinalIgnoreCase)
                        &&!path.StartsWith(local+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase))continue;
                    desktop.Add(app);
                    if(app.MainWindowHandle!=IntPtr.Zero)app.CloseMainWindow();
                }
                catch(InvalidOperationException) when(app.HasExited){}
            }
            await Task.WhenAll(desktop.Select(app=>app.WaitForExitAsync(timeout.Token)));
        }
        catch(OperationCanceledException) when(!ct.IsCancellationRequested)
        {throw new InvalidOperationException("请完全退出 Claude Desktop，然后点击「打开 Claude Desktop」。");}
        finally{foreach(var app in apps)app.Dispose();}
        return await OpenClaudeAsync(access,ct);
    }
    internal static async Task<string> OpenClaudeAsync(DesktopKeyAccess access,CancellationToken ct)
    {
        if(!IsClaudeInstalled())throw new InvalidOperationException("未检测到 Claude Desktop，请先安装官方客户端，或从开始菜单启动。");
        await access.PrepareLaunchAsync(ct);
        try
        {
            // Use the registered app entry so the MSIX process reads its selected profile.
            // A claude:// deep link can start a separate consumer-mode process.
            var launch=new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows),"explorer.exe"))
            {UseShellExecute=false};
            launch.ArgumentList.Add(@"shell:AppsFolder\Claude_pzs8sxrjxfjjc!Claude");
            using var request=Process.Start(launch)??throw new IOException("无法启动 Windows 应用入口。");
            return "已请求打开官方 Claude Desktop。";
        }
        catch(Exception error) when(error is System.ComponentModel.Win32Exception or IOException)
        {
            throw new InvalidOperationException("未能打开官方 Claude Desktop，请从开始菜单启动，或先安装官方客户端。",error);
        }
    }
    private static bool IsClaudeInstalled()
    {
        try
        {
            using var key=Registry.CurrentUser.OpenSubKey(@"Software\Classes\claude\shell\open\command",false)
                ??Registry.LocalMachine.OpenSubKey(@"Software\Classes\claude\shell\open\command",false);
            if(key?.GetValue(null) is string command&&command.Length>0)return true;
        }
        catch(UnauthorizedAccessException){}
        catch(SecurityException){}
        try
        {
            string packages=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Packages");
            return Directory.Exists(packages)&&Directory.EnumerateDirectories(packages,"Claude_*").Any();
        }
        catch(IOException){return false;}
        catch(UnauthorizedAccessException){return false;}
    }
    public static string OpenCodex()
    {
        try
        {
            Process.Start(new ProcessStartInfo("codex://") { UseShellExecute = true });
            return "已请求打开 Codex 客户端。";
        }
        catch
        {
            string packages = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Packages");
            if (Directory.Exists(Path.Combine(packages, "OpenAI.Codex_2p2nqsd0c76g0")))
            {
                try
                {
                    Process.Start(new ProcessStartInfo("explorer.exe", @"shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App") { UseShellExecute = true });
                    return "已请求打开 Codex 客户端。";
                }
                catch { }
            }
            throw new InvalidOperationException("未能打开 Codex，请先安装官方客户端，或从开始菜单启动。");
        }
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
