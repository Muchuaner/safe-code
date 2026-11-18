using SafeCode.Core;
using System.Diagnostics;
using System.Buffers.Binary;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
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
        var account=await session.RequestAsync<AccountInfo>("account_info",null,ct);
        byte[] identity=SHA256.HashData(Encoding.UTF8.GetBytes(
            $"{session.Host.ToLowerInvariant()}:{session.Port}|{session.Fingerprint}|{account.Username}"));
        uint preferredPort=20000u+BinaryPrimitives.ReadUInt32BigEndian(identity)%20000u;
        uint port=session.ForwardRemoteDesktop(preferredPort,ct);
        if(!await DesktopRespondsAsync(port,ct))
            throw new IOException("SSH 隧道已建立，但虚拟桌面没有返回 RDP 握手。请检查服务器上的 xrdp 服务。");

        string folder=Path.Combine(directory,"Desktops");
        Directory.CreateDirectory(folder);
        string path=Path.Combine(folder,"Safe Code-"+Convert.ToHexString(identity.AsSpan(0,10)).ToLowerInvariant()+".rdp");
        string temp=path+"."+Guid.NewGuid().ToString("N")+".tmp";
        try
        {
            var options=new FileStreamOptions{Mode=FileMode.CreateNew,Access=FileAccess.Write,Share=FileShare.None};
            if(!OperatingSystem.IsWindows()) options.UnixCreateMode=UnixFileMode.UserRead|UnixFileMode.UserWrite;
            using(var writer=new StreamWriter(temp,options))
            {
                writer.WriteLine("full address:s:127.0.0.1:"+port);
                writer.WriteLine("username:s:"+account.Username);
                writer.WriteLine("prompt for credentials:i:1");
                writer.WriteLine("screen mode id:i:1");
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

    internal static async Task<bool> DesktopRespondsAsync(uint port,CancellationToken ct)
    {
        using var tcp=new TcpClient();
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(TimeSpan.FromSeconds(5));
        try
        {
            await tcp.ConnectAsync(IPAddress.Loopback,(int)port,deadline.Token);
            // Standard X.224 connection request with RDP negotiation, no credentials.
            byte[] request=[0x03,0x00,0x00,0x13,0x0e,0xe0,0,0,0,0,0,0x01,0,0x08,0,0x03,0,0,0];
            var stream=tcp.GetStream();
            await stream.WriteAsync(request,deadline.Token);
            var confirmation=new byte[11];
            await stream.ReadExactlyAsync(confirmation,deadline.Token);
            return confirmation[0]==3 && confirmation[1]==0 &&
                BinaryPrimitives.ReadUInt16BigEndian(confirmation.AsSpan(2))>=11 &&
                confirmation[4]>=6 && confirmation[5]==0xd0;
        }
        catch(OperationCanceledException) when(!ct.IsCancellationRequested){return false;}
        catch(SocketException){return false;}
        catch(IOException){return false;}
    }
}
