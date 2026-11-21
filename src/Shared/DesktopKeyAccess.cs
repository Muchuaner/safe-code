using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Net.Security;
using System.Security;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Runtime.Versioning;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Microsoft.Win32;
using SafeCode.Core;
using Ticket = SafeCode.Core.PublicGatewayTicket;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Hosting;

namespace SafeCode.Desktop;

// Claude receives a device-local bearer. Only SafeCode holds the remote bearer and TLS pin.
internal sealed class DesktopKeyAccess(string dataDirectory, Func<byte[],byte[]> protect,
    Func<byte[],byte[]> unprotect, string? profileDirectory=null,ClientServices? diagnostics=null,
    string? windowsPolicyPath=null,Func<CancellationToken,Task>? beforeMessages=null) : IDisposable
{
    internal sealed record DeviceBinding(string DeviceId,string DeviceProof,string Alias,int Port,string PublicKey);
    internal sealed record DeviceSetup(Ticket Remote,bool Replace,bool UseSaved,DeviceBinding? Existing,SshSession? HostSession=null);
    private sealed record Saved(Ticket Remote,string Local,int Port,string Profile,string Previous,JsonNode? Hybrid,bool CreatedMeta=false,
        Dictionary<string,PolicyValue>? WindowsPolicy=null,DeviceBinding? Device=null);
    private sealed record Relay(WebApplication App,HttpClient Client,int Port,Func<bool>? HostReady) : IAsyncDisposable
    {
        public async ValueTask DisposeAsync()
        {
            try{await App.StopAsync(TimeSpan.FromSeconds(3)).ConfigureAwait(false);}
            catch(OperationCanceledException){}
            finally
            {
                try{await App.DisposeAsync().ConfigureAwait(false);}
                finally{Client.Dispose();}
            }
        }
    }
    private readonly SemaphoreSlim gate=new(1);
    private string StatePath=>Path.Combine(dataDirectory,"desktop-key.state");
    private Relay? relay;
    private static readonly JsonSerializerOptions Json=new(JsonSerializerDefaults.Web);
    internal bool Running=>relay!=null;
    internal bool ReusesHostControl=>relay?.HostReady!=null;
    internal bool Configured=>File.Exists(StatePath);
    private string Library()
    {
        return profileDirectory??ProfileLibrary();
    }
    internal static string ProfileLibrary()
    {
        if(OperatingSystem.IsMacOS())return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),"Library/Application Support/Claude-3p/configLibrary");
        var local=Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string package=Path.Combine(local,"Packages","Claude_pzs8sxrjxfjjc");
        string packaged=Path.Combine(package,"LocalCache","Local","Claude-3p","configLibrary");
        string unpackaged=Path.Combine(local,"Claude-3p","configLibrary");
        if(File.Exists(Path.Combine(packaged,"_meta.json")))return packaged;
        if(File.Exists(Path.Combine(unpackaged,"_meta.json")))return unpackaged;
        return Directory.Exists(packaged)||Directory.Exists(package)&&!Directory.Exists(unpackaged)?packaged:unpackaged;
    }
    private static HttpClient Remote(Ticket t)
    {
        var pin=Convert.FromHexString(t.Pin);
        var handler=new HttpClientHandler{AllowAutoRedirect=false,UseCookies=false};
        handler.ServerCertificateCustomValidationCallback=(_,cert,_,_)=>MatchesPin(pin,cert);
        var c=new HttpClient(handler){BaseAddress=new Uri(t.Url),Timeout=TimeSpan.FromMinutes(11),MaxResponseContentBufferSize=1024*1024};
        c.DefaultRequestHeaders.Authorization=new AuthenticationHeaderValue("Bearer",t.Key);
        c.DefaultRequestHeaders.Add("X-SafeCode-Platform",OperatingSystem.IsMacOS()?"macos":"windows");
        return c;
    }

    internal static bool MatchesPin(ReadOnlySpan<byte> pin,System.Security.Cryptography.X509Certificates.X509Certificate2? certificate)
        =>certificate!=null&&DateTime.UtcNow>=certificate.NotBefore.ToUniversalTime()&&
            DateTime.UtcNow<=certificate.NotAfter.ToUniversalTime()&&
            CryptographicOperations.FixedTimeEquals(pin,certificate.GetCertHash(HashAlgorithmName.SHA256));

    internal static async Task<JsonElement> DeviceControlAsync(Ticket ticket,object request,CancellationToken ct)
    {
        using var client=Remote(ticket);
        client.Timeout=TimeSpan.FromSeconds(15);
        using var body=new ByteArrayContent(JsonSerializer.SerializeToUtf8Bytes(request,Json));
        body.Headers.ContentType=new MediaTypeHeaderValue("application/json");
        using var response=await client.PostAsync("/v1/device-control",body,ct).ConfigureAwait(false);
        if(!response.IsSuccessStatusCode)
            throw new InvalidOperationException("网关未能完成本机远程控制操作（HTTP "+(int)response.StatusCode+"）。请检查该 Key 的设备授权与 FRP 状态。");
        using var document=JsonDocument.Parse(await response.Content.ReadAsByteArrayAsync(ct).ConfigureAwait(false));
        return document.RootElement.Clone();
    }

    internal async Task<DeviceSetup> VerifyDeviceGatewayAsync(string code,bool replace,bool useSaved,SshSession? ssh,CancellationToken ct)
    {
        await gate.WaitAsync(ct);
        try
        {
            var saved=Configured?Load():null;
            if(useSaved&&saved==null)throw new InvalidOperationException("没有保存的接入 Key。");
            if(saved!=null&&!replace&&!useSaved)throw new InvalidOperationException("请使用「更换 Key」。");
            var ticket=useSaved?saved!.Remote:Ticket.Parse(code.Trim());
            using var client=Remote(ticket);
            using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);
            deadline.CancelAfter(TimeSpan.FromSeconds(20));
            using var response=await VerifyRemoteAsync(client,deadline.Token);
            if(response.StatusCode==HttpStatusCode.Unauthorized)throw new InvalidOperationException("Key 无效或已撤销。");
            response.EnsureSuccessStatusCode();
            if(ssh!=null&&await ssh.CanReuseHostControlForKeyAsync(ticket,ct))
                return new(ticket,replace,useSaved,null,ssh);
            var capabilities=await DeviceControlAsync(ticket,new{action="capability"},deadline.Token);
            if(!capabilities.TryGetProperty("httpsTunnel",out var tunnel)||tunnel.ValueKind!=JsonValueKind.True)
                throw new InvalidOperationException("此网关尚未支持通过 HTTPS 建立本机远程控制。");
            return new(ticket,replace,useSaved,saved?.Remote==ticket&&saved.Device is { } previous&&
                !previous.DeviceId.StartsWith("ssh-",StringComparison.Ordinal)?previous:null);
        }
        finally{gate.Release();}
    }

    private async Task Forward(HttpContext ctx,HttpClient client,string token,Func<bool>? hostReady)
    {
        string path=ctx.Request.Path.Value??"";
        if(!(ctx.Request.Method=="GET"&&path=="/v1/models"||
             ctx.Request.Method=="POST"&&path is ("/v1/messages" or "/v1/messages/count_tokens")))
        {ctx.Response.StatusCode=404;return;}
        string auth=ctx.Request.Headers.Authorization.ToString();
        string key=auth.StartsWith("Bearer ",StringComparison.Ordinal)?auth[7..]:ctx.Request.Headers["x-api-key"].ToString();
        if(!CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(key),Encoding.UTF8.GetBytes(token)))
        {ctx.Response.StatusCode=401;return;}
        // Old gateways do not expose this namespace, so downgrades cannot dispatch a native CLI turn.
        using var req=new HttpRequestMessage(new HttpMethod(ctx.Request.Method),"/v1/cloudcli"+path[3..]+ctx.Request.QueryString.Value);
        if(ctx.Request.Method=="POST")
        {
            // The remote Messages endpoint requires Content-Length, including for a chunked local request.
            using var body=new MemoryStream();
            await ctx.Request.Body.CopyToAsync(body,ctx.RequestAborted);
            if(body.Length>8*1024*1024){ctx.Response.StatusCode=413;return;}
            req.Content=new ByteArrayContent(body.ToArray());
            req.Content.Headers.ContentType=new MediaTypeHeaderValue("application/json");
        }
        foreach(var h in new[]{"anthropic-version","anthropic-beta","x-claude-code-session-id"})
            if(ctx.Request.Headers.TryGetValue(h,out var v))req.Headers.TryAddWithoutValidation(h,v.ToArray());
        if(path=="/v1/messages"&&hostReady?.Invoke()==false)
        {
            ctx.Response.StatusCode=503;
            await ctx.Response.WriteAsJsonAsync(new {type="error",error=new {type="api_error",
                message="已授权的本机远程控制暂不可用。请在 SafeCode 恢复控制通道后再试。"}},ctx.RequestAborted);
            return;
        }
        var trace=diagnostics?.BeginDiagnostic("desktop_key_request");
        var gateway=trace==null?null:new GatewayDiagnostic(path switch
        {
            "/v1/models"=>"models", "/v1/messages/count_tokens"=>"count_tokens", _=>"messages"
        });
        long bytes=0;
        if(gateway!=null)req.Headers.Add("X-SafeCode-Diagnostics","1");
        Exception? failure=null;
        try
        {
            if(path=="/v1/messages"&&beforeMessages!=null)await beforeMessages(ctx.RequestAborted);
            using var res=await client.SendAsync(req,HttpCompletionOption.ResponseHeadersRead,ctx.RequestAborted);
            if(gateway!=null)
            {
                res.Headers.TryGetValues("X-SafeCode-Gateway-Diagnostic",out var headers);
                gateway.Response((int)res.StatusCode,headers);
                if(!res.IsSuccessStatusCode)failure=new HttpRequestException(null,null,res.StatusCode);
            }
            ctx.Response.StatusCode=(int)res.StatusCode;
            ctx.Response.ContentType=res.Content.Headers.ContentType?.ToString()??"application/json";
            if(gateway!=null&&res.Content.Headers.ContentType?.MediaType=="text/event-stream")
            {
                await using var input=await res.Content.ReadAsStreamAsync(ctx.RequestAborted);
                var buffer=new byte[16384];
                int count;
                while((count=await input.ReadAsync(buffer,ctx.RequestAborted))>0)
                {
                    bytes+=count;
                    gateway.ObserveSse(buffer.AsSpan(0,count));
                    await ctx.Response.Body.WriteAsync(buffer.AsMemory(0,count),ctx.RequestAborted);
                    await ctx.Response.Body.FlushAsync(ctx.RequestAborted);
                }
                if(gateway.SawError)failure=new HttpRequestException(null,null,(HttpStatusCode)(gateway.FailureStatus??502));
                else if(!gateway.Ended)failure=new HttpRequestException(null,null,HttpStatusCode.BadGateway);
            }
            else
            {
                bytes=res.Content.Headers.ContentLength??0;
                await res.Content.CopyToAsync(ctx.Response.Body,ctx.RequestAborted);
            }
        }
        catch(Exception e) when(e is HttpRequestException or OperationCanceledException or IOException)
        {
            failure??=e is OperationCanceledException&&!ctx.RequestAborted.IsCancellationRequested?new TimeoutException():e;
            if(!ctx.Response.HasStarted)ctx.Response.StatusCode=502;else ctx.Abort();
        }
        finally
        {
            if(trace!=null&&(failure!=null||path=="/v1/messages")&&!ctx.RequestAborted.IsCancellationRequested)
            {
                if(failure!=null)trace.Gateway=gateway?.Finish(bytes);
                _=diagnostics!.SendDiagnosticAsync(trace,failure,CancellationToken.None);
            }
        }
    }

    private async Task<Relay> Listen(Ticket t,string local,int port,CancellationToken ct,DeviceBinding? device=null,Func<bool>? hostReady=null)
    {
        var builder=WebApplication.CreateSlimBuilder(new WebApplicationOptions{Args=[],ContentRootPath=AppContext.BaseDirectory});
        builder.Configuration.Sources.Clear();
        builder.Logging.ClearProviders();
        builder.WebHost.ConfigureKestrel(k=>{k.Limits.MaxRequestBodySize=8*1024*1024;k.Limits.MaxConcurrentConnections=32;k.Listen(IPAddress.Loopback,port);});
        var app=builder.Build();var client=Remote(t);
        if(device!=null)
        {
            client.DefaultRequestHeaders.Add("X-SafeCode-Device",device.DeviceId);
            client.DefaultRequestHeaders.Add("X-SafeCode-Device-Proof",device.DeviceProof);
        }
        app.Run(ctx=>Forward(ctx,client,local,hostReady));
        try
        {
            await app.StartAsync(ct);
            var addresses=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses;
            return new(app,client,new Uri(addresses.Single()).Port,hostReady);
        }
        catch{client.Dispose();await app.DisposeAsync();throw;}
    }
    internal static void WriteProfileFile(string path,byte[] data)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        string temp=path+"."+Guid.NewGuid().ToString("N")+".tmp";
        try
        {
            var options=new FileStreamOptions{Mode=FileMode.CreateNew,Access=FileAccess.Write,Share=FileShare.None};
            if(!OperatingSystem.IsWindows())options.UnixCreateMode=UnixFileMode.UserRead|UnixFileMode.UserWrite;
            using(var stream=new FileStream(temp,options)){stream.Write(data);stream.Flush(true);}
            File.Move(temp,path,true);
        }
        finally{DeleteOptionalFile(temp);}
    }
    private static byte[]? ReadOptionalFile(string path)
    {
        try{return File.ReadAllBytes(path);}
        catch(FileNotFoundException){return null;}
        catch(DirectoryNotFoundException){return null;}
    }
    private static void DeleteOptionalFile(string path)
    {
        try{File.Delete(path);}
        catch(DirectoryNotFoundException){}
    }
    private static void RestoreFile(string path,byte[]? original)
    {
        if(original==null)DeleteOptionalFile(path);else WriteProfileFile(path,original);
    }
    internal static JsonObject ReadProfileMeta(string lib)
        =>ParseProfileMeta(ReadOptionalFile(Path.Combine(lib,"_meta.json")));
    private static JsonObject ParseProfileMeta(byte[]? data)
    {
        var meta=data!=null?JsonNode.Parse(data) as JsonObject:new JsonObject{["entries"]=new JsonArray(),["appliedId"]=""};
        if(meta?["entries"] is not JsonArray)throw new IOException("Claude 配置列表无法读取。");
        return meta;
    }
    private Saved Load()=>JsonSerializer.Deserialize<Saved>(unprotect(File.ReadAllBytes(StatePath)),Json)??throw new IOException("无法读取保存的接入配置。");
    private void Save(Saved value)
    {
        byte[] data=JsonSerializer.SerializeToUtf8Bytes(value,Json);
        try{WriteProfileFile(StatePath,protect(data));}
        finally{CryptographicOperations.ZeroMemory(data);}
    }

    private sealed record PolicyChange(PolicyValue? Before,PolicyValue? After);
    private string WindowsPolicyPath=>windowsPolicyPath??ClaudeDesktopPolicy.RegistryPath;
    private const string WindowsPolicyPermissionMessage="Windows 拒绝访问 Claude Desktop 的本机策略。请先安全退出 Safe Code，再用当前 Windows 账户右键 Safe Code，选择「以管理员身份运行」后重试。若此账户没有管理员权限，或重试后仍失败，请联系管理员检查策略权限。";
    private const string WindowsPolicyConflictMessage="Claude Desktop 现有管理策略与 Safe Code API 接入冲突，请先停用原管理配置。";

    [SupportedOSPlatform("windows")]
    private (Dictionary<string,PolicyValue> Owned,Dictionary<string,PolicyChange> Changes) PlanWindowsPolicy(
        JsonObject profile,Saved saved,IReadOnlyDictionary<string,PolicyValue>? previousPolicy=null)
    {
        // The Windows app selects its deployment mode before loading the per-user profile.
        // Wire the supported policy only to the device-local relay URL and bearer.
        var desired=new Dictionary<string,PolicyValue>
        {
            ["deploymentMode"]=PolicyValue.FromString("3p"),
            ["disableDeploymentModeChooser"]=PolicyValue.FromDWord(1),
            ["deploymentDisplayName"]=PolicyValue.FromString("SafeCode"),
            ["inferenceProvider"]=PolicyValue.FromString("gateway"),
            ["inferenceGatewayBaseUrl"]=PolicyValue.FromString("http://127.0.0.1:"+saved.Port),
            ["inferenceGatewayApiKey"]=PolicyValue.FromString(saved.Local),
            ["inferenceGatewayAuthScheme"]=PolicyValue.FromString("bearer"),
            ["inferenceCredentialKind"]=PolicyValue.FromString("static"),
            ["inferenceModels"]=PolicyValue.FromString((profile["inferenceModels"] as JsonArray??throw new IOException("Claude 配置缺少模型列表。")).ToJsonString()),
            ["modelDiscoveryEnabled"]=PolicyValue.FromDWord(0),
            ["autoModeEnabled"]=PolicyValue.FromDWord(1),
            ["chatTabEnabled"]=PolicyValue.FromDWord(1)
        };
        if(profile["claudeCodeSettings"]?["env"]?["CLAUDE_CODE_AUTO_MODE_MODEL"] is JsonValue model&&model.TryGetValue<string>(out var classifier))
            desired["autoModeClassifierModel"]=PolicyValue.FromString(classifier);
        previousPolicy??=saved.WindowsPolicy;
        try
        {
            using var machine=OpenWindowsPolicy(Registry.LocalMachine);
            using var user=OpenWindowsPolicy(Registry.CurrentUser);
            var owned=new Dictionary<string,PolicyValue>(StringComparer.OrdinalIgnoreCase);
            var changes=new Dictionary<string,PolicyChange>(StringComparer.OrdinalIgnoreCase);
            foreach(var (field,value) in desired)
            {
                var machineValue=ReadPolicyValue(machine,field);
                var userValue=ReadPolicyValue(user,field);
                var previous=previousPolicy?.GetValueOrDefault(field);
                bool stillOwned=previous!=null&&MatchesPolicyValue(userValue,previous);
                if((machineValue!=null&&!MatchesPolicyValue(machineValue,value))||
                    (userValue!=null&&!stillOwned&&!MatchesPolicyValue(userValue,value)))
                    throw new InvalidOperationException(WindowsPolicyConflictMessage);
                if(!stillOwned&&(machineValue!=null||userValue!=null))continue;
                owned[field]=value;
                if(!MatchesPolicyValue(userValue,value))changes[field]=new(stillOwned?previous:null,value);
            }
            if(previousPolicy!=null)
                foreach(var (field,value) in previousPolicy)
                    if(!desired.ContainsKey(field)&&MatchesPolicyValue(ReadPolicyValue(user,field),value))
                        changes[field]=new(value,null);
            return (owned,changes);
        }
        catch(Exception error) when(error is SecurityException or UnauthorizedAccessException)
        {
            throw new InvalidOperationException(WindowsPolicyPermissionMessage,error);
        }
    }

    private static bool MatchesPolicyValue(object? current,PolicyValue? value)=>value switch
    {
        null=>current==null,
        {Kind:PolicyValueKind.DWord}=>current is int number&&number==value.Number,
        _=>current is string text&&text==value.Text
    };

    [SupportedOSPlatform("windows")]
    private static object? ReadPolicyValue(RegistryKey? key,string field)=>
        key?.GetValue(field,null,RegistryValueOptions.DoNotExpandEnvironmentNames);

    [SupportedOSPlatform("windows")]
    private RegistryKey? OpenWindowsPolicy(RegistryKey hive,bool writable=false)=>hive.OpenSubKey(
        WindowsPolicyPath,writable?RegistryKeyPermissionCheck.ReadWriteSubTree:RegistryKeyPermissionCheck.ReadSubTree,
        writable?RegistryRights.QueryValues|RegistryRights.SetValue:RegistryRights.QueryValues);

    [SupportedOSPlatform("windows")]
    private RegistryKey OpenOrCreateWindowsPolicy()
    {
        if(OpenWindowsPolicy(Registry.CurrentUser,writable:true) is {} key)return key;
        using var created=Registry.CurrentUser.CreateSubKey(WindowsPolicyPath,RegistryKeyPermissionCheck.ReadSubTree);
        return OpenWindowsPolicy(Registry.CurrentUser,writable:true)??throw new IOException("无法打开 Claude Desktop 本机策略。");
    }

    [SupportedOSPlatform("windows")]
    private Action ApplyWindowsPolicy(IReadOnlyDictionary<string,PolicyChange> changes)
    {
        if(changes.Count==0)return static ()=>{};
        var applied=new List<KeyValuePair<string,PolicyChange>>();
        try
        {
            using var key=OpenOrCreateWindowsPolicy();
            try
            {
                foreach(var change in changes)
                {
                    var (field,value)=change;
                    if(!MatchesPolicyValue(ReadPolicyValue(key,field),value.Before))
                        throw new InvalidOperationException("Claude Desktop 策略已变更，请重试。");
                    WritePolicyValue(key,field,value.After);
                    applied.Add(change);
                }
            }
            catch
            {
                RestorePolicyValues(key,applied);
                throw;
            }
            return ()=>UndoWindowsPolicy(applied);
        }
        catch(Exception error) when(error is SecurityException or UnauthorizedAccessException)
        {
            throw new InvalidOperationException(WindowsPolicyPermissionMessage,error);
        }
    }

    [SupportedOSPlatform("windows")]
    private Action RemoveWindowsPolicy(IReadOnlyDictionary<string,PolicyValue>? fields)
    {
        if(fields is not {Count:>0})return static ()=>{};
        try
        {
            using var read=OpenWindowsPolicy(Registry.CurrentUser);
            var changes=fields.Where(pair=>MatchesPolicyValue(ReadPolicyValue(read,pair.Key),pair.Value))
                .ToDictionary(pair=>pair.Key,pair=>new PolicyChange(pair.Value,null));
            return ApplyWindowsPolicy(changes);
        }
        catch(Exception error) when(error is SecurityException or UnauthorizedAccessException)
        {
            throw new InvalidOperationException(WindowsPolicyPermissionMessage,error);
        }
    }

    [SupportedOSPlatform("windows")]
    private void UndoWindowsPolicy(List<KeyValuePair<string,PolicyChange>> applied)
    {
        try
        {
            using var key=OpenWindowsPolicy(Registry.CurrentUser,writable:true);
            if(key!=null)RestorePolicyValues(key,applied);
        }
        catch(Exception error) when(error is SecurityException or UnauthorizedAccessException)
        {
            throw new InvalidOperationException(WindowsPolicyPermissionMessage,error);
        }
    }

    [SupportedOSPlatform("windows")]
    private static void RestorePolicyValues(RegistryKey key,List<KeyValuePair<string,PolicyChange>> applied)
    {
        for(int i=applied.Count-1;i>=0;i--)
        {
            var (field,value)=applied[i];
            if(MatchesPolicyValue(ReadPolicyValue(key,field),value.After))WritePolicyValue(key,field,value.Before);
        }
    }

    [SupportedOSPlatform("windows")]
    private static void WritePolicyValue(RegistryKey key,string field,PolicyValue? value)
    {
        switch(value)
        {
            case null:
                key.DeleteValue(field,false);
                if(ReadPolicyValue(key,field)!=null)throw new UnauthorizedAccessException("无法删除本机策略字段。");
                break;
            case {Kind:PolicyValueKind.DWord}:
                key.SetValue(field,value.Number!.Value,RegistryValueKind.DWord);
                break;
            default:
                key.SetValue(field,value.Text!,RegistryValueKind.String);
                break;
        }
    }
    private object Status(string? error=null)
    {
        bool disconnected=relay?.HostReady?.Invoke()==false;
        return new{configured=Configured,running=relay!=null&&!disconnected,
            error=error??(disconnected?"已授权的本机远程控制暂不可用。请恢复本机控制或选择「重新启用」。":null)};
    }
    // The classifier accepts permission checks only. Publishing it as a chat model
    // also makes it compete with the real Sonnet family default in Desktop.
    internal static void SetClassifier(JsonObject profile,string classifier)
    {
        if(profile["claudeCodeSettings"] is null)profile["claudeCodeSettings"]=new JsonObject();
        var settings=profile["claudeCodeSettings"]!.AsObject();
        if(settings["env"] is null)settings["env"]=new JsonObject();
        settings["env"]!.AsObject()["CLAUDE_CODE_AUTO_MODE_MODEL"]=classifier;
    }

    private static void MigrateClassifier(string path)
    {
        if(!File.Exists(path))return;
        var profile=JsonNode.Parse(File.ReadAllBytes(path))?.AsObject();
        if(profile?["autoModeClassifierModel"] is not JsonValue legacy ||
            !legacy.TryGetValue<string>(out var classifier) || profile["inferenceModels"] is not JsonArray models)return;
        var matches=models.Where(m=>(string?)m?["name"]==classifier).ToArray();
        if(models.Count==matches.Length)throw new InvalidDataException("此配置缺少可用的聊天模型，请重新启用接入。");
        foreach(var match in matches)models.Remove(match);
        SetClassifier(profile,classifier);
        profile.Remove("autoModeClassifierModel");
        WriteProfileFile(path,JsonSerializer.SerializeToUtf8Bytes(profile));
    }

    internal async Task<object> RestoreAsync(CancellationToken ct,bool interactive=false,bool requireDevice=false)
    {
        await gate.WaitAsync(ct);
        try
        {
            if(relay==null&&Configured)
            {
                var trace=diagnostics?.BeginDiagnostic("desktop_key_restore",background:!interactive);
                Exception? failure=null;
                try
                {
                    var saved=Load();
                    if(requireDevice||saved.Device!=null)return Status("请重新启用本机远程控制，验证设备连接后恢复接入。");
                    string lib=Library();
                    var meta=ReadProfileMeta(lib);
                    if((string?)meta["appliedId"]==saved.Profile)
                    {
                        var path=Path.Combine(lib,saved.Profile+".json");
                        if(ReadOptionalFile(path)==null)
                            return Status("Claude Desktop 的接入配置已被移除。请选择「重新启用」或「更换 Key」。");
                        MigrateClassifier(path);
                        var restored=await Listen(saved.Remote,saved.Local,saved.Port,ct);
                        try
                        {
                            if(OperatingSystem.IsWindows())
                            {
                                var profile=JsonNode.Parse(File.ReadAllBytes(path))?.AsObject()??throw new IOException("无法读取 Claude 接入配置。");
                                var (fields,changes)=PlanWindowsPolicy(profile,saved);
                                var undoPolicy=ApplyWindowsPolicy(changes);
                                try
                                {
                                    if(saved.WindowsPolicy==null||!ClaudeDesktopPolicy.MatchesWritten(fields,saved.WindowsPolicy)||saved.WindowsPolicy.Count!=fields.Count)
                                        Save(saved with{WindowsPolicy=fields});
                                }
                                catch{undoPolicy();throw;}
                            }
                            relay=restored;
                        }
                        catch{await restored.DisposeAsync();throw;}
                    }
                    else if(OperatingSystem.IsWindows())RemoveWindowsPolicy(saved.WindowsPolicy);
                }
                catch(Exception error)
                {
                    failure=error;
                    if(error is OperationCanceledException)throw;
                    return Status(ProfileError(error).Message);
                }
                finally
                {
                    if(trace!=null&&(relay!=null||failure!=null))_=diagnostics!.SendDiagnosticAsync(trace,failure,ct);
                }
            }
            return Status();
        }
        finally{gate.Release();}
    }
    internal async Task<object> EnableAsync(string code,CancellationToken ct,bool replace=false,bool useSaved=false,DeviceSetup? verified=null,DeviceBinding? device=null,Func<bool>? hostReady=null)
    {
        await gate.WaitAsync(ct);
        try
        {
            var previous=Configured?Load():null;
            if(useSaved&&previous==null)throw new InvalidOperationException("没有保存的接入 Key，请输入新的 Key。");
            if(previous!=null&&!replace&&!useSaved)throw new InvalidOperationException("请使用「更换 Key」切换已保存的接入。");
            var t=verified?.Remote??(useSaved?previous!.Remote:Ticket.Parse(code.Trim()));
            bool controlReady=verified?.HostSession==null?device!=null:device!=null&&hostReady?.Invoke()==true;
            if(verified!=null&&(!controlReady||replace!=verified.Replace||useSaved!=verified.UseSaved||
               useSaved&&t!=previous!.Remote))
                throw new InvalidOperationException("接入配置已变更，请重新验证设备。");
            using var client=Remote(t);using var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeout.CancelAfter(TimeSpan.FromSeconds(45));
            using var result=await VerifyRemoteAsync(client, timeout.Token);
            if(result.StatusCode==HttpStatusCode.Unauthorized)throw new InvalidOperationException("Key 无效或已撤销。");
            result.EnsureSuccessStatusCode();
            var catalog=JsonNode.Parse(await result.Content.ReadAsStringAsync(ct))?["data"]?.AsArray()??throw new IOException("模型列表无效。");
            var models=new JsonArray();string? classifier=null;
            foreach(var m in catalog.OrderByDescending(m=>(string?)m?["anthropic_family_tier"]=="opus"))
            {
                string? id=(string?)m?["id"];if(id==null||!Regex.IsMatch(id,@"\Aclaude-[a-z0-9-]+\z"))continue;
                if((bool?)m?["auto_mode_classifier"]==true){classifier=id;continue;}
                var entry=new JsonObject{["name"]=id,["labelOverride"]=(string?)m?["display_name"]??id};
                foreach(var (src,dst) in new[]{("supports_1m","supports1m"),("anthropic_family_tier","anthropicFamilyTier"),("is_family_default","isFamilyDefault")})
                    if(m?[src] is {} value)entry[dst]=value.DeepClone();
                if(m?["supported_effort_levels"] is JsonArray levels && levels.Count>0)entry["maxEffort"]=levels[^1]?.DeepClone();
                models.Add(entry);
            }
            if(models.Count==0)throw new IOException("远端 CLI 未提供可用模型。");
            string lib=Library(),profileId=previous?.Profile??Guid.NewGuid().ToString();
            string metaPath=Path.Combine(lib,"_meta.json"),profilePath=Path.Combine(lib,profileId+".json");
            byte[]? originalMeta=ReadOptionalFile(metaPath),originalProfile=ReadOptionalFile(profilePath);
            var meta=ParseProfileMeta(originalMeta);var entries=meta["entries"]!.AsArray();
            string applied=(string?)meta["appliedId"]??"";
            bool wasApplied=previous!=null&&applied==profileId;
            string local=previous?.Local??Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
            if(hostReady?.Invoke()==false)throw new InvalidOperationException("本机远程控制连接已中断，请重新验证。");
            var next=await Listen(t,local,0,ct,device,hostReady);
            Action? undoPolicy=null;
            bool profileWritten=false,metaWritten=false;
            try
            {
                var s=new Saved(t,local,next.Port,profileId,wasApplied?previous!.Previous:applied,
                    (wasApplied?previous!.Hybrid:meta["hybridPointer"])?.DeepClone(),wasApplied?previous!.CreatedMeta:originalMeta==null,Device:device);
                var profile=new JsonObject{["inferenceProvider"]="gateway",["inferenceGatewayBaseUrl"]="http://127.0.0.1:"+next.Port,
                    ["inferenceGatewayAuthScheme"]="bearer",["inferenceCredentialKind"]="static",["inferenceGatewayApiKey"]=local,
                    ["deploymentMode"]="3p",["disableDeploymentModeChooser"]=true,["deploymentDisplayName"]="SafeCode",
                    ["inferenceModels"]=models,["modelDiscoveryEnabled"]=false,["autoModeEnabled"]=true,["chatTabEnabled"]=true};
                if(classifier!=null)SetClassifier(profile,classifier);
                if(OperatingSystem.IsWindows())
                {
                    var (owned,changes)=PlanWindowsPolicy(profile,s,previous?.WindowsPolicy);
                    undoPolicy=ApplyWindowsPolicy(changes);
                    s=s with{WindowsPolicy=owned};
                }
                WriteProfileFile(profilePath,JsonSerializer.SerializeToUtf8Bytes(profile));
                profileWritten=true;
                if(!entries.Any(entry=>(string?)entry?["id"]==profileId))
                    entries.Add(new JsonObject{["id"]=profileId,["name"]="SafeCode"});
                meta["appliedId"]=profileId;meta.Remove("hybridPointer");
                WriteProfileFile(metaPath,JsonSerializer.SerializeToUtf8Bytes(meta));
                metaWritten=true;
                Save(s);
            }
            catch
            {
                try{undoPolicy?.Invoke();}
                finally
                {
                    try{if(metaWritten)RestoreFile(metaPath,originalMeta);}
                    finally
                    {
                        try{if(profileWritten)RestoreFile(profilePath,originalProfile);}
                        finally{await next.DisposeAsync();}
                    }
                }
                throw;
            }
            var oldRelay=relay;relay=next;
            if(oldRelay!=null)await oldRelay.DisposeAsync();
            return Status();
        }
        catch(Exception error)when(IsProfileAccessError(error)){throw ProfileError(error);}
        finally{gate.Release();}
    }

    private static bool IsProfileAccessError(Exception error)=>error is UnauthorizedAccessException or SecurityException or DirectoryNotFoundException or FileNotFoundException;
    private static Exception ProfileError(Exception error)=>error switch
    {
        UnauthorizedAccessException or SecurityException=>new IOException("无法访问本机 Claude Desktop 或 Safe Code 的配置文件。请使用平时运行 Claude Desktop 的系统账户，并检查该账户的文件权限。",error),
        DirectoryNotFoundException or FileNotFoundException=>new IOException("Claude Desktop 的本机配置位置已变更。请重新打开 Claude Desktop 后再试。",error),
        _=>error
    };

    private static async Task<HttpResponseMessage> VerifyRemoteAsync(HttpClient client, CancellationToken ct)
    {
        try
        {
            var response = await client.GetAsync("/v1/cloudcli/models", ct);
            if (response.StatusCode is HttpStatusCode.NotFound or HttpStatusCode.ServiceUnavailable)
            {
                response.Dispose();
                throw new InvalidOperationException("这个 Key 的 VPS 尚未启用 CloudCLI 网关。请通过 SSH 进入 Safe Code 的公网网关，配置 CloudCLI 并升级 API 服务。");
            }
            return response;
        }
        catch (HttpRequestException error) when (error.HttpRequestError == HttpRequestError.SecureConnectionError)
        {
            throw new InvalidOperationException("无法与网关建立 TLS 连接。请核对公网 HTTPS 地址、转发端口和证书，必要时重新签发 Key。", error);
        }
    }
    internal async Task<object> DisableAsync(CancellationToken ct)
    {
        await gate.WaitAsync(ct);
        try
        {
            Saved? revoke=null;
            if(Configured)
            {
                var s=Load();revoke=s;var lib=Library();
                string metaPath=Path.Combine(lib,"_meta.json"),profilePath=Path.Combine(lib,s.Profile+".json");
                byte[]? originalMeta=ReadOptionalFile(metaPath),originalProfile=ReadOptionalFile(profilePath);
                var meta=ParseProfileMeta(originalMeta);var entries=meta["entries"]!.AsArray();
                string? originalApplied=(string?)meta["appliedId"];
                var ours=entries.FirstOrDefault(n=>(string?)n?["id"]==s.Profile);if(ours!=null)entries.Remove(ours);
                if((string?)meta["appliedId"]==s.Profile)
                {
                    var prior=entries.Any(n=>(string?)n?["id"]==s.Previous)?s.Previous:(string?)entries.FirstOrDefault()?["id"];
                    meta["appliedId"]=prior??"";
                    if(s.Hybrid!=null&&prior==s.Previous)meta["hybridPointer"]=s.Hybrid.DeepClone();
                }
                Action? undoPolicy=null;
                bool metaChanged=false,profileDeleted=false;
                try
                {
                    if(OperatingSystem.IsWindows())undoPolicy=RemoveWindowsPolicy(s.WindowsPolicy);
                    if(ours!=null&&s.CreatedMeta&&entries.Count==0&&meta.Count==2&&originalApplied==s.Profile)
                    {DeleteOptionalFile(metaPath);metaChanged=true;}
                    else if(ours!=null||(string?)meta["appliedId"]!=originalApplied)
                    {WriteProfileFile(metaPath,JsonSerializer.SerializeToUtf8Bytes(meta));metaChanged=true;}
                    DeleteOptionalFile(profilePath);profileDeleted=originalProfile!=null;
                    DeleteOptionalFile(StatePath);
                }
                catch
                {
                    try{if(profileDeleted)RestoreFile(profilePath,originalProfile);}
                    finally
                    {
                        try{if(metaChanged)RestoreFile(metaPath,originalMeta);}
                        finally{undoPolicy?.Invoke();}
                    }
                    throw;
                }
            }
            if(revoke is {Device:{} binding})
            {
                try{await DeviceControlAsync(revoke.Remote,new{action="revoke",binding.DeviceId,binding.DeviceProof},ct);}
                catch(Exception error) when(error is IOException or HttpRequestException or InvalidOperationException or OperationCanceledException)
                {
                    // Closing the local SSH lease also prevents remote access when offline.
                }
            }
            if(relay!=null){await relay.DisposeAsync();relay=null;}
            return Status();
        }
        catch(Exception error)when(IsProfileAccessError(error)){throw ProfileError(error);}
        finally{gate.Release();}
    }
    internal async Task StopAsync()
    {
        await gate.WaitAsync().ConfigureAwait(false);
        try
        {
            var current=relay;
            relay=null;
            if(current!=null)await current.DisposeAsync().ConfigureAwait(false);
        }
        finally{gate.Release();}
    }
    public void Dispose()=>StopAsync().GetAwaiter().GetResult();
}
