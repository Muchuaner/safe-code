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
    public HostControlStatus Status=>session?.HostControlStatus??new(false,"disabled","","","未啟用；需先開啟系統遠端登入");
    public MacHostControl(string directory)
    {
        this.directory=directory;ssh=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),".ssh");auth=Path.Combine(ssh,"authorized_keys");owned=Path.Combine(directory,"host-authorized-line.json");
        preferencePath=Path.Combine(directory,"host-control-resume.aes");
        RevokeLocal();
    }
    public async Task<HostControlStatus> EnableAsync(SshSession target,string alias,CancellationToken ct)
    {
        if(Enabled)throw new InvalidOperationException("本機控制已啟用。");
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
                return status with{Message="本機控制已連線。未能儲存重新啟動後的恢復設定。"};
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
        if(!preference.Server.Matches(target))return "伺服器身份與儲存的授權不一致，請重新啟用本機遠端控制。";
        if(target.Server?.Username!=preference.WorkspaceUser)return "伺服器工作用戶已變化，請重新啟用本機遠端控制。";
        try
        {
            await EnableCoreAsync(target,preference.Alias,preference.Client,ct);
            return null;
        }
        catch(OperationCanceledException)when(ct.IsCancellationRequested){throw;}
        catch(Exception error){return "本機遠端控制恢復失敗："+error.Message;}
    }
    private async Task<HostControlStatus> EnableCoreAsync(SshSession target,string alias,string id,CancellationToken ct)
    {
        if(Enabled)throw new InvalidOperationException("本機控制已啟用。");
        using(var probe=new TcpClient())
        {
            using var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct);timeout.CancelAfter(TimeSpan.FromSeconds(2));
            try{await probe.ConnectAsync("127.0.0.1",22,timeout.Token);}catch{throw new InvalidOperationException("請先在 macOS 系統設定 → 通用 → 共享中開啟“遠端登入”，並僅允許當前帳戶。Safe Code 不會自行開啟系統 SSH 服務。");}
        }
        var hostKey=File.ReadAllText("/etc/ssh/ssh_host_ed25519_key.pub").Trim();
        string uid=(await MacClients.Run("/usr/bin/id",["-u"],ct)).Trim();
        if(!uint.TryParse(uid,out uint userId)||userId==0)throw new InvalidOperationException("不支援以 root 帳戶開放本機控制。");
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
        if(Enabled)throw new InvalidOperationException("本機控制已啟用，請先停用。");
        using(var probe=new TcpClient())
        {
            using var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct);timeout.CancelAfter(TimeSpan.FromSeconds(2));
            try{await probe.ConnectAsync("127.0.0.1",22,timeout.Token);}catch{throw new InvalidOperationException("請先在 macOS 系統設定 → 通用 → 共享中開啟“遠端登入”，並僅允許當前帳戶。Safe Code 不會自行開啟系統 SSH 服務。");}
        }
        var hostKey=File.ReadAllText("/etc/ssh/ssh_host_ed25519_key.pub").Trim();
        string uid=(await MacClients.Run("/usr/bin/id",["-u"],ct)).Trim();
        if(!uint.TryParse(uid,out uint userId)||userId==0)throw new InvalidOperationException("不支援以 root 帳戶開放本機控制。");
        client="device"+Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(publicKey)))[..14].ToLowerInvariant();
        session=null;deviceActive=true;
        try{Authorize(publicKey);return new(22,Environment.UserName,"macos:"+userId,hostKey);}
        catch{RevokeLocal();deviceActive=false;throw;}
    }
    private void ValidatePaths()
    {
        if(new DirectoryInfo(ssh).LinkTarget!=null||new FileInfo(auth).LinkTarget!=null)throw new InvalidOperationException("SSH 授權路徑包含連結，請使用普通目錄和檔案後重試。");
        if(File.Exists(ssh)||Directory.Exists(auth))throw new InvalidOperationException("SSH 授權路徑類型錯誤。");
    }
    private void Authorize(string publicKey)
    {
        ValidatePaths();Directory.CreateDirectory(ssh);
        if(!Directory.Exists(ssh))throw new IOException("無法建立 SSH 目錄。");
        string[] parts=publicKey.Split(' ',StringSplitOptions.RemoveEmptyEntries);
        if(parts.Length<2||parts[0]!="ssh-ed25519"||parts[1].Any(c=>!(char.IsAsciiLetterOrDigit(c)||c is '+' or '/' or '=')))throw new InvalidOperationException("伺服器提供的專用公鑰無效。");
        var lines=File.Exists(auth)?File.ReadAllLines(auth).ToList():new List<string>();
        if(lines.Any(line=>line.Split(' ').Contains(parts[1])))throw new InvalidOperationException("此公鑰已有其他授權，未重複或放寬權限。");
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
        if(entry==null||!entry.StartsWith("restrict,pty ssh-ed25519 ")||!entry.Contains(" safe-code-mac-"))throw new IOException("本機控制撤權記錄無效，請檢查授權檔案。");
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
