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
    private string mountState="idle",mountMessage="未挂载目录";
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
        // 转发清理异常不能阻止关闭 SSH 会话，否则旧监听会阻塞后续重连。
        protected override void OnDisconnecting(){try{base.OnDisconnecting();}catch{}}
    }
    private SshClient NewMountConnection(ConnectionSettings? settings=null)
    {
        if(connection==null||trustedKey==null)throw new InvalidOperationException("尚未确认服务器身份，不能建立挂载通道。");
        settings??=connection;
        var info=new PasswordConnectionInfo(settings.Host,settings.Port,settings.Username,settings.Password){Timeout=TimeSpan.FromSeconds(12),RetryAttempts=1};
        PinHostKeyAlgorithms(info);
        var transport=new MountSshClient(info){KeepAliveInterval=TimeSpan.FromSeconds(10)};
        transport.HostKeyReceived+=(_,e)=>{e.CanTrust=CryptographicOperations.FixedTimeEquals(trustedKey,e.HostKey);if(!e.CanTrust)mountTrustFailed=true;};
        return transport;
    }
    public async Task<MountResult> ApplyMountsAsync(List<MountMapping> mappings,CancellationToken ct=default)
    {
        if(Server?.MountReady!=true)throw new InvalidOperationException("请先安装目录挂载组件。");
        if(mappings.Count==0){if(HasMounts)await UnmountAsync(ct);SaveMountMappings(mappings);return new("",[]);}
        if(mappings.Count>12)throw new ArgumentException("一次最多挂载 12 组目录。");
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
                // 验证和同步成功前保留旧通道，失败时继续保护原挂载。
                result=await RequestAsync<MountResult>("mount_apply",new{clientId,port=forward.BoundPort,username=proposed.Username,password=proposed.Password,mappings},ct);
            }
            catch{DisposeMountResource(forward);DisposeMountResource(transport);DisposeMountResource(proposed);throw;}
            AccumulateTransfers();DisposeMountResource(fileForward);DisposeMountResource(mountTransport);DisposeMountResource(fileServer);
            fileForward=forward;mountRemotePort=forward.BoundPort;mountTransport=transport;fileServer=proposed;mountClientId=clientId;guardedMappings=[..mappings];Interlocked.CompareExchange(ref mountedAtTimestamp,System.Diagnostics.Stopwatch.GetTimestamp(),0);
            mountState="connected";mountMessage="挂载守护中";mountGuard??=Task.Run(MountGuardLoopAsync);SaveMountMappings(mappings);return result;
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
                mountState="reconnecting";mountMessage="连接中断，正在恢复原挂载通道";
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
            // 只读取 mountinfo，不访问可能等待网络的 FUSE 目录。保留活跃挂载及其待上传缓存。
            const string probe="import json,pathlib,sys;p=pathlib.Path('/var/lib/safe-code')/('mount-'+sys.argv[1]+'.json');r=json.loads(p.read_text()) if p.exists() else {};mounted={x.split()[4] for x in pathlib.Path('/proc/self/mountinfo').read_text().splitlines()};print('mounted' if len(r.get('paths',[]))==int(sys.argv[2]) and all(x in mounted for x in r['paths']) else 'missing')";
            using var command=mountTransport!.CreateCommand(prefix+"python3 -I -c "+Quote(probe)+" "+Quote(mountClientId)+" "+guardedMappings.Count);command.CommandTimeout=TimeSpan.FromSeconds(10);await command.ExecuteAsync(ct);channelValidated=true;
            if(command.ExitStatus!=0)throw new IOException("挂载健康检查未完成。");
            await EnsureMountControlAsync(ct);
            if(command.Result.Trim()=="missing")
            {
                mountState="reconnecting";mountMessage="通道已恢复，正在恢复服务器挂载";
                await RequestAsync<MountResult>("mount_recover",new{clientId=mountClientId,port=mountRemotePort,username=fileServer.Username,password=fileServer.Password,mappings=guardedMappings},ct);
            }
            mountState="connected";mountMessage="挂载守护中";
        }
        catch(OperationCanceledException)when(ct.IsCancellationRequested){throw;}
        catch(Exception error)
        {
            if(!channelValidated){var stale=mountTransport;var port=fileForward;mountTransport=null;fileForward=null;DisposeMountResource(stale);DisposeMountResource(port);}
            mountAuthenticationFailed=error is SshAuthenticationException;
            mountState=mountTrustFailed||mountAuthenticationFailed?"attention":"reconnecting";
            mountMessage=mountTrustFailed?"服务器身份已变化，请先人工核对；未信任新密钥":mountAuthenticationFailed?"SSH 登录认证失败；若已更改服务器密码，请点击“更新登录密码”。原挂载和缓存已保留，自动重试已暂停。":"连接暂不可用，保留挂载和缓存并继续重试";
        }
        finally{mountLifecycle.Release();}
    }
    private async Task EnsureMountControlAsync(CancellationToken ct)
    {
        if(!IsConnected){await RestoreMountControlAsync(ct);return;}
        if(!await operations.WaitAsync(0,ct))return;
        bool restore=false;
        try{using var probe=client!.CreateCommand("true");probe.CommandTimeout=TimeSpan.FromSeconds(8);await probe.ExecuteAsync(ct);if(probe.ExitStatus!=0)throw new IOException("控制连接检查失败。");}
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
                if(!ParseOsRelease(check.Result).TryGetValue("ID",out var id)||id!="ubuntu")throw new UnsupportedOsException("恢复目标不再是 Ubuntu，已停止挂载恢复。");
                using var create=replacement.CreateCommand("mktemp -d /tmp/safe-code.XXXXXXXX");create.CommandTimeout=TimeSpan.FromSeconds(10);await create.ExecuteAsync(ct);string restoredDirectory=create.Result.Trim();
                if(!restoredDirectory.StartsWith("/tmp/safe-code."))throw new IOException("无法恢复管理通道。");
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
            // 只有远端确认同步卸载后才关闭文件桥，失败时保持守护，防止丢弃尚未上传的内容。
            await RequestAsync<MountResult>("mount_remove",new{clientId=mountClientId},ct);
            AccumulateTransfers();DisposeMountResource(fileForward);fileForward=null;DisposeMountResource(mountTransport);mountTransport=null;DisposeMountResource(fileServer);fileServer=null;
            mountClientId="";guardedMappings=[];Interlocked.Exchange(ref mountedAtTimestamp,0);mountState="idle";mountMessage="未挂载目录";
        }
        finally{mountLifecycle.Release();}
    }
}
