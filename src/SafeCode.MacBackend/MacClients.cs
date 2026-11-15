using SafeCode.Core;
using System.Diagnostics;
namespace SafeCode.Desktop;
internal sealed class MacClients(string directory)
{
    internal static string Quote(string value)=>"'"+value.Replace("'","'\\''")+"'";
    public static async Task<string> Run(string file,IEnumerable<string> args,CancellationToken ct=default)
    {
        var start=new ProcessStartInfo(file){RedirectStandardOutput=true,RedirectStandardError=true,UseShellExecute=false};foreach(var arg in args)start.ArgumentList.Add(arg);
        using var process=Process.Start(start)??throw new IOException("无法启动系统工具。");var output=process.StandardOutput.ReadToEndAsync(ct);var error=process.StandardError.ReadToEndAsync(ct);
        try{await process.WaitForExitAsync(ct);}catch{try{process.Kill(true);}catch{}throw;}
        if(process.ExitCode!=0)throw new IOException("系统工具执行失败："+await error);return await output;
    }
    public async Task OpenTerminalAsync(SshSession session,CancellationToken ct)
    {
        if(session.Server?.Statuses.GetValueOrDefault("account")!=true)throw new InvalidOperationException("请先部署工作用户。");
        await session.DeployAsync("ssh",null,ct:ct);await session.VerifyWorkspaceKeyAsync(ct);
        var account=await session.RequestAsync<AccountInfo>("account_info",null,ct);
        var config=Path.Combine(directory,"terminal-config");File.WriteAllText(config,"# Safe Code isolated SSH client\n");MacFiles.Protect(config);
        string[] args=["/usr/bin/ssh","-t","-F",config,"-i",session.KeyPath,"-p",session.Port.ToString(),"-o","BatchMode=yes","-o","ForwardAgent=no","-o","IdentitiesOnly=yes","-o","StrictHostKeyChecking=yes","-o","UserKnownHostsFile="+session.KnownHostsPath,account.Username+"@"+session.Host,".local/bin/safe-code-terminal"];
        string command=string.Join(" ",args.Select(Quote));
        // AppleScript string encoding is separate from shell quoting. No password in args or scripts.
        string literal="\""+command.Replace("\\","\\\\").Replace("\"","\\\"").Replace("\r","\\r").Replace("\n","\\n")+"\"";
        await Run("/usr/bin/osascript",["-e","tell application \"Terminal\"","-e","activate","-e","do script "+literal,"-e","end tell"],ct);
    }
    public async Task<string> DesktopAsync(SshSession session,DesktopResolution resolution,CancellationToken ct)
    {
        if(session.Server?.DesktopReady!=true)throw new InvalidOperationException("请先安装虚拟桌面。");
        var account=await session.RequestAsync<AccountInfo>("account_info",null,ct);uint port=await session.ForwardAsync(3389,ct);
        string folder=Path.Combine(directory,"Desktops");Directory.CreateDirectory(folder);string path=Path.Combine(folder,"Safe Code-"+port+".rdp");
        File.WriteAllText(path,"full address:s:127.0.0.1:"+port+"\nusername:s:"+account.Username+"\nprompt for credentials:i:1\nscreen mode id:i:1\ndesktopwidth:i:"+resolution.Width+"\ndesktopheight:i:"+resolution.Height+"\nauthentication level:i:2\nredirectclipboard:i:1\n");MacFiles.Protect(path);return path;
    }
}
