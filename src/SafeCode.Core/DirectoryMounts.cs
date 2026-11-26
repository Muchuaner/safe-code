using Renci.SshNet;
using Renci.SshNet.Common;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace SafeCode.Core;
public sealed record MountResult(string ClientId,List<string> Paths,List<string>? ManagedPaths=null,string? CloudWarning=null);
public sealed record MountStatus(int Count,string State,string Message,long DurationSeconds,long BytesToServer,long BytesToLocal,long CompletedFiles,int ActiveTransfers,bool AuthenticationRequired=false);
public sealed partial class SshSession
{
    private WebDavServer? fileServer;
    private ForwardedPortRemote? fileForward;
    private SshClient? mountTransport;
    private string mountClientId="";
    private uint mountRemotePort;
    private List<MountMapping> guardedMappings=[];
    private readonly SemaphoreSlim mountLifecycle=new(1,1);
    private readonly CancellationTokenSource mountLifetime=new();
    private Task? mountGuard;
    private long mountedAtTimestamp;
    private long previousToServer,previousToLocal,previousFiles;
    private string mountState="idle",mountMessage="未掛載目錄";
    private bool mountTrustFailed;
    private bool mountAuthenticationFailed;
    public TimeSpan MountGuardInterval {get;init;}=TimeSpan.FromSeconds(5);
    public bool HasMounts => fileServer!=null;
    public MountStatus GetMountStatus()
    {
        var stats=fileServer?.TransferStats??new(0,0,0,0);long since=Interlocked.Read(ref mountedAtTimestamp);
        return new(guardedMappings.Count,mountState,mountMessage,since>0?(long)System.Diagnostics.Stopwatch.GetElapsedTime(since).TotalSeconds:0,Interlocked.Read(ref previousToServer)+stats.BytesToServer,Interlocked.Read(ref previousToLocal)+stats.BytesToLocal,Interlocked.Read(ref previousFiles)+stats.CompletedFiles,stats.ActiveTransfers,mountAuthenticationFailed);
    }
    private static void DisposeMountResource(IDisposable? resource){try{resource?.Dispose();}catch{}}
    private sealed class MountSshClient(Renci.SshNet.ConnectionInfo info):SshClient(info)
    {
        // 轉發清理異常不能阻止關閉 SSH 會話，否則舊監聽會阻塞後續重連。
        protected override void OnDisconnecting(){try{base.OnDisconnecting();}catch{}}
    }
    private SshClient NewMountConnection(ConnectionSettings? settings=null)
    {
        if(connection==null||trustedKey==null)throw new InvalidOperationException("尚未確認伺服器身份，不能建立掛載通道。");
        settings??=connection;
        var info=new PasswordConnectionInfo(settings.Host,settings.Port,settings.Username,settings.Password){Timeout=TimeSpan.FromSeconds(12),RetryAttempts=1};
        PinHostKeyAlgorithms(info);
        var transport=new MountSshClient(info){KeepAliveInterval=TimeSpan.FromSeconds(10)};
        transport.HostKeyReceived+=(_,e)=>{e.CanTrust=CryptographicOperations.FixedTimeEquals(trustedKey,e.HostKey);if(!e.CanTrust)mountTrustFailed=true;};
        return transport;
    }
    public async Task<MountResult> ApplyMountsAsync(List<MountMapping> mappings,CancellationToken ct=default)
    {
        if(Server?.MountReady!=true)throw new InvalidOperationException("請先安裝目錄掛載組件。");
        if(mappings.Count==0){if(HasMounts)await UnmountAsync(ct);SaveMountMappings(mappings);return new("",[]);}
        if(mappings.Count>12)throw new ArgumentException("一次最多掛載 12 組目錄。");
        await mountLifecycle.WaitAsync(ct);
        try
        {
            var key=CreateWorkspaceKey();var clientId=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(key.PublicKey)))[..20].ToLowerInvariant();
            var proposed=new WebDavServer(mappings);var transport=NewMountConnection();ForwardedPortRemote? forward=null;MountResult result;
            try
            {
                await transport.ConnectAsync(ct);
                forward=new ForwardedPortRemote("127.0.0.1",0,"127.0.0.1",(uint)proposed.Port);
                transport.AddForwardedPort(forward);await Task.Run(forward.Start,ct);
                // 驗證和同步成功前保留舊通道，失敗時繼續保護原掛載。
                result=await RequestAsync<MountResult>("mount_apply",new{clientId,port=forward.BoundPort,username=proposed.Username,password=proposed.Password,mappings},ct);
            }
            catch{DisposeMountResource(forward);DisposeMountResource(transport);DisposeMountResource(proposed);throw;}
            AccumulateTransfers();DisposeMountResource(fileForward);DisposeMountResource(mountTransport);DisposeMountResource(fileServer);
            fileForward=forward;mountRemotePort=forward.BoundPort;mountTransport=transport;fileServer=proposed;mountClientId=clientId;guardedMappings=[..mappings];Interlocked.CompareExchange(ref mountedAtTimestamp,System.Diagnostics.Stopwatch.GetTimestamp(),0);
            mountState="connected";mountMessage="掛載守護中";mountGuard??=Task.Run(MountGuardLoopAsync);SaveMountMappings(mappings);return result;
        }
        finally{mountLifecycle.Release();}
    }
    private void AccumulateTransfers()
    {
        var stats=fileServer?.TransferStats;if(stats==null)return;
        Interlocked.Add(ref previousToServer,stats.BytesToServer);Interlocked.Add(ref previousToLocal,stats.BytesToLocal);Interlocked.Add(ref previousFiles,stats.CompletedFiles);
    }
    private async Task MountGuardLoopAsync()
    {
        using var timer=new PeriodicTimer(MountGuardInterval);
        try{while(await timer.WaitForNextTickAsync(mountLifetime.Token))await RecoverMountsAsync(mountLifetime.Token);}
        catch(OperationCanceledException){}catch(ObjectDisposedException){}
    }
    public async Task RecoverMountsAsync(CancellationToken ct=default)
    {
        if(!HasMounts||mountTrustFailed||mountAuthenticationFailed||!await mountLifecycle.WaitAsync(0,ct))return;
        bool channelValidated=false;
        try
        {
            if(fileServer==null)return;
            if(mountTransport?.IsConnected!=true||fileForward?.IsStarted!=true)
            {
                mountState="reconnecting";mountMessage="連線中斷，正在恢復原掛載通道";
                uint port=mountRemotePort;
                var oldForward=fileForward;var oldTransport=mountTransport;fileForward=null;mountTransport=null;
                DisposeMountResource(oldForward);DisposeMountResource(oldTransport);var transport=NewMountConnection();
                try
                {
                    await transport.ConnectAsync(ct);
                    var forward=new ForwardedPortRemote("127.0.0.1",port,"127.0.0.1",(uint)fileServer.Port);
                    transport.AddForwardedPort(forward);await Task.Run(forward.Start,ct);
                    fileForward=forward;mountTransport=transport;
                }
                catch{DisposeMountResource(transport);throw;}
            }
            // 只讀取 mountinfo，不訪問可能等待網絡的 FUSE 目錄。保留活躍掛載及其待上載快取。
            const string probe="import json,pathlib,sys;p=pathlib.Path('/var/lib/safe-code')/('mount-'+sys.argv[1]+'.json');r=json.loads(p.read_text()) if p.exists() else {};mounted={x.split()[4] for x in pathlib.Path('/proc/self/mountinfo').read_text().splitlines()};print('mounted' if len(r.get('paths',[]))==int(sys.argv[2]) and all(x in mounted for x in r['paths']) else 'missing')";
            using var command=mountTransport!.CreateCommand(prefix+"python3 -I -c "+Quote(probe)+" "+Quote(mountClientId)+" "+guardedMappings.Count);command.CommandTimeout=TimeSpan.FromSeconds(10);await command.ExecuteAsync(ct);channelValidated=true;
            if(command.ExitStatus!=0)throw new IOException("掛載健康檢查未完成。");
            await EnsureMountControlAsync(ct);
            if(command.Result.Trim()=="missing")
            {
                mountState="reconnecting";mountMessage="通道已恢復，正在恢復伺服器掛載";
                await RequestAsync<MountResult>("mount_recover",new{clientId=mountClientId,port=mountRemotePort,username=fileServer.Username,password=fileServer.Password,mappings=guardedMappings},ct);
            }
            mountState="connected";mountMessage="掛載守護中";
        }
        catch(OperationCanceledException)when(ct.IsCancellationRequested){throw;}
        catch(Exception error)
        {
            if(!channelValidated){var stale=mountTransport;var port=fileForward;mountTransport=null;fileForward=null;DisposeMountResource(stale);DisposeMountResource(port);}
            mountAuthenticationFailed=error is SshAuthenticationException;
            mountState=mountTrustFailed||mountAuthenticationFailed?"attention":"reconnecting";
            mountMessage=mountTrustFailed?"伺服器身份已變化，請先人工核對；未信任新密鑰":mountAuthenticationFailed?"SSH 登入認證失敗；若已更改伺服器密碼，請點擊“更新登入密碼”。原掛載和快取已保留，自動重試已暫停。":"連線暫不可用，保留掛載和快取並繼續重試";
        }
        finally{mountLifecycle.Release();}
    }
    private async Task EnsureMountControlAsync(CancellationToken ct)
    {
        if(!IsConnected){await RestoreMountControlAsync(ct);return;}
        if(!await operations.WaitAsync(0,ct))return;
        bool restore=false;
        try{using var probe=client!.CreateCommand("true");probe.CommandTimeout=TimeSpan.FromSeconds(8);await probe.ExecuteAsync(ct);if(probe.ExitStatus!=0)throw new IOException("控制連線檢查失敗。");}
        catch(OperationCanceledException)when(ct.IsCancellationRequested){throw;}
        catch{DisposeMountResource(client);client=null;restore=true;}
        finally{operations.Release();}
        if(restore)await RestoreMountControlAsync(ct);
    }
    private async Task RestoreMountControlAsync(CancellationToken ct)
    {
        await operations.WaitAsync(ct);
        try
        {
            if(IsConnected)return;
            DisposeMountResource(client);client=null;var replacement=NewMountConnection();
            try
            {
                await replacement.ConnectAsync(ct);
                using var check=replacement.CreateCommand("cat /etc/os-release");check.CommandTimeout=TimeSpan.FromSeconds(10);await check.ExecuteAsync(ct);
                if(!ParseOsRelease(check.Result).TryGetValue("ID",out var id)||id!="ubuntu")throw new UnsupportedOsException("恢復目標不再是 Ubuntu，已停止掛載恢復。");
                using var create=replacement.CreateCommand("mktemp -d /tmp/safe-code.XXXXXXXX");create.CommandTimeout=TimeSpan.FromSeconds(10);await create.ExecuteAsync(ct);string restoredDirectory=create.Result.Trim();
                if(!restoredDirectory.StartsWith("/tmp/safe-code."))throw new IOException("無法恢復管理通道。");
                using var sftp=MakeSftp();await sftp.ConnectAsync(ct);
                await UploadManagementModulesAsync(sftp,restoredDirectory,ct);
                remoteDirectory=restoredDirectory;client=replacement;
            }
            catch{DisposeMountResource(replacement);throw;}
        }
        finally{operations.Release();}
    }
    private string MountSettingsPath=>Path.Combine(dataDirectory,"mounts-"+Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(Fingerprint)))[..20].ToLowerInvariant()+".json");
    private void SaveMountMappings(List<MountMapping> mappings)=>File.WriteAllText(MountSettingsPath,JsonSerializer.Serialize(mappings,JsonDefaults.Options));
    public List<MountMapping> ReadMountMappings()=>File.Exists(MountSettingsPath)?JsonSerializer.Deserialize<List<MountMapping>>(File.ReadAllText(MountSettingsPath),JsonDefaults.Options)??[]:[];
    public async Task UnmountAsync(CancellationToken ct=default)
    {
        if(HasMounts&&mountTransport?.IsConnected!=true)await RecoverMountsAsync(ct);
        await mountLifecycle.WaitAsync(ct);
        try
        {
            if(!HasMounts)return;
            await EnsureMountControlAsync(ct);
            // 只有遠端確認同步卸載後才關閉檔案橋，失敗時保持守護，防止丟棄尚未上載的內容。
            await RequestAsync<MountResult>("mount_remove",new{clientId=mountClientId},ct);
            AccumulateTransfers();DisposeMountResource(fileForward);fileForward=null;DisposeMountResource(mountTransport);mountTransport=null;DisposeMountResource(fileServer);fileServer=null;
            mountClientId="";guardedMappings=[];Interlocked.Exchange(ref mountedAtTimestamp,0);mountState="idle";mountMessage="未掛載目錄";
        }
        finally{mountLifecycle.Release();}
    }
}
