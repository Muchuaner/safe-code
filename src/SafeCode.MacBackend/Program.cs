using System.Collections.Concurrent;
using System.Security.Cryptography;
using System.Text.Json;
using SafeCode.Core;
using SafeCode.Desktop;

if(!OperatingSystem.IsMacOS())throw new PlatformNotSupportedException("僅支援 macOS。");
if(args.Contains("--self-test")){await MacSelfTest.RunAsync();return;}
if(args.Length!=2||args[0]!="--data-directory")throw new ArgumentException("請從 Safe Code.app 啟動。");
var directory=Path.GetFullPath(args[1]);Directory.CreateDirectory(directory);
File.SetUnixFileMode(directory,UnixFileMode.UserRead|UnixFileMode.UserWrite|UnixFileMode.UserExecute);
LocalSecrets.DirectoryOverride=directory;
bool offline=false;string? boot=await Console.In.ReadLineAsync();using(var document=JsonDocument.Parse(boot??"{}")){if(document.RootElement.TryGetProperty("key",out var key))LocalSecrets.Initialize(Convert.FromBase64String(key.GetString()!));offline=document.RootElement.TryGetProperty("offline",out var flag)&&flag.ValueKind==JsonValueKind.True;}
using var backend=new MacBackend(directory,offline);await backend.RunAsync();

internal sealed class MacBackend : IDisposable
{
    private static readonly JsonSerializerOptions Json=new(JsonDefaults.Options){WriteIndented=false};
    private readonly string directory;
    private readonly bool offline;
    private readonly ClientServices services;
    private readonly object consentGate=new();
    private readonly ClientPreferences preferences;
    private readonly MacClients clients;
    private readonly RdpResizePreferences desktopDisplay;
    private readonly SemaphoreSlim writer=new(1),mutation=new(1);
    private readonly CancellationTokenSource lifetime=new();
    private readonly ConcurrentDictionary<string,DiagnosticTrace> traces=new();
    private readonly ConcurrentDictionary<int,Task> tasks=new();
    private SshSession? session;
    private DesktopKeyAccess? keyAccess;
    private DesktopKeyAccess.DeviceSetup? pendingKeyDevice;
    private MacDeviceTunnel? keyDeviceTunnel;
    private SshSession? keyStartedHost;
    private MacHostControl? hostControl;
    private CancellationTokenSource? connecting,browsing;
    private sealed record GatewayContext(SshSession Session, string Token);
    private GatewayContext? gatewayContext;
    private sealed record GatewayObservation(SshSession Session,int Generation,bool Enabled,string Outcome);
    private GatewayObservation? gatewayObservation;
    private (string Token,HostKeyInfo Key)? challenge;
    private DownloadedInstaller? update;
    private int connectionPending,activeMutations,serial;
    private string step="";
    public MacBackend(string directory,bool offline){this.directory=directory;this.offline=offline;services=new(directory,endpointEnabled:!offline);preferences=new(directory);clients=new(directory);desktopDisplay=new(directory);hostControl=new(directory);}
    public async Task RunAsync()
    {
        var statusUpdates=PublishStatusAsync();
        await EmitAsync(new{type="ready"});
        while(await Console.In.ReadLineAsync() is {} line)
        {
            if(line.Length>1048576)continue;int key=Interlocked.Increment(ref serial);
            var task=Task.Run(()=>RespondAsync(line));tasks[key]=task;_=task.ContinueWith(completed=>tasks.TryRemove(key,out _));
        }
        // Native app normally calls app.shutdown first. Pipe loss stops all loopback tunnels immediately.
        lifetime.Cancel();session?.Dispose();await Task.WhenAll(tasks.Values.Append(statusUpdates));
    }
    private async Task RespondAsync(string line)
    {
        string id="",operation="";DiagnosticTrace? trace=null;Exception? failure=null;bool gate=false;
        try
        {
            using var doc=JsonDocument.Parse(line);var root=doc.RootElement;id=root.GetProperty("id").GetString()??"";operation=root.GetProperty("op").GetString()??"";var args=root.GetProperty("args");
            bool modifies=operation is "deploy" or "job.resume" or "mount.apply" or "mount.install" or "cloud.install" or "cloud.expose" or "ip.apply" or "terminal" or "desktop" or "desktop.resolution.save" or "host.enable" or "host.disable" or "app.shutdown" or "connection.reauthenticate"
                or "desktopKey.enable" or "desktopKey.disable" or "desktopKey.refreshModels" or "desktopKey.authorizeDevice" or "desktopKey.cancelDevice" or "safeCodeApi.enablePublic" or "safeCodeApi.install" or "safeCodeApi.repair"
                or "socks.probe" or "socks.enable" or "socks.disable";
            if(modifies){if(!await mutation.WaitAsync(0))throw new InvalidOperationException("已有任務正在執行，請等待完成。");gate=true;Interlocked.Increment(ref activeMutations);}
            var diagnosticOperation=offline||operation=="mount.reconnect"&&session?.HasMounts!=true?null:DiagnosticTrace.OperationFor(operation,args);
            if(diagnosticOperation!=null)trace=services.BeginDiagnostic(diagnosticOperation,DiagnosticTrace.IsBackgroundOperation(diagnosticOperation,args));
            if(trace!=null)traces[trace.Operation]=trace;
            object? result=await HandleAsync(operation,args,error=>failure=error);await EmitAsync(new{id,result,error=(string?)null});
        }
        catch(Exception error){failure=error;string? issue=trace==null?null:services.RecordFailure(trace,error);await EmitAsync(new{id,result=(object?)null,error=ConnectionErrors.DescribeOperation(error)+(issue==null?"":"\n問題編號："+issue)});}
        finally
        {
            if(gate){step="";Interlocked.Decrement(ref activeMutations);mutation.Release();}
            if(trace!=null){traces.TryRemove(new KeyValuePair<string,DiagnosticTrace>(trace.Operation,trace));if(operation!="connect"||challenge==null||failure!=null)_=services.SendDiagnosticAsync(trace,failure,lifetime.Token);}
        }
    }
    private async Task<JsonElement> ReadGatewayStatusAsync(SshSession target,CancellationToken ct)
    {
        var previous=Volatile.Read(ref gatewayObservation);
        bool recovering=previous is {Enabled:true,Outcome:not "none"} saved&&ReferenceEquals(saved.Session,target);
        var trace=services.BeginDiagnostic(recovering?"gateway_reconnect":"gateway_status",true);
        bool enabled=false;
        Exception? failure=null;
        try
        {
            var status=await target.ReadPublicGatewayAsync(true,ct);
            enabled=status.TryGetProperty("enabled",out var field)&&field.ValueKind==JsonValueKind.True;
            return status;
        }
        catch(Exception error){failure=error;throw;}
        finally
        {
            if(trace!=null&&ReferenceEquals(session,target))
            {
                bool knownEnabled=failure!=null&&previous is {Enabled:true} prior&&ReferenceEquals(prior.Session,target);
                var next=new GatewayObservation(target,trace.Generation,enabled||knownEnabled,DiagnosticTrace.ErrorKind(failure));
                if(Interlocked.Exchange(ref gatewayObservation,next)!=next)_=services.SendDiagnosticAsync(trace,failure,ct);
            }
        }
    }

