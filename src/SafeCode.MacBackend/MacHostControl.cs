using SafeCode.Core;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
namespace SafeCode.Desktop;
internal static class MacFiles
{
    public static void Protect(string path){if(!OperatingSystem.IsMacOS())throw new PlatformNotSupportedException();File.SetUnixFileMode(path,UnixFileMode.UserRead|UnixFileMode.UserWrite);}
}
// Uses the user's explicitly enabled macOS Remote Login service. Never enables it or alters sshd.
internal sealed class MacHostControl : IDisposable
{
    private readonly string directory,ssh,auth,owned;
    private SshSession? session;
    private string client="";
    public bool Enabled=>session?.HasHostControl==true;
    public HostControlStatus Status=>session?.HostControlStatus??new(false,"disabled","","","未启用；需先开启系统远程登录");
    public MacHostControl(string directory)
    {
        this.directory=directory;ssh=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),".ssh");auth=Path.Combine(ssh,"authorized_keys");owned=Path.Combine(directory,"host-authorized-line.json");
        RevokeLocal(); // A stale owned key from an interrupted app is never silently reactivated.
    }
    public async Task<HostControlStatus> EnableAsync(SshSession target,string alias,CancellationToken ct)
    {
        if(Enabled)throw new InvalidOperationException("本机控制已启用。");
        using(var probe=new TcpClient())
        {
            using var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct);timeout.CancelAfter(TimeSpan.FromSeconds(2));
            try{await probe.ConnectAsync("127.0.0.1",22,timeout.Token);}catch{throw new InvalidOperationException("请先在 macOS 系统设置 → 通用 → 共享中开启“远程登录”，并仅允许当前账户。Safe Code 不会自行开启系统 SSH 服务。");}
        }
        var hostKey=File.ReadAllText("/etc/ssh/ssh_host_ed25519_key.pub").Trim();
        string uid=(await MacClients.Run("/usr/bin/id",["-u"],ct)).Trim();
        if(!uint.TryParse(uid,out uint userId)||userId==0)throw new InvalidOperationException("不支持以 root 账户开放本机控制。");
        client=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(directory+target.Host+target.Port+target.Fingerprint)))[..20].ToLowerInvariant();
        session=target;
        try
        {
            var identity=await target.PrepareHostControlAsync(client,ct);Authorize(identity.PublicKey);
            return await target.StartHostControlAsync(client,22,Environment.UserName,"macos:"+uid,hostKey,alias,ct);
        }
        catch
        {
            RevokeLocal();try{using var cleanup=new CancellationTokenSource(TimeSpan.FromSeconds(12));await target.StopHostControlAsync(cleanup.Token);await target.RequestAsync<JsonElement>("host_bridge",new{clientId=client,action="remove"},cleanup.Token);}catch{}
            session=null;throw;
        }
    }
    private void ValidatePaths()
    {
        if(new DirectoryInfo(ssh).LinkTarget!=null||new FileInfo(auth).LinkTarget!=null)throw new InvalidOperationException("SSH 授权路径包含链接，请使用普通目录和文件后重试。");
        if(File.Exists(ssh)||Directory.Exists(auth))throw new InvalidOperationException("SSH 授权路径类型错误。");
    }
    private void Authorize(string publicKey)
    {
        ValidatePaths();Directory.CreateDirectory(ssh);
        if(!Directory.Exists(ssh))throw new IOException("无法创建 SSH 目录。");
        string[] parts=publicKey.Split(' ',StringSplitOptions.RemoveEmptyEntries);
        if(parts.Length<2||parts[0]!="ssh-ed25519"||parts[1].Any(c=>!(char.IsAsciiLetterOrDigit(c)||c is '+' or '/' or '=')))throw new InvalidOperationException("服务器提供的专用公钥无效。");
        var lines=File.Exists(auth)?File.ReadAllLines(auth).ToList():new List<string>();
        if(lines.Any(line=>line.Split(' ').Contains(parts[1])))throw new InvalidOperationException("此公钥已有其他授权，未重复或放宽权限。");
        // Restrict forwarding, agent, X11 and user rc; explicitly allow an interactive terminal.
        string entry="restrict,pty "+parts[0]+" "+parts[1]+" safe-code-mac-"+client;
        File.WriteAllText(owned,JsonSerializer.Serialize(entry));MacFiles.Protect(owned);
        lines.Add(entry);WriteAuth(lines);
    }
    private void WriteAuth(IEnumerable<string> lines)
    {
        string temp=Path.Combine(ssh,".safe-code-auth-"+Guid.NewGuid().ToString("N"));
        try{using(var stream=new FileStream(temp,FileMode.CreateNew,FileAccess.Write,FileShare.None,4096,FileOptions.None)){MacFiles.Protect(temp);using var writer=new StreamWriter(stream);foreach(var line in lines)writer.WriteLine(line);}File.Move(temp,auth,true);}finally{File.Delete(temp);}
    }
    private void RevokeLocal()
    {
        if(!File.Exists(owned))return;ValidatePaths();string? entry=JsonSerializer.Deserialize<string>(File.ReadAllText(owned));
        if(entry==null||!entry.StartsWith("restrict,pty ssh-ed25519 ")||!entry.Contains(" safe-code-mac-"))throw new IOException("本机控制撤权记录无效，请检查授权文件。");
        if(File.Exists(auth))WriteAuth(File.ReadAllLines(auth).Where(line=>line!=entry));File.Delete(owned);
    }
    public async Task<HostControlStatus> DisableAsync(CancellationToken ct)
    {
        RevokeLocal();if(session!=null)await session.StopHostControlAsync(ct);var result=Status;session=null;return result;
    }
    public void Dispose(){RevokeLocal();}
}
