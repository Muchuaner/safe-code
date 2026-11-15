using SafeCode.Core;
using System.Diagnostics;
namespace SafeCode.Desktop;
internal sealed class MacClients(string directory)
{
    // Stable loopback port for the desktop tunnel so the RDP client keeps one saved connection.
    private const uint DesktopPort=13389;
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
        var account=await session.RequestAsync<AccountInfo>("account_info",null,ct);
        // Preferred port first, ephemeral only if something still holds it: a random port made every
        // session a new "PC" in the client and forced the user to retype the password each time.
        uint port=await session.ForwardAsync(3389,ct,preferredPort:DesktopPort);
        if(!await DesktopRespondsAsync(port,ct))throw new InvalidOperationException("隧道已建立，但服务器上的虚拟桌面没有应答。请确认服务器 xrdp 正在运行，或重新执行“安装虚拟桌面”。");
        string folder=Path.Combine(directory,"Desktops");Directory.CreateDirectory(folder);
        // Earlier builds named the file after their random port, one file per session; drop them so
        // the client list does not keep growing. The current file is rewritten in place.
        foreach(var stale in Directory.EnumerateFiles(folder,"Safe Code-*.rdp")){try{File.Delete(stale);}catch{}}
        string path=Path.Combine(folder,"Safe Code.rdp");
        string[] lines=[
            "full address:s:127.0.0.1:"+port,
            "username:s:"+account.Username,
            // xrdp 的密码校验发生在服务端（PAM），.rdp 无法携带密码（password 51:b 是 Windows
            // DPAPI 块，macOS 客户端不认）。服务端已配置 autorun + 固定凭据自动登录，因此这里
            // 不要再让客户端弹自己的凭据框，否则用户会被问两次而只有一次是必要的。
            "prompt for credentials:i:0",
            "screen mode id:i:1",
            "desktopwidth:i:"+resolution.Width,
            "desktopheight:i:"+resolution.Height,
            // xrdp always presents a self-signed certificate and the traffic never leaves the
            // loopback interface, so the identity prompt only teaches the user to click through it.
            "authentication level:i:0",
            // 不要开 dynamic resolution：sesman 按 宽/高/bpp 匹配已有会话，尺寸一变就被当成新会话，
            // 旧的不会被回收，2 GB 的 VPS 上几次点击就会把内存吃完。全屏模式下客户端本来就用
            // 屏幕尺寸，保持固定反而能稳定复用同一个会话（"reconnected session"）。
            "redirectclipboard:i:1",
            // Without these two the client keeps the bitrate it picked at connect time, so a
            // briefly slow link shows as a frozen desktop instead of a softer one.
            "bandwidthautodetect:i:1",
            "networkautodetect:i:1",
            "audiomode:i:2",
            "redirectprinters:i:0"
        ];
        File.WriteAllText(path,string.Join("\n",lines)+"\n");MacFiles.Protect(path);return path;
    }
    // A local listener only proves the tunnel exists, not that xrdp answers behind it. Send an X.224
    // connection request through the tunnel and require a reply before handing the address over.
    private static async Task<bool> DesktopRespondsAsync(uint port,CancellationToken ct)
    {
        try
        {
            using var tcp=new System.Net.Sockets.TcpClient();
            using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(5));
            await tcp.ConnectAsync(System.Net.IPAddress.Loopback,(int)port,deadline.Token);
            byte[] request=[0x03,0x00,0x00,0x13,0x0e,0xe0,0x00,0x00,0x00,0x00,0x00,0x01,0x00,0x08,0x00,0x03,0x00,0x00,0x00];
            var stream=tcp.GetStream();await stream.WriteAsync(request,deadline.Token);
            var reply=new byte[16];return await stream.ReadAsync(reply,deadline.Token) > 0;
        }
        catch{return false;}
    }
}
