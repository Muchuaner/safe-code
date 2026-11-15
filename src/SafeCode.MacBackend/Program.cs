using System.Collections.Concurrent;
using System.Security.Cryptography;
using System.Text.Json;
using SafeCode.Core;
using SafeCode.Desktop;

if(!OperatingSystem.IsMacOS())throw new PlatformNotSupportedException("仅支持 macOS。");
if(args.Contains("--self-test")){await MacSelfTest.RunAsync();return;}
if(args.Length!=2||args[0]!="--data-directory")throw new ArgumentException("请从 Safe Code.app 启动。");
var directory=Path.GetFullPath(args[1]);Directory.CreateDirectory(directory);
File.SetUnixFileMode(directory,UnixFileMode.UserRead|UnixFileMode.UserWrite|UnixFileMode.UserExecute);
LocalSecrets.DirectoryOverride=directory;
bool offline=false;string? boot=await Console.In.ReadLineAsync();using(var document=JsonDocument.Parse(boot??"{}")){LocalSecrets.Initialize(Convert.FromBase64String(document.RootElement.GetProperty("key").GetString()!));offline=document.RootElement.TryGetProperty("offline",out var flag)&&flag.ValueKind==JsonValueKind.True;}
using var backend=new MacBackend(directory,offline);await backend.RunAsync();

