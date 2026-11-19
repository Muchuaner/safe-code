using Microsoft.Win32;
using SafeCode.Core;
using System.Diagnostics;
using System.IO;
using System.Security;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text.Json;

namespace SafeCode.Desktop;

internal sealed record ClaudeDesktopStatus(bool Active,bool Managed,bool Configured,bool Installed,
    string State,string Message,string Endpoint,string Model,string Display,bool CredentialPresent,bool Takeover,
    DateTimeOffset? StartedAt,DateTimeOffset? StoppedAt,IntegrationUsage Usage,string UsageSource)
{
    public long DurationSeconds=>(long)Math.Max(0,((StoppedAt??DateTimeOffset.UtcNow)-(StartedAt??DateTimeOffset.UtcNow)).TotalSeconds);
}

/// <summary>Desktop 只连本机回环 SSH 网关；策略中没有模型 API key。</summary>
internal sealed class ClaudeDesktopBridge : IDisposable
{
    private readonly string directory,statePath,backupPath,preferencePath,registryPath;
    private readonly SemaphoreSlim gate=new(1,1);
    private readonly object stateLock=new();
    private DesktopState? state;
    private Dictionary<string,PolicyValue> policy=[];
    private DesktopVpsGateway? gateway;
    private readonly ClaudePermissionSettings permissionSettings;
    private bool loaded,installed;
    private string? lastError;
    private DateTime lastPolicyRead;
    public ClaudeDesktopBridge(string dataDirectory,string? registryPath=null,string? permissionSettingsPath=null)
    {this.registryPath=registryPath??ClaudeDesktopPolicy.RegistryPath;directory=Path.Combine(dataDirectory,"ClaudeDesktop");statePath=Path.Combine(directory,"session.json");backupPath=Path.Combine(directory,"backup.dpapi");preferencePath=Path.Combine(directory,"enabled.json");permissionSettings=new ClaudePermissionSettings(permissionSettingsPath??Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),".claude","settings.json"),Path.Combine(directory,"permissions-backup.json"));}
    public GatewayPreference Preference=>GatewayPreference.Load(preferencePath);
    public bool Running=>gateway!=null;
    public ClaudeDesktopStatus Status
    {
        get
        {
            EnsureLoaded();
            bool configured=ClaudeDesktopPolicy.IsGateway(policy);
            bool managed=state?.Active==true||gateway!=null||File.Exists(backupPath);
            bool active=state?.Active==true&&gateway?.Connected==true&&MatchesCurrentPolicy();
            string message=lastError??(active?"SSH 网关已连接，可在 Claude Desktop 中开始对话":managed?"VPS 通道不可用或待清理；不会回退到外部 API":configured?"检测到旧网关配置；启用后将替换为 VPS 桥接，旧直连配置仅加密归档":state?.Note??"尚未启用 VPS 桥接");
            return new(active,managed,configured,installed,lastError!=null||managed&&!active?"attention":active?"active":"unconfigured",message,
                gateway?.Endpoint??"",state?.Model??"opus",state?.Display??"Opus",active,false,
                state?.StartedAt,state?.StoppedAt,gateway?.Usage??state?.Usage??IntegrationUsage.Unavailable("尚未启用 VPS 桥接"),"VPS 原版 Claude Code assistant usage");
        }
    }
    public Task<ClaudeDesktopStatus> TickAsync(CancellationToken ct=default)=>RefreshAsync(ct);
    public async Task<ClaudeDesktopStatus> RefreshAsync(CancellationToken ct=default)
    {
        EnsureLoaded();if(!await gate.WaitAsync(0,ct))return Status;
        try
        {
            await ReadPolicyAsync(true,ct);
            if(state?.Active==true&&gateway!=null)
            {
                if(!MatchesCurrentPolicy())
                {
                    await gateway.StopAsync(ct);gateway.Dispose();gateway=null;state.Active=false;state.StoppedAt=DateTimeOffset.UtcNow;
                    (Preference with{Enabled=false}).Save(preferencePath);
                    lastError="Desktop 策略已被外部修改，VPS 桥接已关闭；未覆盖外部修改。";
                }
                else
                {
                    bool ok=await gateway.RefreshAsync(ct);state.Usage=gateway.Usage;
                    lastError=ok?null:"SSH 网关不可用；请检查服务器连接并停止后重新启用，不会直连其他 API。";
                }
                PersistState();
            }
            return Status;
        }
        finally{gate.Release();}
    }
    private bool MatchesCurrentPolicy()=>gateway!=null&&ClaudeDesktopPolicy.Endpoint(policy)==gateway.Endpoint
        &&policy.TryGetValue("inferenceGatewayApiKey",out var key)&&key.Text==gateway.LocalToken;

    public async Task<ClaudeDesktopStatus> StartAsync(SshSession target,bool accepted,CancellationToken ct=default)
    {
        if(!accepted)throw new InvalidOperationException("请先确认通过 VPS 上原版 Claude Code 接入。");
        await gate.WaitAsync(ct);
        try
        {
            EnsureLoaded();if(gateway!=null||state?.Active==true)throw new InvalidOperationException("请先停止现有 Desktop 桥接。");
            try
            {
                using var writable=Registry.CurrentUser.CreateSubKey(registryPath,true)??throw new IOException("无法打开 Claude Desktop 策略。");
            }
            catch(Exception error)when(error is UnauthorizedAccessException or SecurityException){throw new InvalidOperationException("Windows 限制了官方策略写入，请以管理员身份启动 Safe Code 后再启用 Desktop 桥接；尚未启动 VPS 网关。");}
            ProtectDirectory(directory);
            await ReadPolicyAsync(true,ct);
            Backup? existing=File.Exists(backupPath)?ReadBackup()??throw new IOException("无法读取上次加密备份；未修改 Desktop 策略。"):null;
            if(existing!=null&&(existing.Version!=2||ClaudeDesktopPolicy.RouteWasReplaced(policy,existing.Written)))
                throw new IOException("Desktop 配置已被其他程序更改，请先停用桥接，再确认是否重新启用；未覆盖外部配置。");
            string? savedToken=existing?.Written.GetValueOrDefault("inferenceGatewayApiKey")?.Text;
            uint savedPort=0;
            if(existing!=null&&Uri.TryCreate(existing.Written.GetValueOrDefault("inferenceGatewayBaseUrl")?.Text,UriKind.Absolute,out var endpoint)&&endpoint.Host=="127.0.0.1"&&endpoint.Port>=1024)
                savedPort=(uint)endpoint.Port;
            if(existing!=null&&(savedToken==null||savedPort==0))throw new IOException("已保存的本机网关配置不完整，请先停用再启用。");
            string clientId=state?.ClientId??Guid.NewGuid().ToString("N");
            var proposed=new DesktopVpsGateway(target,clientId,savedToken,savedPort);
            Backup? attempted=null;
            var beforePolicy=new Dictionary<string,PolicyValue>(policy);
            var beforePreference=Preference;
            try
            {
                await proposed.StartAsync(ct);
                var models=await proposed.ModelsAsync(ct);
                string[] effortOrder=["low","medium","high","xhigh","max"];
                string? previousModel=state?.Model?.Replace("[1m]","",StringComparison.Ordinal);
                var chatModels=models.Where(m=>!m.AutoModeClassifier).ToArray();
                var selected=chatModels.FirstOrDefault(m=>previousModel!=null&&(m.Id==previousModel||m.Family==previousModel))
                    ??chatModels.FirstOrDefault(m=>m.Family=="opus")??chatModels.FirstOrDefault()
                    ??throw new InvalidOperationException("VPS 原版 Claude Code 未返回可用模型，请检查远端配置与授权。");
                var modelEntries=chatModels.OrderBy(m=>m.Id==selected.Id?0:1).Select(m=>
                {
                    var entry=new Dictionary<string,object>
                    {
                        ["name"]=m.Id,["labelOverride"]=m.DisplayName,
                        ["maxEffort"]=effortOrder.LastOrDefault(e=>m.SupportedEffortLevels.Contains(e))??"low",
                        ["supports1m"]=m.Supports1m
                    };
                    if(m.Family!=null){entry["anthropicFamilyTier"]=m.Family;entry["isFamilyDefault"]=true;}
                    return entry;
                }).ToArray();
                var original=ClaudeDesktopPolicy.ManagedFields.ToDictionary(f=>f,f=>policy.TryGetValue(f,out var v)?v:null);
                if(existing==null&&ClaudeDesktopPolicy.IsGateway(policy))
                {
                    // 归档误接的外部路由；停止不能悄悄恢复 DeepSeek 直连。
                    WriteEncrypted(Path.Combine(directory,"legacy-direct-"+Guid.NewGuid().ToString("N")+".dpapi"),original);
                    original=ClaudeDesktopPolicy.ManagedFields.ToDictionary(f=>f,f=>(PolicyValue?)null);
                }
                if(existing!=null)original=existing.Original;
                var written=new Dictionary<string,PolicyValue>
                {
                    ["inferenceProvider"]=PolicyValue.FromString("gateway"),["inferenceGatewayBaseUrl"]=PolicyValue.FromString(proposed.Endpoint),
                    ["inferenceGatewayAuthScheme"]=PolicyValue.FromString("bearer"),["inferenceCredentialKind"]=PolicyValue.FromString("static"),
                    ["inferenceGatewayApiKey"]=PolicyValue.FromString(proposed.LocalToken),
                    ["inferenceModels"]=PolicyValue.FromString(JsonSerializer.Serialize(modelEntries)),
                    ["defaultModelEffort"]=PolicyValue.FromString("high"),["modelDiscoveryEnabled"]=PolicyValue.FromDWord(0),
                    ["deploymentMode"]=PolicyValue.FromString("3p"),["disableDeploymentModeChooser"]=PolicyValue.FromDWord(1),
                    ["endUserAttribution"]=PolicyValue.FromDWord(0),["deploymentDisplayName"]=PolicyValue.FromString("SafeCode"),
                    ["alwaysStartWithDefaultModel"]=PolicyValue.FromDWord(1),["autoModeEnabled"]=PolicyValue.FromDWord(1),
                    ["inferenceModelPricingEnabled"]=PolicyValue.FromDWord(0),["chatTabEnabled"]=PolicyValue.FromDWord(1)
                };
                if(models.FirstOrDefault(m=>m.AutoModeClassifier) is {} classifier)
                    written["autoModeClassifierModel"]=PolicyValue.FromString(classifier.Id);
                var backup=new Backup{Version=2,Original=original,Written=written};attempted=backup;WriteBackup(backup);
                permissionSettings.Enable();WritePolicy(written);
                state=new DesktopState{Version=2,Active=true,ClientId=clientId,Model=selected.Id,Display=selected.DisplayName,StartedAt=DateTimeOffset.UtcNow};
                GatewayPreference.For(target).Save(preferencePath);
                lastError=null;PersistState();await ReadPolicyAsync(true,ct);gateway=proposed;return Status;
            }
            catch
            {
                using var timeout=new CancellationTokenSource(TimeSpan.FromSeconds(15));await proposed.StopAsync(timeout.Token);proposed.Dispose();
                if(state!=null){state.Active=false;state.StoppedAt=DateTimeOffset.UtcNow;PersistState();}
                if(attempted!=null)
                {
                    var restore=ClaudeDesktopPolicy.Restore(ReadPolicy(),attempted.Written,ClaudeDesktopPolicy.ManagedFields.ToDictionary(f=>f,f=>beforePolicy.GetValueOrDefault(f)));
                    foreach(var pair in restore)WriteOne(pair.Key,pair.Value);
                    if(existing!=null)WriteBackup(existing);else{permissionSettings.Restore();File.Delete(backupPath);}
                    beforePreference.Save(preferencePath);
                    await ReadPolicyAsync(true,CancellationToken.None);
                }
                throw;
            }
        }
        finally{gate.Release();}
    }
    public async Task<ClaudeDesktopStatus> StopAsync(CancellationToken ct=default,bool suspend=false)
    {
        await gate.WaitAsync(ct);
        try
        {
            EnsureLoaded();string? warning=null;
            if(!suspend)(Preference with{Enabled=false}).Save(preferencePath);
            if(gateway!=null)
            {
                state!.Usage=gateway.Usage;warning=await gateway.StopAsync(ct);
                if(warning==null){gateway.Dispose();gateway=null;}
            }
            if(!suspend&&File.Exists(backupPath))
            {
                var backup=ReadBackup()??throw new IOException("无法解密备份，已关闭本机通道但保留待恢复配置。");
                if(backup.Version==2)RestorePolicy(backup);
                else File.Copy(backupPath,Path.Combine(directory,"legacy-v1-"+Guid.NewGuid().ToString("N")+".dpapi"));
                File.Delete(backupPath);
            }
            if(!suspend)permissionSettings.Restore();
            if(state!=null){state.Active=false;state.StoppedAt=DateTimeOffset.UtcNow;state.Note=suspend?"已保存启用选择，下次连接原服务器后自动恢复。":"VPS 桥接已停止；本工具策略已移除/恢复，旧外部直连配置不会自动恢复。请重启 Claude Desktop。";PersistState();}
            lastError=warning;await ReadPolicyAsync(true,ct);return Status;
        }
        finally{gate.Release();}
    }
    private void RestorePolicy(Backup backup,bool partialWrite=false)
    {
        var current=ReadPolicy();
        if(!partialWrite&&ClaudeDesktopPolicy.RouteWasReplaced(current,backup.Written))return;
        var operations=ClaudeDesktopPolicy.Restore(current,backup.Written,backup.Original);
        foreach(var pair in operations)WriteOne(pair.Key,pair.Value);
    }
    private void EnsureLoaded()
    {
        if(loaded)return;loaded=true;
        try{if(File.Exists(statePath))state=JsonSerializer.Deserialize<DesktopState>(File.ReadAllText(statePath));}
        catch(Exception error)when(error is IOException or UnauthorizedAccessException or JsonException){lastError="本机桥接状态读取失败。";}
        if(state?.Active==true){state.Active=false;state.StoppedAt=DateTimeOffset.UtcNow;state.Note=Preference.Enabled?"等待恢复已启用的 VPS 桥接。":"上次进程的通道已关闭；请重新启用 VPS 桥接。";}
        policy=ReadPolicy();installed=DetectInstalled();
    }
    private void PersistState()
    {
        if(state==null)return;ProtectDirectory(directory);string temp=statePath+".new";
        File.WriteAllText(temp,JsonSerializer.Serialize(state));ProtectFile(temp);File.Move(temp,statePath,true);
    }
    private void WriteEncrypted(string path,object value)
    {
        byte[] plain=JsonSerializer.SerializeToUtf8Bytes(value);
        try{File.WriteAllBytes(path,ProtectedData.Protect(plain,null,DataProtectionScope.CurrentUser));ProtectFile(path);}
        finally{CryptographicOperations.ZeroMemory(plain);}
    }
    public void Dispose(){gateway?.Dispose();gateway=null;}
    public string OpenClient()
    {
        if(!DetectInstalled())throw new InvalidOperationException("未检测到 Claude Desktop，请先安装官方客户端，或从开始菜单启动。");
        try
        {
            // Use the registered app entry so the MSIX process reads its selected profile.
            // A claude:// deep link can start a separate consumer-mode process.
            var launch=new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows),"explorer.exe"))
            {UseShellExecute=false};
            launch.ArgumentList.Add(@"shell:AppsFolder\Claude_pzs8sxrjxfjjc!Claude");
            using var request=Process.Start(launch)??throw new IOException("无法启动 Windows 应用入口。");
            return "已请求打开官方 Claude Desktop。";
        }
        catch(Exception error) when(error is System.ComponentModel.Win32Exception or IOException)
        {
            throw new InvalidOperationException("未能打开官方 Claude Desktop，请从开始菜单启动，或先安装官方客户端。",error);
        }
    }
    private async Task ReadPolicyAsync(bool force,CancellationToken ct)
    {
        if(!force&&DateTime.UtcNow-lastPolicyRead<TimeSpan.FromSeconds(5))return;
        var (read,isInstalled)=await Task.Run(()=>(ReadPolicy(),DetectInstalled()),ct);
        lock(stateLock){policy=read;lastPolicyRead=DateTime.UtcNow;installed=isInstalled;}
    }
    private Dictionary<string,PolicyValue> ReadPolicy()
    {
        var result=new Dictionary<string,PolicyValue>(StringComparer.OrdinalIgnoreCase);
        try
        {
            using var key=Registry.CurrentUser.OpenSubKey(registryPath,false);
            if(key==null)return result;
            foreach(string name in key.GetValueNames())
            {
                var kind=key.GetValueKind(name);object? value=key.GetValue(name,null,RegistryValueOptions.DoNotExpandEnvironmentNames);
                if(kind==RegistryValueKind.String&&value is string text)result[name]=PolicyValue.FromString(text);
                else if(kind==RegistryValueKind.DWord&&value is int number)result[name]=PolicyValue.FromDWord(number);
            }
        }
        catch(UnauthorizedAccessException){}
        catch(SecurityException){}
        return result;
    }
    private void WritePolicy(IReadOnlyDictionary<string,PolicyValue> values)
    {
        foreach(var pair in values)WriteOne(pair.Key,pair.Value);
    }
    private void WriteOne(string name,PolicyValue? value)
    {
        using var key=Registry.CurrentUser.CreateSubKey(registryPath,true)??throw new IOException("无法打开 Claude Desktop 本机策略。");
        if(value==null){if(key.GetValue(name)!=null)key.DeleteValue(name,false);return;}
        object? current=key.GetValue(name);
        if(value.Kind==PolicyValueKind.DWord&&current is int number&&number==value.Number)return;
        if(value.Kind!=PolicyValueKind.DWord&&current is string text&&text==value.Text)return;
        if(value.Kind==PolicyValueKind.DWord)key.SetValue(name,value.Number??0,RegistryValueKind.DWord);
        else key.SetValue(name,value.Text??"",RegistryValueKind.String);
    }
    private static bool DetectInstalled()
    {
        try
        {
            using var key=Registry.CurrentUser.OpenSubKey(@"Software\Classes\claude\shell\open\command",false)
                ??Registry.LocalMachine.OpenSubKey(@"Software\Classes\claude\shell\open\command",false);
            if(key?.GetValue(null) is string command&&command.Length>0)return true;
        }
        catch(UnauthorizedAccessException){}
        catch(SecurityException){}
        try
        {
            string packages=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Packages");
            return Directory.Exists(packages)&&Directory.EnumerateDirectories(packages,"Claude_*").Any();
        }
        catch(IOException){return false;}
        catch(UnauthorizedAccessException){return false;}
    }
    private void WriteBackup(Backup backup)
    {
        Directory.CreateDirectory(directory);ProtectDirectory(directory);
        byte[] plain=JsonSerializer.SerializeToUtf8Bytes(backup);
        byte[] sealedBytes=ProtectedData.Protect(plain,null,DataProtectionScope.CurrentUser);
        string temp=backupPath+".new";File.WriteAllBytes(temp,sealedBytes);ProtectFile(temp);File.Move(temp,backupPath,true);
        CryptographicOperations.ZeroMemory(plain);
    }
    private Backup? ReadBackup()
    {
        try
        {
            if(!File.Exists(backupPath))return null;
            byte[] sealedBytes=File.ReadAllBytes(backupPath);byte[] plain=ProtectedData.Unprotect(sealedBytes,null,DataProtectionScope.CurrentUser);
            try{return JsonSerializer.Deserialize<Backup>(plain);}finally{CryptographicOperations.ZeroMemory(plain);}
        }
        catch(CryptographicException){lastError="加密备份无法解密，未改写本机配置。";return null;}
        catch(IOException){lastError="加密备份读取失败，未改写本机配置。";return null;}
    }
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
    private sealed class DesktopState
    {
        public int Version{get;set;}
        public bool Active{get;set;}
        public string? ClientId{get;set;}
        public string Model{get;set;}="opus";
        public string Display{get;set;}="Opus";
        public DateTimeOffset StartedAt{get;set;}
        public DateTimeOffset? StoppedAt{get;set;}
        public string? Note{get;set;}
        public IntegrationUsage Usage{get;set;}=IntegrationUsage.Unavailable("等待 VPS 原版 Claude Code usage");
    }
    private sealed class Backup
    {
        public int Version{get;set;}
        public Dictionary<string,PolicyValue> Written{get;set;}=new();
        public Dictionary<string,PolicyValue?> Original{get;set;}=new();
    }
}