    private async Task<object> AuthorizeKeyDeviceAsync(CancellationToken ct)
    {
        var setup=pendingKeyDevice??throw new InvalidOperationException("請重新驗證接入 Key。");
        if(setup.Switching)
        {
            try{await KeyAccess().DisableAsync(ct);}
            finally{await StopKeyDeviceAsync();}
            if(KeyAccess().Configured)throw new InvalidOperationException("原網關尚未確認解綁，請先點擊「重試解綁」查看結果。");
            setup=setup with {Switching=false};
            pendingKeyDevice=setup;
        }
        var control=hostControl??=new MacHostControl(directory);
        if(setup.HostSession is { } target)return await AuthorizeKeyHostControlAsync(setup,target,control,ct);
        if(control.Enabled)throw new InvalidOperationException("本機已有控制通道，請先停用後再部署 Key 接入。");
        await StopKeyDeviceAsync();
        step="host_enable";
        DesktopKeyAccess.DeviceBinding? device=setup.Existing;
        MacDeviceTunnel? tunnel=null;
        bool startedLocal=false;
        try
        {
            device??=await MacDeviceTunnel.PrepareAsync(setup.Remote,ct);
            var ssh=await control.EnableDeviceAsync(device.PublicKey,ct);
            startedLocal=true;
            tunnel=await MacDeviceTunnel.ConnectAsync(setup.Remote,device,ssh.Port,directory,ct);
            await tunnel.VerifyAsync(ssh,ct);
            var activeTunnel=tunnel;
            var path=new DesktopKeyAccess.DevicePath(()=>activeTunnel.ConnectionProblem(control.Enabled),
                (force,token)=>activeTunnel.CheckReturnAsync(force,control.Enabled,token),()=>activeTunnel.Generation);
            var result=await KeyAccess().EnableAsync("",ct,setup.Replace,setup.UseSaved,setup,device,devicePath:path);
            keyDeviceTunnel=tunnel;tunnel=null;pendingKeyDevice=null;
            return result;
        }
        catch
        {
            if(tunnel!=null)await tunnel.DisposeAsync();
            if(startedLocal)await control.DisableAsync(CancellationToken.None);
            if(setup.Existing==null&&device!=null)
            {
                try{await DesktopKeyAccess.DeviceControlAsync(setup.Remote,
                    new{action="revoke",device.DeviceId,device.DeviceProof},CancellationToken.None);}
                catch(Exception error)when(error is HttpRequestException or InvalidOperationException or OperationCanceledException){}
            }
            throw;
        }
        finally{step="";}
    }

