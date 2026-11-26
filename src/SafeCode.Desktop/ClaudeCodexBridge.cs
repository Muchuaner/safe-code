using SafeCode.Core;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace SafeCode.Desktop;
internal sealed record ClaudeCodexStatus(bool Running,bool Connected,bool CleanupPending,int Port,string Model,bool Writable,string Message,string Configuration="",DateTimeOffset? StartedAt=null,DateTimeOffset? StoppedAt=null,IntegrationUsage? Usage=null,string UsageSource="")
{
    public long DurationSeconds=>(long)Math.Max(0,((StoppedAt??DateTimeOffset.UtcNow)-(StartedAt??DateTimeOffset.UtcNow)).TotalSeconds);
}
internal sealed class ClaudeCodexBridge : IDisposable
{
    internal const string ExeSha256="c8b2353f14afeef2a04c759d0da64d4aca301127a9e0fcdb82a8032026acd070";
    private readonly string dataDirectory;
    private readonly SemaphoreSlim gate=new(1,1);
    private Process? process;
    private SshSession? session;
    private string profile="",token="",clientId="",keyId="",publicKey="",configuration="",model="sonnet";
    private int port=18797;
    private bool connected,cleanupPending,writable;
    private string message="未啟動";
    private DateTime lastHealth;
    private readonly SemaphoreSlim usageGate=new(1,1);
    private CodexUsageSource? usageSource;
    private IntegrationUsage usage=IntegrationUsage.Unavailable("尚未取樣");
    private string usageSourceName="";
    private DateTimeOffset? startedAt,stoppedAt;
    private DateTime lastUsageSample;
    private string PreferencePath=>Path.Combine(dataDirectory,"CodexBridge","enabled.json");
    public GatewayPreference Preference=>GatewayPreference.Load(PreferencePath);
    public bool Running=>process!=null&&!process.HasExited;
    public ClaudeCodexBridge(string dataDirectory)=>this.dataDirectory=dataDirectory;
    public ClaudeCodexStatus Status=>new(Running,Running&&connected,cleanupPending,port,model,writable,Running?message:cleanupPending?"本機已停止，遠端清理待重試":"未啟動",Running?configuration:"",startedAt,stoppedAt,Running||startedAt!=null?usage:IntegrationUsage.Unavailable("尚未啟用"),usageSourceName);
    private static void ProtectDirectory(string path)
    {
        Directory.CreateDirectory(path);using var current=WindowsIdentity.GetCurrent();var acl=new DirectorySecurity();acl.SetAccessRuleProtection(true,false);acl.SetOwner(current.User!);
        foreach(var sid in new[]{current.User!,new SecurityIdentifier("S-1-5-18")})acl.AddAccessRule(new FileSystemAccessRule(sid,FileSystemRights.FullControl,InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit,PropagationFlags.None,AccessControlType.Allow));new DirectoryInfo(path).SetAccessControl(acl);
    }
    private static void ProtectFile(string path)
    {
        using var current=WindowsIdentity.GetCurrent();var acl=new FileSecurity();acl.SetAccessRuleProtection(true,false);acl.SetOwner(current.User!);
        foreach(var sid in new[]{current.User!,new SecurityIdentifier("S-1-5-18")})acl.AddAccessRule(new FileSystemAccessRule(sid,FileSystemRights.FullControl,AccessControlType.Allow));new FileInfo(path).SetAccessControl(acl);
    }
    private static string Secret(string path,int bytes)
    {if(File.Exists(path)){ProtectFile(path);string saved=File.ReadAllText(path).Trim();if(!Regex.IsMatch(saved,@"\A[A-Za-z0-9_-]{43}\z"))throw new IOException("本機適配器密鑰格式異常，未啟動。");return saved;}string result=Convert.ToBase64String(RandomNumberGenerator.GetBytes(bytes)).TrimEnd('=').Replace('+','-').Replace('/','_');File.WriteAllText(path,result);ProtectFile(path);return result;}
    private static async Task<string> SshClientAsync(string profile,CancellationToken ct)
    {
        string system=Path.Combine(Environment.SystemDirectory,"OpenSSH","ssh.exe");if(File.Exists(system))return system;
        string zip=Path.Combine(AppContext.BaseDirectory,"Payloads","openssh","OpenSSH-Win64.zip");
        if(!File.Exists(zip))throw new IOException("缺少 Windows OpenSSH 客戶端，請使用包含 SSH 組件的完整測試包。");
        using var input=File.OpenRead(zip);if(!Convert.ToHexString(await SHA256.HashDataAsync(input,ct)).Equals(HostBridgeInstaller.PayloadSha256,StringComparison.OrdinalIgnoreCase))throw new IOException("隨包 SSH 客戶端校驗失敗。");
        input.Position=0;string directory=Path.Combine(profile,"ssh-client");ProtectDirectory(directory);using var archive=new ZipArchive(input,ZipArchiveMode.Read);
        foreach(string name in new[]{"ssh.exe","libcrypto.dll","ssh-sk-helper.exe","ssh-pkcs11-helper.exe"})
        {var entry=archive.GetEntry("OpenSSH-Win64/"+name)??throw new IOException("SSH 客戶端組件不完整。");entry.ExtractToFile(Path.Combine(directory,name),true);}
        return Path.Combine(directory,"ssh.exe");
    }
    public async Task<ClaudeCodexStatus> StartAsync(SshSession target,int requestedPort,string requestedModel,bool allowWrite,CancellationToken ct)
    {
        if(requestedPort<1024||requestedPort>65535||!Regex.IsMatch(requestedModel,@"\A(?:claude-[a-z0-9]+(?:-[a-z0-9]+)*|sonnet|opus|haiku|fable)\z"))throw new ArgumentException("請填寫 1024–65535 的連接埠及 Claude 支援的模型標識，如 sonnet、opus 或 haiku。");
        await gate.WaitAsync(ct);
        if(Running){gate.Release();throw new InvalidOperationException("請先停止當前適配器，再修改設定。");}
        try
        {
            string exe=Path.Combine(AppContext.BaseDirectory,"Payloads","codex-claude","codex-claude-win.exe");
            if(!File.Exists(exe))throw new IOException("當前安裝包未包含第三方適配器，請使用完整 Codex 接入測試版。");
            await using(var file=File.OpenRead(exe))if(!Convert.ToHexString(await SHA256.HashDataAsync(file,ct)).Equals(ExeSha256,StringComparison.OrdinalIgnoreCase))throw new IOException("第三方適配器 SHA256 校驗失敗，未啟動。");
            var probe=new TcpListener(IPAddress.Loopback,requestedPort);try{probe.Start();}catch(SocketException){throw new IOException("本機連接埠已被佔用，請更換適配器連接埠。");}finally{probe.Stop();}
            port=requestedPort;model=requestedModel;writable=allowWrite;session=target;
            string identity=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(target.Fingerprint+"@"+target.Host+":"+target.Port)))[..20].ToLowerInvariant();profile=Path.Combine(dataDirectory,"CodexBridge",identity);ProtectDirectory(profile);ProtectDirectory(Path.Combine(profile,"ssh"));
            foreach(string name in new[]{"token","client-id","salt","config.json","state.json","adapter.log","claude-models.json","session.json"}){string file=Path.Combine(profile,name);if(File.Exists(file))ProtectFile(file);}
            var key=SshSession.CreateCodexBridgeKey(Path.Combine(profile,"ssh"));ProtectFile(key.Path);publicKey=key.PublicKey;keyId=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(publicKey.Split(' ')[1])))[..20].ToLowerInvariant();
            string idFile=Path.Combine(profile,"client-id");clientId=File.Exists(idFile)?File.ReadAllText(idFile).Trim():"c_"+Convert.ToHexString(RandomNumberGenerator.GetBytes(12)).ToLowerInvariant();
            if(!Regex.IsMatch(clientId,@"\Ac_[a-f0-9]{24}\z"))throw new IOException("適配器客戶端標識異常，已停止。");File.WriteAllText(idFile,clientId);token=Secret(Path.Combine(profile,"token"),32);
            string host=target.Port==22?target.Host:$"[{target.Host}]:{target.Port}";
            string pin=File.ReadAllLines(target.KnownHostsPath).SingleOrDefault(line=>line.StartsWith(host+" ",StringComparison.Ordinal))??throw new IOException("缺少已核驗的伺服器指紋，未啟動適配器。");
            string known=Path.Combine(profile,"ssh","known_hosts");File.WriteAllText(known,pin+"\n");
            string sshPath=await SshClientAsync(profile,ct);
            startedAt=DateTimeOffset.UtcNow;stoppedAt=null;usage=IntegrationUsage.Unavailable("正在建立本機 Codex 用量基線");usageSourceName="本機 Codex 會話 token_count";
            usageSource=new CodexUsageSource(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),".codex","sessions"),startedAt.Value);
            try{using var baselineTimeout=new CancellationTokenSource(TimeSpan.FromSeconds(6));await Task.Run(()=>usageSource.InitializeAsync(baselineTimeout.Token),baselineTimeout.Token);usage=usageSource.Snapshot();}
            catch(OperationCanceledException){usage=IntegrationUsage.Unavailable("用量基線建立超時，稍後重試");}
            catch(Exception error)when(error is IOException or UnauthorizedAccessException or NotSupportedException){usage=IntegrationUsage.Unavailable("本機 Codex 會話用量暫不可用");}
            lastUsageSample=DateTime.UtcNow;PersistSession();
            var installed=await target.InstallCodexBridgeAsync(publicKey,keyId,clientId,ct);
            if(!installed.Ready)throw new IOException("適配器伺服器組件未就緒。");cleanupPending=true;
            string slug=requestedModel.StartsWith("claude-",StringComparison.Ordinal)?requestedModel:"claude-"+requestedModel;
            var config=new{
                listen=new{host="127.0.0.1",port},ssh=new{target=installed.Username+"@"+target.Host,port=target.Port,identityFile=key.Path,knownHostsFile=known,remoteCommand=installed.ConnectCommand,sshPath,extraOptions=new[]{"ProxyJump=none"}},
                privacy=new{mode="strict",iUnderstandFullModeExposesThisPc=false,useCodexProjectFolder=true,projectFolderWritable=writable,folders=Array.Empty<object>(),userContent="redact",shareCodexSkills=false,shareCodexMemory=false,keepToolImagesInHistory=false},
                vpsNativeTools="web",housekeeping="local-title",models=new[]{new{slug,displayName="Claude "+requestedModel+" (VPS)",claudeModel=requestedModel}},logLevel="error"
            };
            await File.WriteAllTextAsync(Path.Combine(profile,"config.json"),JsonSerializer.Serialize(config,new JsonSerializerOptions{WriteIndented=true}),ct);
            var start=new ProcessStartInfo(exe){UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=profile,RedirectStandardOutput=true,RedirectStandardError=true};start.Environment["CODEX_CLAUDE_WIN_APPDIR"]=profile;start.Environment["NO_COLOR"]="1";start.ArgumentList.Add("run");
            process=Process.Start(start)??throw new IOException("無法啟動第三方適配器。");
            // Never forward vendor stdout/stderr: future versions may print a token.
            _=process.StandardOutput.BaseStream.CopyToAsync(Stream.Null);_=process.StandardError.BaseStream.CopyToAsync(Stream.Null);
            connected=false;message="正在驗證本機 API 與 VPS 通道";
            for(int i=0;i<45;i++)
            {
                ct.ThrowIfCancellationRequested();if(!Running)throw new IOException("第三方適配器已退出，請查看本機私有適配器日誌。");
                if(await CheckHealthAsync(ct))break;await Task.Delay(500,ct);
            }
            if(!connected)throw new IOException("本機適配器已啟動，但未能連接 VPS 組件；請檢查 SSH、Node.js 和 Claude Code。");
            if(!File.Exists(Path.Combine(profile,"claude-models.json")))throw new IOException("適配器未生成 Codex 模型目錄。");
            string catalog=JsonSerializer.Serialize(Path.Combine(profile,"claude-models.json").Replace('\\','/'));
            configuration=$"model_catalog_json = {catalog}\nmodel_provider = \"safe_code_claude\"\nmodel = \"{slug}\"\nmodel_reasoning_effort = \"high\"\nmodel_context_window = 200000\nmodel_auto_compact_token_limit = 160000\n\n[model_providers.safe_code_claude]\nname = \"Claude via Safe Code\"\nbase_url = \"http://127.0.0.1:{port}/v1\"\nwire_api = \"responses\"\nsupports_websockets = false\nrequires_openai_auth = true\nrequest_max_retries = 2\nstream_max_retries = 3\nstream_idle_timeout_ms = 600000\n";
            GatewayPreference.For(target,port,model,writable).Save(PreferencePath);
            message="接口與 SSH 已連線；Claude 登入及模型權限需在使用前確認";return Status;
        }
        catch
        {
            KillLocal();await CleanupRemoteAsync();throw;
        }
        finally{gate.Release();}
    }
    private async Task<bool> CheckHealthAsync(CancellationToken ct)
    {
        try
        {
            using var http=new HttpClient(new HttpClientHandler{UseProxy=false}){Timeout=TimeSpan.FromSeconds(2)};http.DefaultRequestHeaders.Authorization=new AuthenticationHeaderValue("Bearer",token);
            using var result=await http.GetAsync("http://127.0.0.1:"+port+"/health",ct);if(!result.IsSuccessStatusCode)return false;
            using var json=JsonDocument.Parse(await result.Content.ReadAsStringAsync(ct));var root=json.RootElement;
            connected=root.GetProperty("ok").GetBoolean()&&root.GetProperty("privacyMode").GetString()=="strict"&&root.GetProperty("vpsConnected").GetBoolean();lastHealth=DateTime.UtcNow;return connected;
        }
        catch(OperationCanceledException)when(ct.IsCancellationRequested){throw;}
        catch{connected=false;return false;}
    }
    public async Task<ClaudeCodexStatus> TickAsync(CancellationToken ct=default)
    {
        if(Running)await SampleUsageAsync(force:false,ct);
        return Status;
    }
    private async Task SampleUsageAsync(bool force,CancellationToken ct)
    {
        if(usageSource==null||startedAt==null)return;
        if(!force&&DateTime.UtcNow-lastUsageSample<TimeSpan.FromSeconds(8))return;
        bool acquired=force?await usageGate.WaitAsync(TimeSpan.FromSeconds(3),ct):await usageGate.WaitAsync(0,ct);
        if(!acquired)return;
        try
        {
            using var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct);
            if(!force)timeout.CancelAfter(TimeSpan.FromSeconds(4));
            await Task.Run(()=>usageSource.SampleAsync(timeout.Token),timeout.Token);
            usage=usageSource.Snapshot();lastUsageSample=DateTime.UtcNow;PersistSession();
        }
        catch(OperationCanceledException)when(!ct.IsCancellationRequested){usage=IntegrationUsage.Unavailable("用量取樣超時，稍後重試");}
        catch(Exception error)when(error is IOException or UnauthorizedAccessException){usage=IntegrationUsage.Unavailable("本機 Codex 會話用量暫不可用");}
        finally{usageGate.Release();}
    }
    private void PersistSession()
    {
        if(string.IsNullOrEmpty(profile)||startedAt is null||usageSource is null)return;
        try
        {
            string path=Path.Combine(profile,"session.json");
            string temp=path+".new";
            File.WriteAllText(temp,JsonSerializer.Serialize(new CodexBridgeSession(startedAt.Value,stoppedAt,usageSource.Export()),new JsonSerializerOptions{WriteIndented=false}));
            ProtectFile(temp);File.Move(temp,path,true);
        }
        catch(IOException){}
        catch(UnauthorizedAccessException){}
    }
    private sealed record CodexBridgeSession(DateTimeOffset StartedAt,DateTimeOffset? StoppedAt,CodexUsageState Usage);
    public async Task<ClaudeCodexStatus> RefreshAsync(CancellationToken ct)
    {
        if(Running&&DateTime.UtcNow-lastHealth>TimeSpan.FromSeconds(4)&&await gate.WaitAsync(0,ct))
        {try{await CheckHealthAsync(ct);message=connected?"接口與 SSH 已連線；模型調用需使用 VPS Claude 帳戶":"連線暫不可用，適配器正在嘗試恢復";}finally{gate.Release();}}
        return Status;
    }
    private void KillLocal()
    {
        if(process==null)return;
        if(!process.HasExited)
        {
            try{process.Kill(true);}catch(InvalidOperationException)when(process.HasExited){}
            if(!process.WaitForExit(5000))throw new IOException("適配器仍在停止，已保留進程守護，請重試。");
        }
        process.Dispose();process=null;connected=false;configuration="";
        if(startedAt!=null&&stoppedAt==null){stoppedAt=DateTimeOffset.UtcNow;PersistSession();}
    }
    private async Task CleanupRemoteAsync()
    {
        if(session==null||publicKey.Length==0)return;
        try{using var timeout=new CancellationTokenSource(TimeSpan.FromSeconds(18));var result=await session.RemoveCodexBridgeAsync(publicKey,keyId,clientId,timeout.Token);cleanupPending=result.Warning!=null;message=result.Warning??"適配器已停止，專用授權已移除";}
        catch{cleanupPending=true;message="本機入口已停止；遠端清理未確認，恢復連線後可重試停止。";}
    }
    public async Task<ClaudeCodexStatus> StopAsync(CancellationToken ct=default,bool suspend=false)
    {await gate.WaitAsync(ct);try{if(!suspend)(Preference with{Enabled=false}).Save(PreferencePath);try{await SampleUsageAsync(force:true,ct);}catch(OperationCanceledException){}KillLocal();await CleanupRemoteAsync();return Status;}finally{gate.Release();}}
    public string Configuration=>Running?configuration:throw new InvalidOperationException("請先啟動適配器。");
    public string Token=>Running?token:throw new InvalidOperationException("請先啟動適配器。");
    public void Dispose(){if(startedAt!=null&&stoppedAt==null){stoppedAt=DateTimeOffset.UtcNow;PersistSession();}KillLocal();}
}
