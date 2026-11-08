using System.IO;
using SafeCode.Core;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Security.Cryptography;
using System.Net.Sockets;

namespace SafeCode.Desktop;

public sealed class WindowsClients : IDisposable
{
    private readonly List<string> credentialTargets = [];
    private readonly List<string> temporaryFiles = [];
    private readonly List<Process> terminals = [];
    public async Task OpenDesktopAsync(SshSession session, CancellationToken ct)
    {
        if (session.Server?.DesktopReady != true) throw new InvalidOperationException("请先安装虚拟桌面。");
        var account = await session.RequestAsync<AccountInfo>("account_info", null, ct);
        var endpoint=await PrepareRdpAsync(session,account,ct);uint port=endpoint.Port;
        string file = Path.Combine(Path.GetTempPath(), "safe-code-" + Guid.NewGuid().ToString("N") + ".rdp");
        File.WriteAllText(file, $"full address:s:{endpoint.Host}:{port}\r\nusername:s:{account.Username}\r\nprompt for credentials:i:0\r\nenablecredsspsupport:i:0\r\nauthentication level:i:2\r\nredirectclipboard:i:1\r\ndynamic resolution:i:1\r\nscreen mode id:i:1\r\ndesktopwidth:i:1280\r\ndesktopheight:i:800\r\n", Encoding.Unicode);
        temporaryFiles.Add(file);
        var start = new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "mstsc.exe")) { UseShellExecute = false };
        start.ArgumentList.Add(file); Process.Start(start);
    }
    private async Task<(string Host,uint Port,string Target)> PrepareRdpAsync(SshSession session,AccountInfo account,CancellationToken ct)
    {
        for(int attempt=0;attempt<12;attempt++){
            byte[] digest=SHA256.HashData(Encoding.UTF8.GetBytes(session.Host+":"+session.Port+":"+session.Fingerprint+":"+attempt));
            string host="127.77."+digest[0]+"."+(digest[1]%254+1);string target="TERMSRV/"+host;
            if(!credentialTargets.Contains(target)&&CredRead(target,1,0,out var existing)){CredFree(existing);continue;}
            uint port=await session.ForwardAsync(3389,ct,host);WriteCredential(target,account.Username,account.Password);if(!credentialTargets.Contains(target))credentialTargets.Add(target);return(host,port,target);
        }
        throw new IOException("无法分配独立的远程桌面凭据入口，未覆盖已有凭据。");
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
    public async Task<bool> VerifyCredentialStoreAsync(SshSession session,CancellationToken ct)
    {
        var account=await session.RequestAsync<AccountInfo>("account_info",null,ct);
        var endpoint=await PrepareRdpAsync(session,account,ct);string target=endpoint.Target;
        using(var tcp=new TcpClient()){
            await tcp.ConnectAsync(endpoint.Host,(int)endpoint.Port,ct);byte[] packet=[3,0,0,19,14,224,0,0,0,0,0,1,0,8,0,3,0,0,0];await tcp.GetStream().WriteAsync(packet,ct);
            byte[] response=new byte[256];using var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct);timeout.CancelAfter(TimeSpan.FromSeconds(12));await tcp.GetStream().ReadExactlyAsync(response.AsMemory(0,4),timeout.Token);int count=(response[2]<<8)|response[3];if(count<11||count>256)throw new IOException("RDP 响应长度无效。");await tcp.GetStream().ReadExactlyAsync(response.AsMemory(4,count-4),timeout.Token);if(response[0]!=3||response[5]!=208)throw new IOException("RDP 独立回环隧道协商失败。");
        }
        if(!CredRead(target,1,0,out var pointer))throw new IOException("无法读取本次 RDP 凭据。");
        try{var data=Marshal.PtrToStructure<Credential>(pointer);string password=Marshal.PtrToStringUni(data.CredentialBlob,(int)data.CredentialBlobSize/2)??"";return data.UserName==account.Username&&password==account.Password;}
        finally{CredFree(pointer);}
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
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct Credential
    {
        public uint Flags, Type;
        public string TargetName;
        public string? Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist, AttributeCount;
        public IntPtr Attributes;
        public string? TargetAlias;
        public string UserName;
    }
    [DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredWrite(ref Credential credential, uint flags);
    [DllImport("advapi32.dll", EntryPoint = "CredDeleteW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredDelete(string target, uint type, uint flags);
    [DllImport("advapi32.dll",EntryPoint="CredReadW",CharSet=CharSet.Unicode,SetLastError=true)]
    private static extern bool CredRead(string target,uint type,uint flags,out IntPtr pointer);
    [DllImport("advapi32.dll")] private static extern void CredFree(IntPtr pointer);
    private static void WriteCredential(string target, string username, string password)
    {
        var bytes = Encoding.Unicode.GetBytes(password); IntPtr pointer = Marshal.AllocHGlobal(bytes.Length);
        try
        {
            Marshal.Copy(bytes, 0, pointer, bytes.Length);
            var credential = new Credential { Type = 1, TargetName = target, UserName = username, CredentialBlobSize = (uint)bytes.Length, CredentialBlob = pointer, Persist = 1 };
            if (!CredWrite(ref credential, 0)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "无法保存本次会话的桌面凭据。");
        }
        finally { Array.Clear(bytes); for (int i = 0; i < bytes.Length; i++) Marshal.WriteByte(pointer, i, 0); Marshal.FreeHGlobal(pointer); }
    }
    public void Dispose()
    {
        foreach(var terminal in terminals){try{if(!terminal.HasExited)terminal.Kill();terminal.Dispose();}catch{}}
        foreach (string target in credentialTargets) CredDelete(target, 1, 0);
        foreach (string file in temporaryFiles) { try { File.Delete(file); } catch { } }
    }
}