    private async Task<object> AuthorizeKeyHostControlAsync(DesktopKeyAccess.DeviceSetup setup,SshSession target,
        MacHostControl control,CancellationToken ct)
    {
        if(!ReferenceEquals(RequireSession(),target))throw new InvalidOperationException("SSH 會話已變更，請重新驗證接入 Key。");
        if(control.Enabled&&!control.IsEnabledFor(target))throw new InvalidOperationException("現有本機遠端控制屬於另一台 VPS。請先在本機遠端控制中停用它。");
        step="host_enable";
        bool startedHere=false;
        try
        {
            if(!await target.CanReuseHostControlForKeyAsync(setup.Remote,ct))throw new InvalidOperationException("該 Key 或網關的裝置授權已變更，請重新驗證接入 Key。");
            if(!control.Enabled){await control.EnableAsync(target,control.DefaultAlias,ct);startedHere=true;}
            else if(target.HostControlStatus.State!="connected")await target.MaintainHostControlForChatAsync(ct);
            var leaseReady=target.CaptureVerifiedHostControl();
            bool HostReady()=>ReferenceEquals(session,target)&&control.IsEnabledFor(target)&&leaseReady();
            string hostname=Environment.MachineName.Length<=48?Environment.MachineName:Environment.MachineName[..48];
            var binding=await target.BindPublicGatewayHostAsync(setup.Remote,hostname,ct);
            var device=new DesktopKeyAccess.DeviceBinding(binding.GetProperty("deviceId").GetString()!,
                binding.GetProperty("deviceProof").GetString()!,binding.GetProperty("alias").GetString()!,0,"");
            var result=await KeyAccess().EnableAsync("",ct,setup.Replace,setup.UseSaved,setup,device,hostReady:HostReady,
                devicePath:new(()=>HostReady()?null:target.HostControlStatus.State=="connected"?"本次授權的 SSH 會話已斷開，請重新啟用接入。":target.HostControlStatus.Message,
                    (force,token)=>target.MaintainHostControlForChatAsync(token,force),()=>target.HostNetworkGeneration));
            if(startedHere)keyStartedHost=target;
            pendingKeyDevice=null;
            return result;
        }
        catch{if(startedHere)await control.DisableAsync(CancellationToken.None);throw;}
        finally{step="";}
    }

    private async Task StopKeyDeviceAsync()
    {
        if(keyDeviceTunnel is { } tunnel)
        {
            keyDeviceTunnel=null;
            try{await tunnel.DisposeAsync();}
            finally{if(hostControl?.Enabled==true)await hostControl.DisableAsync(CancellationToken.None);}
        }
        if(keyStartedHost is { } target)
        {
            keyStartedHost=null;
            if(hostControl?.IsEnabledFor(target)==true)await hostControl.DisableAsync(CancellationToken.None);
        }
    }

