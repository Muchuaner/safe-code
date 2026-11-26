using SafeCode.Core;
using System.Diagnostics;
namespace SafeCode.Desktop;
internal sealed class MacClients(string directory)
{
    internal static string Quote(string value)=>"'"+value.Replace("'","'\\''")+"'";
    public static async Task<string> Run(string file,IEnumerable<string> args,CancellationToken ct=default)
    {
        var start=new ProcessStartInfo(file){RedirectStandardOutput=true,RedirectStandardError=true,UseShellExecute=false};foreach(var arg in args)start.ArgumentList.Add(arg);
        using var process=Process.Start(start)??throw new IOException("無法啟動系統工具。");var output=process.StandardOutput.ReadToEndAsync(ct);var error=process.StandardError.ReadToEndAsync(ct);
        try{await process.WaitForExitAsync(ct);}catch{try{process.Kill(true);}catch{}throw;}
        if(process.ExitCode!=0)throw new IOException("系統工具執行失敗："+await error);return await output;
    }
    public static async Task<string> OpenLocalClientAsync(string name,CancellationToken ct)
    {
        if(name != "Codex")throw new ArgumentException("未知客戶端。");
        try{await Run("/usr/bin/open",["-a",name],ct);return "已請求開啟 "+name+" 客戶端。";}
        catch(IOException){throw new InvalidOperationException("未能開啟 "+name+"，請先安裝官方客戶端，或從應用程式目錄啟動。");}
    }
    public async Task OpenTerminalAsync(SshSession session,CancellationToken ct)
    {
        if(session.Server?.Statuses.GetValueOrDefault("account")!=true)throw new InvalidOperationException("請先部署工作用戶。");
        await session.DeployAsync("ssh",null,ct:ct);await session.VerifyWorkspaceKeyAsync(ct);
        var account=await session.RequestAsync<AccountInfo>("account_info",null,ct);
        var config=Path.Combine(directory,"terminal-config");File.WriteAllText(config,"# Safe Code isolated SSH client\n");MacFiles.Protect(config);
        string[] args=["/usr/bin/ssh","-t","-F",config,"-i",session.KeyPath,"-p",session.Port.ToString(),"-o","BatchMode=yes","-o","ForwardAgent=no","-o","IdentitiesOnly=yes","-o","StrictHostKeyChecking=yes","-o",SshClientOptions.UserKnownHostsFile(session.KnownHostsPath),account.Username+"@"+session.Host,".local/bin/safe-code-terminal"];
        string command=string.Join(" ",args.Select(Quote));
        // AppleScript string encoding is separate from shell quoting. No password in args or scripts.
        string literal="\""+command.Replace("\\","\\\\").Replace("\"","\\\"").Replace("\r","\\r").Replace("\n","\\n")+"\"";
        await Run("/usr/bin/osascript",["-e","tell application \"Terminal\"","-e","activate","-e","do script "+literal,"-e","end tell"],ct);
    }
    public async Task<string?> DesktopAsync(SshSession session,DesktopResolution resolution,
        RdpResizePreferences display,CancellationToken ct)
    {
        if(session.Server?.DesktopReady!=true)throw new InvalidOperationException("請先安裝虛擬桌面。");
        var account=await session.RequestAsync<AccountInfo>("account_info",null,ct);
        string? freeRdp=FindFreeRdp();
        var endpoint=await session.PrepareRemoteDesktopAsync(account.Username,freeRdp!=null,ct);
        if(freeRdp!=null)
        {
            await OpenFreeRdpAsync(freeRdp,endpoint,account,resolution,display.Read(),display.ReadFullscreen(),ct);
            return null;
        }

        string folder=Path.Combine(directory,"Desktops");
        Directory.CreateDirectory(folder);
        string path=Path.Combine(folder,"Safe Code-"+endpoint.ProfileId+".rdp");
        string temp=path+"."+Guid.NewGuid().ToString("N")+".tmp";
        try
        {
            var options=new FileStreamOptions{Mode=FileMode.CreateNew,Access=FileAccess.Write,Share=FileShare.None};
            if(!OperatingSystem.IsWindows()) options.UnixCreateMode=UnixFileMode.UserRead|UnixFileMode.UserWrite;
            using(var writer=new StreamWriter(temp,options))
            {
                writer.WriteLine("full address:s:127.0.0.1:"+endpoint.Port);
                writer.WriteLine("username:s:"+account.Username);
                writer.WriteLine("prompt for credentials:i:1");
                writer.WriteLine("screen mode id:i:"+(display.ReadFullscreen()?2:1));
                var mode=display.Read();
                writer.WriteLine("dynamic resolution:i:"+(mode==RdpResizeMode.Dynamic?1:0));
                writer.WriteLine("smart sizing:i:"+(mode==RdpResizeMode.Scale?1:0));
                writer.WriteLine("desktopwidth:i:"+resolution.Width);
                writer.WriteLine("desktopheight:i:"+resolution.Height);
                writer.WriteLine("authentication level:i:2");
                writer.WriteLine("redirectclipboard:i:1");
                writer.WriteLine("bandwidthautodetect:i:1");
                writer.WriteLine("networkautodetect:i:1");
            }
            File.Move(temp,path,true);
            return path;
        }
        finally{File.Delete(temp);}
    }

