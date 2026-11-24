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
    private sealed record ResumePreference(GatewayPreference Server, string WorkspaceUser, string Alias, string Client);
    private readonly string directory,ssh,auth,owned,preferencePath;
    private volatile SshSession? session;
    private string client="";
    private volatile bool deviceActive;
    public bool Enabled=>deviceActive||session?.HasHostControl==true;
    public string DefaultAlias=>"mac";
    public bool IsEnabledFor(SshSession target)=>!deviceActive&&Enabled&&ReferenceEquals(session,target);
    public HostControlStatus Status=>session?.HostControlStatus??new(false,"disabled","","","未启用；需先开启系统远程登录");
    public MacHostControl(string directory)
    {
        this.directory=directory;ssh=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),".ssh");auth=Path.Combine(ssh,"authorized_keys");owned=Path.Combine(directory,"host-authorized-line.json");
        preferencePath=Path.Combine(directory,"host-control-resume.aes");
        RevokeLocal();
    }
    public async Task<HostControlStatus> EnableAsync(SshSession target,string alias,CancellationToken ct)
    {
        if(Enabled)throw new InvalidOperationException("本机控制已启用。");
        string id=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(directory+target.Host+target.Port+target.Fingerprint)))[..20].ToLowerInvariant();
        // A failed explicit enable must not leave consent for an older target.
        File.Delete(preferencePath);
        var status=await EnableCoreAsync(target,alias,id,ct);
        if(LocalSecrets.IsUnlocked&&target.Server?.Username is {Length:>0} workspaceUser)
        {
            try
            {
                var preference=new ResumePreference(GatewayPreference.For(target),workspaceUser,alias,id);
                DesktopKeyAccess.WriteProfileFile(preferencePath,LocalSecrets.Protect(JsonSerializer.SerializeToUtf8Bytes(preference)));
            }
            catch(Exception error)when(error is IOException or UnauthorizedAccessException or CryptographicException)
            {
                return status with{Message="本机控制已连接。未能保存重启后的恢复设置。"};
            }
        }
        return status;
    }
    private ResumePreference? ReadPreference()
    {
        if(!LocalSecrets.IsUnlocked||!File.Exists(preferencePath))return null;
        try
        {
            byte[] data=LocalSecrets.Unprotect(File.ReadAllBytes(preferencePath));
            try
            {
                var value=JsonSerializer.Deserialize<ResumePreference>(data);
                return value?.Server is {Fingerprint.Length:>0} ? value : null;
            }
            finally{CryptographicOperations.ZeroMemory(data);}
        }
        catch(Exception error)when(error is IOException or UnauthorizedAccessException or CryptographicException or JsonException)
        {return null;}
    }
    public bool CanResume(ConnectionSettings settings)=>ReadPreference()?.Server.Matches(settings)==true;
    public async Task<string?> ResumeAsync(SshSession target,CancellationToken ct)
    {
        var preference=ReadPreference();
        if(Enabled||preference==null)return null;
        if(!preference.Server.Matches(target))return "服务器身份与保存的授权不一致，请重新启用本机远程控制。";
        if(target.Server?.Username!=preference.WorkspaceUser)return "服务器工作用户已变化，请重新启用本机远程控制。";
        try
        {
            await EnableCoreAsync(target,preference.Alias,preference.Client,ct);
            return null;
        }
        catch(OperationCanceledException)when(ct.IsCancellationRequested){throw;}
        catch(Exception error){return "本机远程控制恢复失败："+error.Message;}
    }
    private async Task<HostControlStatus> EnableCoreAsync(SshSession target,string alias,string id,CancellationToken ct)
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
        client=id;
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
    internal sealed record LocalSsh(int Port,string Username,string Sid,string HostKey);
    internal async Task<LocalSsh> EnableDeviceAsync(string publicKey,CancellationToken ct)
    {
        if(Enabled)throw new InvalidOperationException("本机控制已启用，请先停用。");
        using(var probe=new TcpClient())
        {
            using var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct);timeout.CancelAfter(TimeSpan.FromSeconds(2));
            try{await probe.ConnectAsync("127.0.0.1",22,timeout.Token);}catch{throw new InvalidOperationException("请先在 macOS 系统设置 → 通用 → 共享中开启“远程登录”，并仅允许当前账户。Safe Code 不会自行开启系统 SSH 服务。");}
        }
        var hostKey=File.ReadAllText("/etc/ssh/ssh_host_ed25519_key.pub").Trim();
        string uid=(await MacClients.Run("/usr/bin/id",["-u"],ct)).Trim();
        if(!uint.TryParse(uid,out uint userId)||userId==0)throw new InvalidOperationException("不支持以 root 账户开放本机控制。");
        client="device"+Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(publicKey)))[..14].ToLowerInvariant();
        session=null;deviceActive=true;
        try{Authorize(publicKey);return new(22,Environment.UserName,"macos:"+userId,hostKey);}
        catch{RevokeLocal();deviceActive=false;throw;}
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
        File.Delete(preferencePath);
        return await SuspendAsync(ct);
    }
    public async Task<HostControlStatus> SuspendAsync(CancellationToken ct)
    {
        RevokeLocal();if(session!=null)await session.StopHostControlAsync(ct);var result=Status;session=null;deviceActive=false;return result;
    }
    public void Dispose(){RevokeLocal();}
}