internal sealed class MacBackend : IDisposable
{
    private static readonly JsonSerializerOptions Json=new(JsonDefaults.Options){WriteIndented=false};
    private readonly string directory;
    private readonly bool offline;
    private readonly ClientServices services;
    private readonly ClientPreferences preferences;
    private readonly MacClients clients;
    private readonly SemaphoreSlim writer=new(1),mutation=new(1);
    private readonly CancellationTokenSource lifetime=new();
    private readonly ConcurrentDictionary<string,DiagnosticTrace> traces=new();
    private readonly ConcurrentDictionary<int,Task> tasks=new();
    private SshSession? session;
    private MacHostControl? hostControl;
    private CancellationTokenSource? connecting,browsing;
    private (string Token,HostKeyInfo Key)? challenge;
    private DownloadedInstaller? update;
    private int connectionPending,activeMutations,serial;
    private string step="";
    public MacBackend(string directory,bool offline){this.directory=directory;this.offline=offline;services=new(directory);preferences=new(directory);clients=new(directory);hostControl=new(directory);}
    public async Task RunAsync()
    {
        _=PublishStatusAsync();await EmitAsync(new{type="ready"});
        while(await Console.In.ReadLineAsync() is {} line)
        {
            if(line.Length>1048576)continue;int key=Interlocked.Increment(ref serial);
            var task=Task.Run(()=>RespondAsync(line));tasks[key]=task;_=task.ContinueWith(completed=>tasks.TryRemove(key,out _));
        }
        // Native app normally calls app.shutdown first. Pipe loss stops all loopback tunnels immediately.
        lifetime.Cancel();session?.Dispose();await Task.WhenAll(tasks.Values);
    }
    private async Task RespondAsync(string line)
    {
        string id="",operation="";DiagnosticTrace? trace=null;Exception? failure=null;bool gate=false;
        try
        {
            using var doc=JsonDocument.Parse(line);var root=doc.RootElement;id=root.GetProperty("id").GetString()??"";operation=root.GetProperty("op").GetString()??"";var args=root.GetProperty("args");
            bool modifies=operation is "deploy" or "job.resume" or "mount.apply" or "mount.install" or "cloud.install" or "cloud.expose" or "ip.apply" or "terminal" or "host.enable" or "host.disable" or "app.shutdown";
            if(modifies){if(!await mutation.WaitAsync(0))throw new InvalidOperationException("已有任务正在执行，请等待完成。");gate=true;Interlocked.Increment(ref activeMutations);}
            var diagnosticOperation=DiagnosticTrace.OperationFor(operation,args);
            if(diagnosticOperation!=null&&services.DiagnosticsAllowed){trace=new(diagnosticOperation,services.DiagnosticGeneration);traces[diagnosticOperation]=trace;}
            object? result=await HandleAsync(operation,args);await EmitAsync(new{id,result,error=(string?)null});
        }
        catch(Exception error){failure=error;string? issue=trace==null?null:services.RecordFailure(trace,error);await EmitAsync(new{id,result=(object?)null,error=error.Message+(issue==null?"":"\n问题编号："+issue)});}
        finally
        {
            if(gate){step="";Interlocked.Decrement(ref activeMutations);mutation.Release();}
            if(trace!=null){traces.TryRemove(trace.Operation,out _);if(operation!="connect"||challenge==null||failure!=null)_=services.SendDiagnosticAsync(trace,failure,lifetime.Token);}
        }
    }
    private async Task<object?> HandleAsync(string op,JsonElement args)
    {
        string Read(string key,string fallback="")=>args.TryGetProperty(key,out var value)?value.GetString()??fallback:fallback;
        bool Flag(string key)=>args.TryGetProperty(key,out var value)&&value.ValueKind==JsonValueKind.True;
        var ct=lifetime.Token;
        if(offline){
            if(op=="services.preferences")return new{enabled=false,confirmed=false,diagnosticsEnabled=false,serviceAvailable=false,recipient=(string?)null,issueId=(string?)null};
            if(op=="login.load")return null;
            if(op=="updates.check")return new UpdateInfo(ClientServices.Version,null,"隔离预览，不连接网络",null);
            if(op=="services.consent")return await services.SetConsentAsync(false,false);
            if(op=="diagnostics.consent")return services.SetDiagnosticConsent(false);
            if(op is not ("app.state" or "app.shutdown" or "preview.begin" or "services.preferences" or "login.load" or "notice.state" or "notice.accept" or "mount.status" or "host.status" or "bridge.status" or "folder.cancel"))throw new InvalidOperationException("隔离预览不允许连接服务器或执行网络操作。");
        }
        switch(op)
        {
            case "app.state":return new{busy=activeMutations>0,guarding=session?.HasMounts==true||hostControl?.Enabled==true};
            case "app.shutdown":
                if(connectionPending!=0)throw new InvalidOperationException("正在连接，请先取消连接后退出。");
                if(hostControl?.Enabled==true)await hostControl.DisableAsync(ct);
                if(session?.HasMounts==true)await session.UnmountAsync(ct);
                return true;
            case "mount.reconnect":if(session!=null)await session.RecoverMountsAsync(ct);return true;
            case "bridge.status":return new{running=false,connected=false,cleanupPending=false,port=18797,model="sonnet",writable=false,message="当前附件仅提供 Windows 适配器，macOS 暂不可用"};
            case "bridge.start":case "bridge.copy-key":case "bridge.copy-config":throw new InvalidOperationException("尚未取得经过验证的 macOS 适配器，不能运行 Windows EXE。此模块保持关闭。");
            case "bridge.stop":return new{running=false,connected=false,cleanupPending=false,message="未启动"};
            case "host.status":return hostControl?.Status??new(false,"disabled","","","未启用；需先开启 macOS 系统远程登录");
            case "host.enable":
                if(!Flag("riskAccepted"))throw new InvalidOperationException("请先阅读并确认本机远程控制权限说明。");
                hostControl??=new(directory);return await hostControl.EnableAsync(RequireSession(),Read("alias","mac"),ct);
            case "host.disable":return hostControl==null?new HostControlStatus(false,"disabled","","","未启用"):await hostControl.DisableAsync(ct);
            case "preview.begin":
                if(activeMutations>0||connectionPending!=0||session?.HasMounts==true||hostControl?.Enabled==true)throw new InvalidOperationException("请先结束当前操作并安全卸载目录。");
                session?.Dispose();session=null;challenge=null;return true;
            case "services.preferences":return services.Preferences();
            case "services.consent":return await services.SetConsentAsync(Flag("enabled"),Flag("confirmed"));
            case "diagnostics.consent":return services.SetDiagnosticConsent(Flag("enabled"));
            case "feedback.send":await services.SendFeedbackAsync(Read("message"),ct);return true;
            case "updates.check":return await services.CheckUpdateAsync(Flag("force"),ct);
            case "updates.download":update=await services.DownloadUpdateAsync((done,total)=>_=EmitAsync(new{type="updateProgress",done,total}),ct);return true;
            case "updates.install":
                if(activeMutations>0||session?.HasMounts==true||hostControl?.Enabled==true)throw new InvalidOperationException("请先完成任务、停用本机控制并安全卸载目录，再打开新版安装镜像。");
                if(update==null)throw new InvalidOperationException("请先下载并校验 macOS 安装镜像。");
                await ClientServices.VerifyInstallerAsync(update,ct);await EmitAsync(new{type="openFile",path=update.Path});return true;
            case "login.load":return LocalSecrets.Load();
            case "notice.state":return new{accepted=File.Exists(Path.Combine(directory,"notice-accepted-v2"))||services.NoticeWasConfirmed};
            case "notice.accept":File.WriteAllText(Path.Combine(directory,"notice-accepted-v2"),DateTimeOffset.UtcNow.ToString("O"));return true;
            case "connection.cancel":connecting?.Cancel();challenge=null;return true;
            case "host.trust":
                if(challenge is not {} current||current.Token!=Read("token"))throw new InvalidOperationException("服务器身份确认已失效。");
                new HostTrustStore(directory).Accept(current.Key);challenge=null;return true;
            case "connect":
                if(activeMutations>0||session?.HasMounts==true||hostControl?.Enabled==true)throw new InvalidOperationException("请先完成任务、停用本机控制并安全卸载目录，再更换连接。");
                if(Interlocked.CompareExchange(ref connectionPending,1,0)!=0)throw new InvalidOperationException("正在连接，请稍候。");
                try
                {
                    connecting?.Dispose();connecting=CancellationTokenSource.CreateLinkedTokenSource(ct);connecting.CancelAfter(TimeSpan.FromSeconds(90));
                    session?.Dispose();session=new(directory);session.Log+=line=>Log(step,line);session.DirectoryLog+=line=>Log("folder",line);
                    var settings=new ConnectionSettings(Read("host"),Read("username"),Read("password"),args.TryGetProperty("port",out var port)?port.GetInt32():22);
                    var server=await session.ConnectAsync(settings,_=>false,connecting.Token);challenge=null;
                    try{LocalSecrets.Save(settings);}catch{Log("connection","连接成功，但钥匙串保护的登录记忆未能保存。下次请重新填写。");}
                    return new{ready=true,server};
                }
                catch(HostTrustRequiredException error){challenge=(Guid.NewGuid().ToString("N"),error.Key);return new{requiresTrust=true,token=challenge.Value.Token,key=error.Key};}
                catch(Exception error){challenge=null;throw new InvalidOperationException(ConnectionErrors.Describe(error),error);}
                finally{Interlocked.Exchange(ref connectionPending,0);}
            case "inspect":return await RequireSession().RequestAsync<ServerInfo>("inspect",null,ct);
            case "ip.info":return await IpIntelligenceService.QueryAsync(Read("ip"),ct);
            case "metrics":return await RequireSession().MeasureNetworkAsync(ct);
            case "job.resume":step=Read("operation");return await RequireSession().ResumeJobAsync(Read("jobId"),ct);
            case "deploy":
                step=Read("step");var locale=args.TryGetProperty("locale",out var region)&&region.ValueKind!=JsonValueKind.Null?JsonSerializer.Deserialize<LocaleSettings>(region,JsonDefaults.Options):null;
                var draft=step=="account"?preferences.ReadDraft(RequireSession()):null;
                if(step=="account"&&hostControl?.Enabled==true)throw new InvalidOperationException("请先停用本机控制，再修改工作账户。");
                var result=await RequireSession().DeployAsync(step,locale,draft?.Username??Read("username","workspace"),ct,draft?.Password??Read("password"),draft?.Hostname??Read("hostname"));
                if(step=="account"&&result.Statuses.GetValueOrDefault("account"))preferences.ClearDraft(RequireSession());
                if(step=="ssh")await RequireSession().VerifyWorkspaceKeyAsync(ct);return result;
            case "folder.cancel":try{browsing?.Cancel();}catch(ObjectDisposedException){}return true;
            case "folder.create":return await RequireSession().CreateDirectoryAsync(Read("path"),Read("name"),ct);
            case "folder.list":
                try{browsing?.Cancel();}catch(ObjectDisposedException){}var browse=CancellationTokenSource.CreateLinkedTokenSource(ct);browsing=browse;
                using(browse){browse.CancelAfter(TimeSpan.FromSeconds(12));try{return await RequireSession().ListDirectoriesAsync(Read("path"),browse.Token);}catch(OperationCanceledException){throw new TimeoutException("读取目录超时，请检查网络后重试。");}}
            case "account.draft.get":return preferences.ReadDraft(RequireSession());
            case "account.draft.save":preferences.SaveDraft(RequireSession(),new(Read("username"),Read("hostname"),Read("password")));return true;
            case "account":return await RequireSession().RequestAsync<AccountInfo>("account_info",null,ct);
            case "desktop.resolution.get":return preferences.ReadResolution();
            case "desktop.resolution.save":preferences.SaveResolution(new(args.GetProperty("width").GetInt32(),args.GetProperty("height").GetInt32()));return new{windows=0,dynamicUpdated=0,deferred=1};
            case "desktop":await EmitAsync(new{type="openFile",path=await clients.DesktopAsync(RequireSession(),preferences.ReadResolution(),ct)});return true;
            case "terminal":step="ssh";await clients.OpenTerminalAsync(RequireSession(),ct);return RequireSession().Server;
            case "cloud.install":step="cloud";return await RequireSession().RequestAsync<ServerInfo>("cloud",null,ct);
            case "cloud.open":
                if(RequireSession().Server?.CloudReady!=true)throw new InvalidOperationException("请先安装 CloudCLI。");
                if(activeMutations>0)throw new InvalidOperationException("请等待当前部署完成。");
                await RequireSession().RequestAsync<ServerInfo>("cloud_setup",null,ct);uint cloudPort=await RequireSession().ForwardAsync(3001,ct);
                await EmitAsync(new{type="openUrl",url="http://127.0.0.1:"+cloudPort});return true;
            case "cloud.expose":step="cloud_public";return await RequireSession().RequestAsync<JsonElement>("cloud_public",new{host=RequireSession().Host},ct);
            case "mount.settings":return RequireSession().ReadMountMappings();
            case "mount.install":step="mount_install";return await RequireSession().RequestAsync<ServerInfo>("mount_install",null,ct);
            case "mount.status":return session?.GetMountStatus()??new(0,"idle","未挂载目录",0,0,0,0,0);
            case "mount.apply":step="mount_apply";return await RequireSession().ApplyMountsAsync(JsonSerializer.Deserialize<List<MountMapping>>(args.GetProperty("mappings"),JsonDefaults.Options)??[],ct);
            case "ip.apply":
                step="ip_policy";var changed=await RequireSession().ApplyIpPolicyAsync(JsonSerializer.Deserialize<IpPolicySettings>(args,JsonDefaults.Options)??throw new ArgumentException("缺少设置。"),ct);
                if(LocalSecrets.Load() is {} saved)LocalSecrets.Save(saved with{Host=RequireSession().Host,Port=RequireSession().Port});return changed;
            default:throw new InvalidOperationException("不支持的客户端操作："+op);
        }
    }
    private SshSession RequireSession()=>connectionPending==0&&session?.IsConnected==true&&session.Server!=null?session:throw new InvalidOperationException("SSH 已断开，请重新连接。");
    private void Log(string operation,string line){if(traces.TryGetValue(operation=="folder"?"directory_list":string.IsNullOrEmpty(operation)?"connection":operation,out var trace))trace.Observe(line);_=EmitAsync(new{type="logs",lines=new[]{new{step=operation,line}}});}
    private async Task PublishStatusAsync()
    {
        using var timer=new PeriodicTimer(TimeSpan.FromSeconds(1));try{while(await timer.WaitForNextTickAsync(lifetime.Token))await EmitAsync(new{type="status",busy=activeMutations>0||connectionPending!=0,mount=session?.GetMountStatus()??new(0,"idle","未挂载目录",0,0,0,0,0),host=hostControl?.Status});}catch(OperationCanceledException){}
    }
    private async Task EmitAsync(object value){await writer.WaitAsync();try{await Console.Out.WriteLineAsync(JsonSerializer.Serialize(value,Json));await Console.Out.FlushAsync();}finally{writer.Release();}}
    public void Dispose(){lifetime.Cancel();hostControl?.Dispose();session?.Dispose();services.Dispose();lifetime.Dispose();}
}