    private static string? FindFreeRdp()
    {
        string bundled=Path.GetFullPath(Path.Combine(AppContext.BaseDirectory,"..","RDP","sdl-freerdp"));
        return new[]{bundled,"/opt/homebrew/bin/sdl-freerdp","/usr/local/bin/sdl-freerdp"}.FirstOrDefault(File.Exists);
    }

    internal static string[] FreeRdpArguments(RemoteDesktopConnection endpoint, AccountInfo account,
        DesktopResolution resolution, RdpResizeMode mode, bool fullscreen)
    {
        if (endpoint.CertificateFingerprint is not { Length: 64 })
            throw new InvalidOperationException("缺少經 SSH 獲取的 RDP 證書指紋。");
        if (account.Username.IndexOfAny(['\r','\n','\0'])>=0 || account.Password.IndexOfAny(['\r','\n','\0'])>=0)
            throw new InvalidOperationException("遠端桌面憑據包含不支援的換行或空字符。");
        var arguments=new List<string> {
            $"/v:127.0.0.1:{endpoint.Port}", "/u:"+account.Username, "/p:"+account.Password,
            $"/size:{resolution.Width}x{resolution.Height}", "/sec:tls",
            "/cert:deny,fingerprint:sha256:"+endpoint.CertificateFingerprint,
            "+clipboard", "/log-level:OFF"
        };
        if (fullscreen) arguments.Add("/f");
        if (mode==RdpResizeMode.Scale) arguments.Add("/smart-sizing");
        if (mode==RdpResizeMode.Dynamic) arguments.Add("/dynamic-resolution");
        return arguments.ToArray();
    }

    private static async Task OpenFreeRdpAsync(string path, RemoteDesktopConnection endpoint, AccountInfo account,
        DesktopResolution resolution, RdpResizeMode mode, bool fullscreen, CancellationToken ct)
    {
        string[] arguments=FreeRdpArguments(endpoint,account,resolution,mode,fullscreen);
        var start=new ProcessStartInfo(path) {
            UseShellExecute=false,RedirectStandardInput=true,RedirectStandardOutput=true,RedirectStandardError=true
        };
        start.ArgumentList.Add("/args-from:stdin");
        using var process=Process.Start(start)??throw new IOException("無法啟動 FreeRDP。");
        process.OutputDataReceived+=(_,_)=>{};
        process.ErrorDataReceived+=(_,_)=>{};
        process.BeginOutputReadLine();process.BeginErrorReadLine();
        try
        {
            foreach(string argument in arguments)
                await process.StandardInput.WriteLineAsync(argument.AsMemory(),ct);
            process.StandardInput.Close();
            await Task.Delay(350,ct);
            if(process.HasExited)throw new IOException("FreeRDP 未能開啟桌面。請確認已安裝支援 SDL 的 FreeRDP 3，並檢查 xrdp 的 TLS 和登入設定。");
        }
        catch
        {
            if (!process.HasExited) process.Kill();
            throw;
        }
    }
}