    private async Task<object?> HandleAsync(string op,JsonElement args,Action<Exception>? observeFailure=null)
    {
        string Read(string key,string fallback="")=>args.TryGetProperty(key,out var value)?value.GetString()??fallback:fallback;
        bool Flag(string key)=>args.TryGetProperty(key,out var value)&&value.ValueKind==JsonValueKind.True;
        var ct=lifetime.Token;
        if(offline){
            if(op=="login.load")return null;
            if(op=="updates.check")return new UpdateInfo(ClientServices.Version,null,"隔離預覽，不連線網絡",null);
            if(op is not ("app.state" or "app.shutdown" or "preview.begin" or "services.preferences" or "services.consent" or "services.confirm" or "diagnostics.consent" or "login.load" or "notice.state" or "notice.accept" or "mount.status" or "host.status" or "bridge.status" or "folder.cancel"))throw new InvalidOperationException("隔離預覽不允許連接伺服器或執行網絡操作。");
        }
        switch(op)
        {
            case "app.state":return new{busy=activeMutations>0,guarding=session?.HasMounts==true||hostControl?.Enabled==true||keyAccess?.Running==true};
            case "app.shutdown":
                if(connectionPending!=0)throw new InvalidOperationException("正在連線，請先取消連線後退出。");
                try{if(keyAccess!=null)await keyAccess.StopAsync();}
                finally{await StopKeyDeviceAsync();}
                if(hostControl?.Enabled==true)await hostControl.SuspendAsync(ct);
                if(session?.HasMounts==true)await session.UnmountAsync(ct);
                return true;
            case "mount.reconnect":
                if(session is {HasMounts:true} mountSession)
                {
                    await mountSession.RecoverMountsAsync(ct);
                    var mountStatus=mountSession.GetMountStatus();
                    if(mountStatus.State is "reconnecting" or "attention")observeFailure?.Invoke(mountStatus.AuthenticationRequired
                        ? new Renci.SshNet.Common.SshAuthenticationException("Mount authentication required.") : new IOException());
                }
                return true;
            case "bridge.status":return new{running=false,connected=false,cleanupPending=false,port=18797,model="sonnet",writable=false,message="當前附件僅提供 Windows 適配器，macOS 暫不可用",startedAt=(string?)null,stoppedAt=(string?)null,usage=new{available=false,input=0L,output=0L,cacheRead=0L,cacheWrite=0L,total=0L,note="macOS 暫無可用的 Codex 接入用量來源"},usageSource=""};
            case "bridge.start":case "bridge.copy-key":case "bridge.copy-config":throw new InvalidOperationException("尚未取得經過驗證的 macOS 適配器，不能執行 Windows EXE。此模組保持關閉。");
            case "bridge.stop":return new{running=false,connected=false,cleanupPending=false,message="未啟動"};
            case "secrets.initialize":
                LocalSecrets.Initialize(Convert.FromBase64String(Read("key")));return true;
            case "desktopKey.invalidate":keyDeviceTunnel?.Invalidate();session?.InvalidateHostVerification();return true;
            case "desktopKey.connection":return keyAccess?.ConnectionStatus;
            case "desktopKey.mode":
                if (args.ValueKind==JsonValueKind.Object && args.TryGetProperty("enabled",out var apiMode))
                    preferences.SetApiAccessMode(apiMode.GetBoolean());
                return preferences.PrefersApiAccess;
            case "desktopKey.status" when !LocalSecrets.IsUnlocked:
                return new{configured=File.Exists(Path.Combine(directory,"desktop-key.state")),running=false};
            case "desktopKey.status":
                return await KeyAccess().RestoreAsync(ct,interactive:Flag("interactive"));
            case "desktopKey.list": return await KeyAccess().ListKeysAsync(ct);
            case "desktopKey.save":
                await KeyAccess().SaveKeyAsync(Read("key"),Read("name"),ct);
                return true;
            case "desktopKey.rename":
                await KeyAccess().RenameKeyAsync(Read("id"),Read("name"),ct);
                return true;
            case "desktopKey.forget":
                await KeyAccess().ForgetKeyAsync(Read("id"),ct);
                return true;
            case "desktopKey.savedBalance": return await KeyAccess().SavedBalanceAsync(Read("id"),ct);
            case "desktopKey.enable":
                _=LocalSecrets.RequireKey();
                if(Read("savedId")==""&&keyDeviceTunnel!=null&&hostControl?.Enabled==true)throw new InvalidOperationException("請先停用當前 Key 接入，再更換 Key。");
                pendingKeyDevice=null;
                var keyResult=await KeyAccess().EnableAsync(Read("key"),ct,replace:Flag("replace"),useSaved:Flag("useSaved"),
                    savedId:Read("savedId"),checkDevice:true,deviceSession:connectionPending==0&&session?.IsConnected==true&&session.Server!=null?session:null);
                if(keyResult is not DesktopKeyAccess.DeviceSetup setup)return keyResult;
                pendingKeyDevice=setup;
                if(Read("savedId")!=""&&await KeyAccess().HasDeviceConsentAsync(setup.Remote,ct))
                    return await AuthorizeKeyDeviceAsync(ct);
                var keyHost=setup.HostSession;
                bool existingControl=keyHost!=null&&hostControl?.IsEnabledFor(keyHost)==true;
                return new{requiresDevice=true,reuseControl=keyHost!=null,existingControl,
                    alias=existingControl?keyHost!.HostControlStatus.Alias:setup.Existing?.Alias};
            case "desktopKey.authorizeDevice":
                if(!Flag("riskAccepted"))throw new InvalidOperationException("請先閱讀並同意本機遠端控制權限說明。");
                _=LocalSecrets.RequireKey();
                return await AuthorizeKeyDeviceAsync(ct);
            case "desktopKey.cancelDevice":pendingKeyDevice=null;return true;
            case "desktopKey.deviceStatus":return keyDeviceTunnel?.Status(hostControl?.Enabled==true);
            case "desktopKey.disable":
                pendingKeyDevice=null;
                try{return await KeyAccess().DisableAsync(ct);}
                finally{await StopKeyDeviceAsync();}
            case "desktopKey.refreshModels": return await KeyAccess().RefreshModelsAsync(ct);
            case "desktopKey.balance": return await KeyAccess().ReadBalanceAsync(ct);
            case "safeCodeApi.refreshSubscription":
                return await RequireGatewaySession().RefreshPublicGatewaySubscriptionAsync(ct);
            case "safeCodeApi.publicStatus":
                return await ReadGatewayStatusAsync(RequireGatewaySession(),ct);
            case "safeCodeApi.install":
                if (!Flag("accepted")) throw new InvalidOperationException("請先確認安裝 Safe Code API 服務。");
                return await RequireGatewaySession().InstallPublicGatewayAsync(ct,
                    args.TryGetProperty("cloudcli",out var cloudcliInstall)?cloudcliInstall:null);
            case "safeCodeApi.repair":
                if (!Flag("accepted")) throw new InvalidOperationException("請先確認故障修復範圍。");
                step="gateway_repair";
                return await RequireGatewaySession().RepairPublicGatewayAsync(Flag("reinstallCloudCLI"),ct);
            case "safeCodeApi.enablePublic":
                if (!Flag("accepted")) throw new InvalidOperationException("請先點擊公網網關的「啟用」。");
                return await RequireGatewaySession().EnablePublicGatewayAsync(ct);
            case "safeCodeApi.context":
                var currentGatewaySession = RequireGatewaySession();
                await currentGatewaySession.RequirePublicGatewayAsync(ct);
                if (!ReferenceEquals(currentGatewaySession, RequireGatewaySession()))
                    throw new InvalidOperationException("SSH 會話已變更，請重新開啟網關管理。");
                var context = new GatewayContext(currentGatewaySession, Guid.NewGuid().ToString("N"));
                gatewayContext = context;
                return context.Token;
            case "safeCodeApi.manage":
                var gatewaySession = RequireGatewaySession();
                var bound = gatewayContext;
                if (bound == null || !ReferenceEquals(gatewaySession, bound.Session) || Read("context") != bound.Token)
                    throw new InvalidOperationException("SSH 會話已變更，請重新開啟網關管理。");
                var request=args.Deserialize<Dictionary<string,JsonElement>>()!;
                request.Remove("background");
                return await gatewaySession.ManagePublicGatewayAsync(request,ct);
            case "codex.open":return await MacClients.OpenLocalClientAsync("Codex",ct);
            case "claudeDesktop.prepare":return await KeyAccess().PrepareLaunchAsync(ct);
            case "host.status":return keyDeviceTunnel?.Status(hostControl?.Enabled==true)??hostControl?.Status??new(false,"disabled","","","未啟用；需先開啟 macOS 系統遠端登入");
            case "host.enable":
                if(!Flag("riskAccepted"))throw new InvalidOperationException("請先閱讀並確認本機遠端控制權限說明。");
                if(keyDeviceTunnel!=null||pendingKeyDevice!=null)throw new InvalidOperationException("請先完成或取消當前 Key 的本機授權。");
                hostControl??=new(directory);return await hostControl.EnableAsync(RequireSession(),Read("alias","mac"),ct);
            case "host.disable":
                if(connectionPending!=0)throw new InvalidOperationException("請等待當前伺服器連線完成。");
                if(keyDeviceTunnel!=null)
                {
                    try{await KeyAccess().DisableAsync(ct);}
                    finally{await StopKeyDeviceAsync();}
                }
                if(keyAccess?.ReusesHostControl==true)
                {
                    await keyAccess.StopAsync();
                    keyStartedHost=null;
                }
                return hostControl==null?new HostControlStatus(false,"disabled","","","未啟用"):await hostControl.DisableAsync(ct);
            case "preview.begin":
                if(activeMutations>0||connectionPending!=0||session?.HasMounts==true||hostControl?.Enabled==true)throw new InvalidOperationException("請先結束當前操作並安全卸載目錄。");
                session?.Dispose();session=null;challenge=null;return true;
            case "services.preferences":
                lock(consentGate)return services.Preferences();
            case "services.consent":
            case "services.confirm":
                Task<object> consent;
                lock(consentGate)consent=op=="services.confirm"?services.ConfirmConsentAsync():services.SetConsentAsync(Flag("enabled"),Flag("confirmed"));
                return await consent;
            case "diagnostics.consent":
                lock(consentGate)return services.SetDiagnosticConsent(Flag("enabled"));
            case "diagnostics.native-failure":
                var nativeOperation=Read("operation");
                if(nativeOperation is not ("login_restore" or "account_customization" or "desktop_key_enable" or "desktop_key_disable" or "desktop_key_restore" or "desktop_launch"))return null;
                var nativeTrace=services.BeginDiagnostic(nativeOperation,false);
                if(nativeTrace==null)return null;
                Exception nativeFailure=Flag("cancelled")?new OperationCanceledException():nativeOperation=="desktop_launch"?new InvalidOperationException():new UnauthorizedAccessException();
                string? nativeIssue=services.RecordFailure(nativeTrace,nativeFailure);
                _=services.SendDiagnosticAsync(nativeTrace,nativeFailure,ct);
                return nativeIssue;
            case "feedback.send":await services.SendFeedbackAsync(Read("message"),ct);return true;
            case "updates.check":return await services.CheckUpdateAsync(Flag("force"),ct);
            case "updates.download":update=await services.DownloadUpdateAsync((done,total)=>_=EmitAsync(new{type="updateProgress",done,total}),ct);return true;
            case "updates.install":
                if(activeMutations>0||session?.HasMounts==true||hostControl?.Enabled==true)throw new InvalidOperationException("請先完成任務、停用本機控制，並安全卸載目錄，再開啟新版安裝鏡像。");
                if(update==null)throw new InvalidOperationException("請先下載並校驗 macOS 安裝鏡像。");
                await ClientServices.VerifyInstallerAsync(update,ct);if(LocalSecrets.IsUnlocked)LocalSecrets.CheckpointLogin();await EmitAsync(new{type="openFile",path=update.Path});return true;
            case "gateways.resume-settings":
                var remembered = LocalSecrets.IsUnlocked ? LocalSecrets.Load() : null;
                bool resumeHost=remembered!=null&&File.Exists(Path.Combine(directory,"desktop-key.state"))
                    &&hostControl?.CanResume(remembered)==true;
                return remembered!=null&&(resumeHost||SshSession.CanResumePublicGateway(directory,remembered))?remembered:null;
            case "login.load":case "login.restoreSaved":return LocalSecrets.IsUnlocked?LocalSecrets.Load():null;
            case "connection.info":return session==null?null:new{host=session.Host,port=session.Port,username=session.LoginUsername};
            case "connection.reauthenticate":
                if(connectionPending!=0)throw new InvalidOperationException("請等待當前連線完成。");
                var recovering=session??throw new InvalidOperationException("請先登入伺服器。");
                using(var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct))
                {
                    timeout.CancelAfter(TimeSpan.FromSeconds(90));
                    try{await recovering.ReauthenticateAsync(Read("password"),timeout.Token);}catch(Exception error){throw new InvalidOperationException(ConnectionErrors.Describe(error),error);}
                    try{if(LocalSecrets.IsUnlocked)LocalSecrets.Save(new(recovering.Host,recovering.LoginUsername,Read("password"),recovering.Port));}catch{Log("connection","新密碼已驗證，但登入記憶未能儲存，下次請重新填寫。");}
                }
                return new{ready=true,mount=recovering.GetMountStatus()};
            case "notice.state":return new{accepted=File.Exists(Path.Combine(directory,"notice-accepted-v2"))||services.NoticeWasConfirmed};
            case "notice.accept":File.WriteAllText(Path.Combine(directory,"notice-accepted-v2"),DateTimeOffset.UtcNow.ToString("O"));return true;
            case "connection.cancel":connecting?.Cancel();challenge=null;return true;
            case "host.trust":
                if(challenge is not {} current||current.Token!=Read("token"))throw new InvalidOperationException("伺服器身份確認已失效。");
                new HostTrustStore(directory).Accept(current.Key);challenge=null;return true;
            case "connect":
                if(activeMutations>0||session?.HasMounts==true||hostControl?.Enabled==true)throw new InvalidOperationException("請先完成任務、停用本機控制，並安全卸載目錄，再更換連線。");
                if(Interlocked.CompareExchange(ref connectionPending,1,0)!=0)throw new InvalidOperationException("正在連線，請稍候。");
                try
                {
                    connecting?.Dispose();connecting=CancellationTokenSource.CreateLinkedTokenSource(ct);connecting.CancelAfter(TimeSpan.FromSeconds(90));
                    session?.Dispose();session=new(directory);session.Log+=line=>Log(step,line);session.DirectoryLog+=line=>Log("folder",line);
                    var settings=new ConnectionSettings(Read("host"),Read("username"),Read("password"),args.TryGetProperty("port",out var port)?port.GetInt32():22);
                    var server=await session.ConnectAsync(settings,_=>false,connecting.Token);challenge=null;
                    var warnings=new List<string>();
                    if (!LocalSecrets.IsUnlocked)
                        warnings.Add("本機鑰匙串尚未授權，無法儲存本次登入資料。");
                    else
                    {
                        try{LocalSecrets.Save(settings);}
                        catch(Exception error) when(error is IOException or UnauthorizedAccessException or CryptographicException)
                        {
                            warnings.Add("連線已成功，但無法儲存本機登入資料。");
                        }
                    }
                    bool keyConfigured=File.Exists(Path.Combine(directory,"desktop-key.state"));
                    if(keyConfigured&&hostControl?.CanResume(settings)==true)
                    {
                        using var hostRestore=CancellationTokenSource.CreateLinkedTokenSource(connecting.Token);
                        hostRestore.CancelAfter(TimeSpan.FromSeconds(40));
                        try
                        {
                            if(await hostControl.ResumeAsync(session,hostRestore.Token) is {} hostWarning)warnings.Add(hostWarning);
                        }
                        catch(OperationCanceledException)when(!connecting.IsCancellationRequested)
                        {warnings.Add("本機遠端控制恢復超時，可在設定中重試。");}
                    }
                    connecting.Token.ThrowIfCancellationRequested();
                    return new{ready=true,server,gatewayWarnings=warnings};
                }
                catch(HostTrustRequiredException error){challenge=(Guid.NewGuid().ToString("N"),error.Key);return new{requiresTrust=true,token=challenge.Value.Token,key=error.Key};}
                catch(Exception error){
                    challenge=null;
                    if(Flag("automatic") && ConnectionErrors.CanRetry(error)){
                        observeFailure?.Invoke(error);
                        return new{retryable=true,message=ConnectionErrors.Describe(error)};
                    }
                    throw new InvalidOperationException(ConnectionErrors.Describe(error),error);
                }
                finally{Interlocked.Exchange(ref connectionPending,0);}
            case "inspect":return await RequireSession().RequestAsync<ServerInfo>("inspect",null,ct);
            case "ip.info":return await IpIntelligenceService.QueryAsync(Read("ip"),ct);
            case "metrics":return await RequireSession().MeasureNetworkAsync(ct);
            case "resources":return await RequireSession().ReadResourceStatsAsync(ct);
            case "environment.assess":return await RequireSession().AssessEnvironmentAsync(ct);
            case "job.resume":step=Read("operation");return await RequireSession().ResumeJobAsync(Read("jobId"),ct);
            case "deploy":
                step=Read("step");
                if(step=="account"&&hostControl?.Enabled==true)throw new InvalidOperationException("請先停用本機控制，再修改工作帳戶。");
                var deploymentTarget=RequireSession();
                var locale=preferences.RequireDeploymentChoices(deploymentTarget);
                var draft=step=="account"?preferences.ReadDraft(deploymentTarget)??throw new InvalidOperationException("請先儲存工作帳戶設定。"):null;
                var result=await deploymentTarget.DeployAsync(step,locale,draft?.Username??"workspace",ct,draft?.Password??"",draft?.Hostname??"");
                if(step=="account"&&result.Statuses.GetValueOrDefault("account"))preferences.ClearDraft(deploymentTarget);
                if(step=="ssh")await deploymentTarget.VerifyWorkspaceKeyAsync(ct);return result;
            case "folder.cancel":try{browsing?.Cancel();}catch(ObjectDisposedException){}return true;
            case "folder.create":return await RequireSession().CreateDirectoryAsync(Read("path"),Read("name"),ct);
            case "folder.list":
                try{browsing?.Cancel();}catch(ObjectDisposedException){}var browse=CancellationTokenSource.CreateLinkedTokenSource(ct);browsing=browse;
                using(browse){browse.CancelAfter(TimeSpan.FromSeconds(12));try{return await RequireSession().ListDirectoriesAsync(Read("path"),browse.Token);}catch(OperationCanceledException){throw new TimeoutException("讀取目錄超時，請檢查網絡後重試。");}}
            case "account.draft.get":return preferences.ReadDraft(RequireSession());
            case "account.change.status":return new{requiresConfirmation=preferences.RequiresAccountConfirmation(RequireSession())};
            case "account.draft.save":preferences.SaveDraft(RequireSession(),new(Read("username"),Read("hostname"),Read("password")));return true;
            case "deployment.settings.get":return preferences.ReadDeploymentChoices(RequireSession());
            case "deployment.locale.save":return preferences.SaveDeploymentLocale(RequireSession(),Read("region"));
            case "account":return await RequireSession().RequestAsync<AccountInfo>("account_info",null,ct);
            case "desktop.resolution.get":
                var resolution=preferences.ReadResolution();
                return new{resolution.Width,resolution.Height,displayMode=desktopDisplay.Read().ToString(),fullscreen=desktopDisplay.ReadFullscreen()};
            case "desktop.resolution.save":
                var mode=Read("displayMode",desktopDisplay.Read().ToString()) switch {
                    "Original"=>RdpResizeMode.Original,"Scale"=>RdpResizeMode.Scale,"Dynamic"=>RdpResizeMode.Dynamic,
                    _=>throw new ArgumentException("請選擇支援的遠端桌面顯示模式。")
                };
                preferences.SaveResolution(new(args.GetProperty("width").GetInt32(),args.GetProperty("height").GetInt32()));
                desktopDisplay.Save(mode);
                if(args.TryGetProperty("fullscreen",out _)) desktopDisplay.SaveFullscreen(Flag("fullscreen"));
                return new{windows=0,dynamicUpdated=0,deferred=1};
            case "desktop":
                var path=await clients.DesktopAsync(RequireSession(),preferences.ReadResolution(),desktopDisplay,ct);
                if(path!=null)await EmitAsync(new{type="openFile",path});
                return true;
            case "terminal":step="ssh";await clients.OpenTerminalAsync(RequireSession(),ct);return RequireSession().Server;
            case "cloud.install":step="cloud";return await RequireSession().RequestAsync<ServerInfo>("cloud",null,ct);
            case "cloud.open":
                if(RequireSession().Server?.CloudReady!=true)throw new InvalidOperationException("請先安裝 CloudCLI。");
                if(activeMutations>0)throw new InvalidOperationException("請等待當前部署完成。");
                await RequireSession().RequestAsync<ServerInfo>("cloud_setup",null,ct);uint cloudPort=await RequireSession().ForwardAsync(3001,ct);
                await EmitAsync(new{type="openUrl",url="http://127.0.0.1:"+cloudPort});return true;
            case "cloud.expose":step="cloud_public";return await RequireSession().RequestAsync<JsonElement>("cloud_public",new{host=RequireSession().Host},ct);
            case "mount.settings":return RequireSession().ReadMountMappings();
            case "mount.install":step="mount_install";return await RequireSession().RequestAsync<ServerInfo>("mount_install",null,ct);
            case "mount.status":return session?.GetMountStatus()??new(0,"idle","未掛載目錄",0,0,0,0,0);
            case "mount.apply":step="mount_apply";return await RequireSession().ApplyMountsAsync(JsonSerializer.Deserialize<List<MountMapping>>(args.GetProperty("mappings"),JsonDefaults.Options)??[],ct);
            case "ip.apply":
                step="ip_policy";var changed=await RequireSession().ApplyIpPolicyAsync(JsonSerializer.Deserialize<IpPolicySettings>(args,JsonDefaults.Options)??throw new ArgumentException("缺少設定。"),ct);
                try{if(LocalSecrets.IsUnlocked&&LocalSecrets.Load() is {} saved)LocalSecrets.Save(saved with{Host=RequireSession().Host,Port=RequireSession().Port});}
                catch{Log("connection","伺服器設定已應用，但本機儲存的登入資料未能更新。");}
                return changed;
            case "socks.probe":
                return await RequireSession().ProbeSocksEgressAsync(Read("proxyUrl"),ct);
            case "socks.status":
                return await RequireSession().RequestAsync<SocksEgressStatus>("socks_status",null,ct);
            case "socks.enable":
                if(!Flag("accepted"))throw new InvalidOperationException("請先確認整機代理的影響。");
                return await RequireSession().SetSocksEgressAsync(Read("proxyUrl"),ct);
            case "socks.disable":
                return await RequireSession().SetSocksEgressAsync(null,ct);
            default:throw new InvalidOperationException("不支援的客戶端操作："+op);
        }
    }
    private SshSession RequireGatewaySession() => connectionPending == 0 && session?.Server != null
        ? session : throw new InvalidOperationException("請先連接伺服器。");
    private SshSession RequireSession()=>connectionPending==0&&session?.IsConnected==true&&session.Server!=null?session:throw new InvalidOperationException("SSH 已斷開，請重新連線。");
    private DesktopKeyAccess KeyAccess()=>keyAccess??=new DesktopKeyAccess(directory,LocalSecrets.Protect,LocalSecrets.Unprotect,
        diagnostics:services);
    private void Log(string operation,string line)
    {
        string diagnostic=operation=="folder"?"directory_list":string.IsNullOrEmpty(operation)?"connection":operation;
        if(traces.TryGetValue(diagnostic,out var trace)||
            (diagnostic=="connection"&&traces.TryGetValue("gateway_reconnect",out trace)))trace.Observe(line);
        _=EmitAsync(new{type="logs",lines=new[]{new{step=operation,line}}});
    }
    private async Task PublishStatusAsync()
    {
        using var timer=new PeriodicTimer(TimeSpan.FromSeconds(1));try{while(await timer.WaitForNextTickAsync(lifetime.Token))await EmitAsync(new{type="status",busy=activeMutations>0||connectionPending!=0,mount=session?.GetMountStatus()??new(0,"idle","未掛載目錄",0,0,0,0,0),host=hostControl?.Status,desktopKeyRunning=keyAccess?.Running==true,connection=keyAccess?.ConnectionStatus,apiMode=preferences.PrefersApiAccess,startedAt=keyAccess?.StartedAt});}catch(OperationCanceledException){}
    }
    private async Task EmitAsync(object value){await writer.WaitAsync();try{await Console.Out.WriteLineAsync(JsonSerializer.Serialize(value,Json));await Console.Out.FlushAsync();}finally{writer.Release();}}
    public void Dispose()
    {
        keyAccess?.Dispose();
        if(keyDeviceTunnel!=null)keyDeviceTunnel.DisposeAsync().AsTask().GetAwaiter().GetResult();
        lifetime.Cancel();hostControl?.Dispose();session?.Dispose();services.Dispose();lifetime.Dispose();
    }
}
