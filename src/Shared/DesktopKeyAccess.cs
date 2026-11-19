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
    Func<byte[],byte[]> unprotect, string? profileDirectory=null,ClientServices? diagnostics=null) : IDisposable
{
    private sealed record Ticket(string Url,string Pin,string Key);
    private sealed record Saved(Ticket Remote,string Local,int Port,string Profile,string Previous,JsonNode? Hybrid,bool CreatedMeta=false,
        Dictionary<string,PolicyValue>? WindowsPolicy=null);
    private sealed record Relay(WebApplication App,HttpClient Client,int Port) : IAsyncDisposable
    {
        public async ValueTask DisposeAsync()
        {
            try{await App.StopAsync(TimeSpan.FromSeconds(3)).ConfigureAwait(false);}
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
    // Shared with the gateway. Bundled keys deter casual inspection only.
    private static readonly byte[] TicketEncryptionKey = Convert.FromHexString("f04223626f9a64f32aa618705067a6a04a5d852f8e5b551c944d0b9cd2d7f54d");
    private static readonly byte[] TicketAuthKey = Convert.FromHexString("bf8145e375d11db0fdd2fb0d9734fdda504c5a480fc2eae4a567c1948aa86155");
    internal bool Running=>relay!=null;
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
        return Path.Combine(Directory.Exists(package)?Path.Combine(package,"LocalCache","Local"):local,"Claude-3p","configLibrary");
    }
    private static Ticket Parse(string text)
    {
        try
        {
            bool encrypted = text.StartsWith("sc2.", StringComparison.Ordinal);
            if(text.Length>2048||(!encrypted&&!text.StartsWith("sc1.",StringComparison.Ordinal)))throw new FormatException();
            string payload=text[4..].Replace('-','+').Replace('_','/');
            byte[] decoded=Convert.FromBase64String(payload.PadRight((payload.Length+3)/4*4,'='));
            var t=JsonSerializer.Deserialize<Ticket>(encrypted?DecryptTicket(decoded):decoded,Json);
            if(t==null||!Uri.TryCreate(t.Url,UriKind.Absolute,out var u)||u.Scheme!="https"||u.UserInfo!=""||u.PathAndQuery!="/"||u.Fragment!=""||
               !Regex.IsMatch(t.Key,@"\A[A-Za-z0-9_-]{43}\z")||!Regex.IsMatch(t.Pin,@"\A[0-9a-f]{64}\z"))throw new FormatException();
            return t;
        }
        catch(Exception e) when(e is ArgumentException or FormatException or JsonException or CryptographicException){throw new InvalidDataException("请输入完整的 SafeCode 接入 Key。");}
    }
    private static byte[] DecryptTicket(ReadOnlySpan<byte> encoded)
    {
        const int ivSize = 16;
        const int tagSize = 32;
        const int versionSize = 4;
        if (encoded.Length < ivSize * 2 + tagSize || (encoded.Length - tagSize) % ivSize != 0)
            throw new FormatException();
        var body = encoded[..^tagSize];
        Span<byte> authenticated = stackalloc byte[versionSize + body.Length];
        "sc2."u8.CopyTo(authenticated);
        body.CopyTo(authenticated[versionSize..]);
        Span<byte> tag = stackalloc byte[tagSize];
        HMACSHA256.HashData(TicketAuthKey, authenticated, tag);
        if (!CryptographicOperations.FixedTimeEquals(tag, encoded[^tagSize..]))
            throw new FormatException();
        using var aes = Aes.Create();
        aes.Key = TicketEncryptionKey;
        return aes.DecryptCbc(body[ivSize..], body[..ivSize]);
    }
    private static HttpClient Remote(Ticket t)
    {
        var pin=Convert.FromHexString(t.Pin);
        var handler=new HttpClientHandler{AllowAutoRedirect=false,UseCookies=false};
        handler.ServerCertificateCustomValidationCallback=(_,cert,_,_)=>cert!=null&&
            DateTime.UtcNow>=cert.NotBefore.ToUniversalTime()&&DateTime.UtcNow<=cert.NotAfter.ToUniversalTime()&&
            CryptographicOperations.FixedTimeEquals(pin,cert.GetCertHash(HashAlgorithmName.SHA256));
        var c=new HttpClient(handler){BaseAddress=new Uri(t.Url),Timeout=TimeSpan.FromMinutes(11),MaxResponseContentBufferSize=1024*1024};
        c.DefaultRequestHeaders.Authorization=new AuthenticationHeaderValue("Bearer",t.Key);
        c.DefaultRequestHeaders.Add("X-SafeCode-Platform",OperatingSystem.IsMacOS()?"macos":"windows");
        return c;
    }

    private async Task Forward(HttpContext ctx,HttpClient client,string token)
    {
        string path=ctx.Request.Path.Value??"";
        if(!(ctx.Request.Method=="GET"&&path=="/v1/models"||
             ctx.Request.Method=="POST"&&path is ("/v1/messages" or "/v1/messages/count_tokens")))
        {ctx.Response.StatusCode=404;return;}
        string auth=ctx.Request.Headers.Authorization.ToString();
        string key=auth.StartsWith("Bearer ",StringComparison.Ordinal)?auth[7..]:ctx.Request.Headers["x-api-key"].ToString();
        if(!CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(key),Encoding.UTF8.GetBytes(token)))
        {ctx.Response.StatusCode=401;return;}
        using var req=new HttpRequestMessage(new HttpMethod(ctx.Request.Method),path+ctx.Request.QueryString.Value);
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
        var trace=diagnostics?.BeginDiagnostic("desktop_key_request",background:true);
        Exception? failure=null;
        try
        {
            using var res=await client.SendAsync(req,HttpCompletionOption.ResponseHeadersRead,ctx.RequestAborted);
            if(trace!=null&&!res.IsSuccessStatusCode)failure=new HttpRequestException(null,null,res.StatusCode);
            ctx.Response.StatusCode=(int)res.StatusCode;
            ctx.Response.ContentType=res.Content.Headers.ContentType?.ToString()??"application/json";
            await res.Content.CopyToAsync(ctx.Response.Body,ctx.RequestAborted);
        }
        catch(Exception e) when(e is HttpRequestException or OperationCanceledException or IOException)
        {
            failure??=e is OperationCanceledException&&!ctx.RequestAborted.IsCancellationRequested?new TimeoutException():e;
            if(!ctx.Response.HasStarted)ctx.Response.StatusCode=502;else ctx.Abort();
        }
        finally
        {
            if(trace!=null&&(failure!=null||path=="/v1/messages")&&!ctx.RequestAborted.IsCancellationRequested)
                _=diagnostics!.SendDiagnosticAsync(trace,failure,CancellationToken.None);
        }
    }

    private async Task<Relay> Listen(Ticket t,string local,int port,CancellationToken ct)
    {
        var builder=WebApplication.CreateSlimBuilder(new WebApplicationOptions{Args=[],ContentRootPath=AppContext.BaseDirectory});
        builder.Configuration.Sources.Clear();
        builder.Logging.ClearProviders();
        builder.WebHost.ConfigureKestrel(k=>{k.Limits.MaxRequestBodySize=8*1024*1024;k.Limits.MaxConcurrentConnections=32;k.Listen(IPAddress.Loopback,port);});
        var app=builder.Build();var client=Remote(t);app.Run(ctx=>Forward(ctx,client,local));
        try
        {
            await app.StartAsync(ct);
            var addresses=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses;
            return new(app,client,new Uri(addresses.Single()).Port);
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
        finally{File.Delete(temp);}
    }
    internal static JsonObject ReadProfileMeta(string lib)
    {
        var path=Path.Combine(lib,"_meta.json");
        var meta=File.Exists(path)?JsonNode.Parse(File.ReadAllText(path)) as JsonObject:new JsonObject{["entries"]=new JsonArray(),["appliedId"]=""};
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

    [SupportedOSPlatform("windows")]
    private static (Dictionary<string,PolicyValue> Owned,Dictionary<string,PolicyValue> Missing) PlanWindowsPolicy(JsonObject profile,Saved saved)
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
        using var machine=Registry.LocalMachine.OpenSubKey(ClaudeDesktopPolicy.RegistryPath,false);
        using var user=Registry.CurrentUser.OpenSubKey(ClaudeDesktopPolicy.RegistryPath,false);
        var owned=new Dictionary<string,PolicyValue>();
        var missing=new Dictionary<string,PolicyValue>();
        foreach(var (field,value) in desired)
        {
            var machineValue=machine?.GetValue(field,null,RegistryValueOptions.DoNotExpandEnvironmentNames);
            var userValue=user?.GetValue(field,null,RegistryValueOptions.DoNotExpandEnvironmentNames);
            if((machineValue!=null&&!MatchesPolicyValue(machineValue,value))||(userValue!=null&&!MatchesPolicyValue(userValue,value)))
                throw new InvalidOperationException("Claude Desktop 现有管理策略与 Safe Code API 接入冲突，请先停用原管理配置。");
            if(machineValue==null&&userValue==null)missing[field]=value;
            if(missing.ContainsKey(field)||saved.WindowsPolicy?.ContainsKey(field)==true)owned[field]=value;
        }
        return (owned,missing);
    }

    private static bool MatchesPolicyValue(object? current,PolicyValue value)=>value.Kind==PolicyValueKind.DWord?
        current is int number&&number==value.Number:current is string text&&text==value.Text;

    [SupportedOSPlatform("windows")]
    private static void ApplyWindowsPolicy(IReadOnlyDictionary<string,PolicyValue> fields)
    {
        if(fields.Count==0)return;
        using var key=OpenWindowsPolicyForValues()??Registry.CurrentUser.CreateSubKey(ClaudeDesktopPolicy.RegistryPath,true)
            ??throw new IOException("无法打开 Claude Desktop 本机策略。");
        var written=new Dictionary<string,PolicyValue>();
        try
        {
            foreach(var (field,value) in fields)
            {
                var current=key.GetValue(field,null,RegistryValueOptions.DoNotExpandEnvironmentNames);
                if(MatchesPolicyValue(current,value))continue;
                if(current!=null)throw new InvalidOperationException("Claude Desktop 策略已变更，请重新启用 Safe Code API 接入。");
                if(value.Kind==PolicyValueKind.DWord)key.SetValue(field,value.Number!.Value,RegistryValueKind.DWord);
                else key.SetValue(field,value.Text!,RegistryValueKind.String);
                written[field]=value;
            }
        }
        catch
        {
            DeleteMatchingPolicyValues(key,written);
            throw;
        }
    }

    [SupportedOSPlatform("windows")]
    private static RegistryKey? OpenWindowsPolicyForValues()=>Registry.CurrentUser.OpenSubKey(
        ClaudeDesktopPolicy.RegistryPath,RegistryKeyPermissionCheck.ReadWriteSubTree,RegistryRights.QueryValues|RegistryRights.SetValue);

    [SupportedOSPlatform("windows")]
    private static void RemoveWindowsPolicy(IReadOnlyDictionary<string,PolicyValue>? fields)
    {
        if(fields is not {Count:>0})return;
        using var read=Registry.CurrentUser.OpenSubKey(ClaudeDesktopPolicy.RegistryPath,false);
        if(read==null||!fields.Any(pair=>MatchesPolicyValue(read.GetValue(pair.Key,null,RegistryValueOptions.DoNotExpandEnvironmentNames),pair.Value)))return;
        using var key=OpenWindowsPolicyForValues();
        if(key!=null)DeleteMatchingPolicyValues(key,fields);
    }

    [SupportedOSPlatform("windows")]
    private static void DeleteMatchingPolicyValues(RegistryKey key,IReadOnlyDictionary<string,PolicyValue> fields)
    {
        foreach(var (field,value) in fields)
            if(MatchesPolicyValue(key.GetValue(field,null,RegistryValueOptions.DoNotExpandEnvironmentNames),value))key.DeleteValue(field,false);
    }

    private const string WindowsPolicyPermissionMessage="Windows 拒绝访问 Claude Desktop 的本机策略。请用原来配置接入的 Windows 账户打开 Safe Code，或请管理员检查该账户的策略权限。";
    private object Status(string? error=null)=>new{configured=Configured,running=relay!=null,error};
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

    internal async Task<object> RestoreAsync(CancellationToken ct,bool interactive=false)
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
                    var saved=Load();string lib=Library();
                    if((string?)ReadProfileMeta(lib)["appliedId"]==saved.Profile)
                    {
                        var path=Path.Combine(lib,saved.Profile+".json");
                        MigrateClassifier(path);
                        var restored=await Listen(saved.Remote,saved.Local,saved.Port,ct);
                        try
                        {
                            if(OperatingSystem.IsWindows())
                            {
                                var profile=JsonNode.Parse(File.ReadAllBytes(path))?.AsObject()??throw new IOException("无法读取 Claude 接入配置。");
                                var (fields,missing)=PlanWindowsPolicy(profile,saved);
                                if(saved.WindowsPolicy==null||!ClaudeDesktopPolicy.MatchesWritten(fields,saved.WindowsPolicy)||saved.WindowsPolicy.Count!=fields.Count)
                                    Save(saved with{WindowsPolicy=fields});
                                ApplyWindowsPolicy(missing);
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
                    return Status(OperatingSystem.IsWindows()&&(error is SecurityException or UnauthorizedAccessException)
                        ? WindowsPolicyPermissionMessage : error.Message);
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
    internal async Task<object> EnableAsync(string code,CancellationToken ct)
    {
        var t=Parse(code.Trim());await gate.WaitAsync(ct);
        try
        {
            if(Configured)throw new InvalidOperationException("请先停用当前 Key 再切换。");
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
            string lib=Library(),idNew=Guid.NewGuid().ToString();
            var meta=ReadProfileMeta(lib);var entries=meta["entries"]!.AsArray();
            string local=Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
            var next=await Listen(t,local,0,ct);
            Dictionary<string,PolicyValue>? windowsPolicy=null;
            Dictionary<string,PolicyValue>? appliedPolicy=null;
            try
            {
                var s=new Saved(t,local,next.Port,idNew,(string?)meta["appliedId"]??"",meta["hybridPointer"]?.DeepClone(),!File.Exists(Path.Combine(lib,"_meta.json")));
                var profile=new JsonObject{["inferenceProvider"]="gateway",["inferenceGatewayBaseUrl"]="http://127.0.0.1:"+next.Port,
                    ["inferenceGatewayAuthScheme"]="bearer",["inferenceCredentialKind"]="static",["inferenceGatewayApiKey"]=local,
                    ["deploymentMode"]="3p",["disableDeploymentModeChooser"]=true,["deploymentDisplayName"]="SafeCode",
                    ["inferenceModels"]=models,["modelDiscoveryEnabled"]=false,["autoModeEnabled"]=true,["chatTabEnabled"]=true};
                if(classifier!=null)SetClassifier(profile,classifier);
                Dictionary<string,PolicyValue>? missingPolicy=null;
                if(OperatingSystem.IsWindows())(windowsPolicy,missingPolicy)=PlanWindowsPolicy(profile,s);
                Save(s with{WindowsPolicy=windowsPolicy});
                WriteProfileFile(Path.Combine(lib,idNew+".json"),JsonSerializer.SerializeToUtf8Bytes(profile));
                if(OperatingSystem.IsWindows()&&missingPolicy!=null)
                {
                    ApplyWindowsPolicy(missingPolicy);
                    appliedPolicy=missingPolicy;
                }
                entries.Add(new JsonObject{["id"]=idNew,["name"]="SafeCode"});
                meta["appliedId"]=idNew;meta.Remove("hybridPointer");
                WriteProfileFile(Path.Combine(lib,"_meta.json"),JsonSerializer.SerializeToUtf8Bytes(meta));
                relay=next;
            }
            catch(Exception error)
            {
                await next.DisposeAsync();
                if(OperatingSystem.IsWindows())RemoveWindowsPolicy(appliedPolicy);
                string profilePath=Path.Combine(lib,idNew+".json");
                if(File.Exists(profilePath))File.Delete(profilePath);
                if(File.Exists(StatePath))File.Delete(StatePath);
                if(OperatingSystem.IsWindows()&&(error is SecurityException or UnauthorizedAccessException))
                    throw new InvalidOperationException(WindowsPolicyPermissionMessage,error);
                throw;
            }
            return Status();
        }
        finally{gate.Release();}
    }

    private static async Task<HttpResponseMessage> VerifyRemoteAsync(HttpClient client, CancellationToken ct)
    {
        try
        {
            return await client.GetAsync("/v1/models", ct);
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
            if(Configured)
            {
                var s=Load();var lib=Library();var meta=ReadProfileMeta(lib);var entries=meta["entries"]!.AsArray();
                if(OperatingSystem.IsWindows())RemoveWindowsPolicy(s.WindowsPolicy);
                string? originalApplied=(string?)meta["appliedId"];
                var ours=entries.FirstOrDefault(n=>(string?)n?["id"]==s.Profile);if(ours!=null)entries.Remove(ours);
                if((string?)meta["appliedId"]==s.Profile)
                {
                    var prior=entries.Any(n=>(string?)n?["id"]==s.Previous)?s.Previous:(string?)entries.FirstOrDefault()?["id"];
                    meta["appliedId"]=prior??"";
                    if(s.Hybrid!=null&&prior==s.Previous)meta["hybridPointer"]=s.Hybrid.DeepClone();
                }
                if(ours!=null && s.CreatedMeta && entries.Count==0 && meta.Count==2 && originalApplied==s.Profile)File.Delete(Path.Combine(lib,"_meta.json"));
                else if(ours!=null || (string?)meta["appliedId"]!=originalApplied) WriteProfileFile(Path.Combine(lib,"_meta.json"),JsonSerializer.SerializeToUtf8Bytes(meta));
                File.Delete(Path.Combine(lib,s.Profile+".json"));File.Delete(StatePath);
            }
            if(relay!=null){await relay.DisposeAsync();relay=null;}
            return Status();
        }
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
