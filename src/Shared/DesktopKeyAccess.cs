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
    string? windowsPolicyPath=null) : IDisposable
{
    private const int MaxMessagesRequest = 16 * 1024 * 1024;
    private const string DeviceAlreadyBoundMessage="该 Key 已绑定设备。请在原设备停用接入，或联系网关管理员检查绑定状态。";
    private const string UnbindPendingMessage="本机接入已停用，网关尚未确认解绑。请点击「重试解绑」查看结果。";

    internal sealed record DeviceBinding(string DeviceId,string DeviceProof,string Alias,int Port,string PublicKey);
    internal sealed record DeviceSetup(Ticket Remote,bool Replace,bool UseSaved,DeviceBinding? Existing,SshSession? HostSession=null,bool Switching=false);
    private sealed record Saved(Ticket Remote,string Local,int Port,string Profile,string Previous,JsonNode? Hybrid,bool CreatedMeta=false,
        Dictionary<string,PolicyValue>? WindowsPolicy=null,DeviceBinding? Device=null,bool UnbindPending=false);
    private sealed record Relay(WebApplication App,HttpClient Client,int Port,Func<bool>? HostReady,long StartedAt,RelayConnection Connection) : IAsyncDisposable
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
    internal sealed record DevicePath(Func<string?> Problem,Func<bool,CancellationToken,Task> Check,Func<int> Generation);
    private sealed class RelayConnection(DevicePath? device,Func<bool>? hostReady)
    {
        internal string? DeviceProblem=>device?.Problem()??(hostReady?.Invoke()==false?"已授权的本机 SSH 控制通道尚未恢复，请检查 SafeCode 的本机远程控制状态。":null);
        internal async Task CheckDeviceAsync(bool force,CancellationToken ct)
        {
            if(device!=null)await device.Check(force,ct);
            if(DeviceProblem is {} reason)throw new InvalidOperationException(reason);
        }
        internal int Generation=>device?.Generation()??0;
        private long lastMessageCheck;
        private int clientGeneration;
        private volatile bool clientSeen;
        internal bool ClientConnected=>clientSeen && Volatile.Read(ref clientGeneration)==Generation;
        internal void ObserveClient(int generation)
        {
            Volatile.Write(ref clientGeneration,generation);
            clientSeen=true;
            RequestError=null;
        }
        internal async Task CheckMessageAsync(CancellationToken ct)
        {
            long now=DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),age=now-Interlocked.Read(ref lastMessageCheck);
            await CheckDeviceAsync(age<0 || age>=45000,ct);
            Interlocked.Exchange(ref lastMessageCheck,now);
        }
        internal volatile string? RequestError;
    }
    internal sealed record ConnectionState(long? StartedAt,bool ClientConnected,string? DeviceProblem,string? RequestError);
    internal ConnectionState ConnectionStatus
    {
        get
        {
            var active=Volatile.Read(ref relay);
            return new(active?.StartedAt,active?.Connection.ClientConnected==true,active?.Connection.DeviceProblem,active?.Connection.RequestError);
        }
    }
    private readonly SemaphoreSlim gate=new(1);
    private string StatePath=>Path.Combine(dataDirectory,"desktop-key.state");
    private Relay? relay;
    private static readonly JsonSerializerOptions Json=new(JsonSerializerDefaults.Web);
    internal bool Running=>relay!=null;
    internal long? StartedAt=>relay?.StartedAt;
    internal bool ReusesHostControl=>relay?.HostReady!=null;
    internal bool Configured=>File.Exists(StatePath);
    internal async Task<KeyBalance> ReadBalanceAsync(CancellationToken ct)
    {
        Ticket remote;
        await gate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            if (!Running) throw new InvalidOperationException("API Key 接入尚未启动。");
            remote=Load().Remote;
        }
        finally { gate.Release(); }
        return await FetchBalanceAsync(remote,ct).ConfigureAwait(false);
    }

    private static async Task<KeyBalance> FetchBalanceAsync(Ticket remote,CancellationToken ct)
    {
        using var client=Remote(remote);
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(TimeSpan.FromSeconds(8));
        using var response=await client.GetAsync("/v1/key/balance",deadline.Token).ConfigureAwait(false);
        if (response.StatusCode==HttpStatusCode.NotFound)
            throw new InvalidOperationException("请升级 VPS 网关后再查看 Key 余额。");
        if (response.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden)
            throw new InvalidOperationException("Key 无效或公网网关已停用。");
        response.EnsureSuccessStatusCode();
        using var body=JsonDocument.Parse(await response.Content.ReadAsByteArrayAsync(deadline.Token).ConfigureAwait(false));
        var value=body.RootElement;
        var remaining=value.GetProperty("remaining_nanos");
        IReadOnlyList<KeyModelUsage>? models=null;
        if(value.TryGetProperty("models",out var rows)&&rows.ValueKind==JsonValueKind.Array)
            models=rows.EnumerateArray().Select(row=>new KeyModelUsage(
                row.GetProperty("model").GetString()??"",
                row.GetProperty("input").GetInt64(),row.GetProperty("output").GetInt64(),
                row.GetProperty("cache_read").GetInt64(),row.GetProperty("cache_write").GetInt64(),
                row.GetProperty("cost_nanos").GetInt64(),row.GetProperty("unpriced_tokens").GetInt64())).ToArray();
        return new KeyBalance(value.GetProperty("used_nanos").GetInt64(),
                              remaining.ValueKind==JsonValueKind.Null?null:remaining.GetInt64(),models);
    }

    internal sealed record KeyModelUsage(string Model,long Input,long Output,long CacheRead,long CacheWrite,
        long CostNanos,long UnpricedTokens);
    internal sealed record KeyBalance(long UsedNanos,long? RemainingNanos,IReadOnlyList<KeyModelUsage>? Models)
    {
        private static string Usd(long nanos)=>"$"+(nanos/1_000_000_000m).ToString("0.00##",System.Globalization.CultureInfo.InvariantCulture);
        public string Used=>"已用 "+Usd(UsedNanos);
        public string Remaining=>RemainingNanos is long amount?"余额 "+Usd(amount):"不限额";
    }
    private string Library(Saved? saved=null)
    {
        if(profileDirectory!=null)return profileDirectory;
        if(OperatingSystem.IsWindows() && saved!=null)
        {
            foreach(var library in new[] { WindowsProfileLibrary(true), WindowsProfileLibrary(false) })
                if(File.Exists(Path.Combine(library,saved.Profile+".json")))return library;
        }
        return ProfileLibrary();
    }
    internal static string ProfileLibrary()
    {
        if(OperatingSystem.IsMacOS())return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),"Library/Application Support/Claude-3p/configLibrary");
        return WindowsProfileLibrary(PrefersPackagedProfile());
    }

    private static string WindowsProfileLibrary(bool packaged)
    {
        var local=Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        return Path.Combine(packaged?Path.Combine(local,"Packages","Claude_pzs8sxrjxfjjc","LocalCache","Local"):local,
            "Claude-3p","configLibrary");
    }

    [System.Runtime.InteropServices.DllImport("kernel32.dll",CharSet=System.Runtime.InteropServices.CharSet.Unicode,ExactSpelling=true)]
    private static extern int GetPackagesByPackageFamily(string packageFamilyName,ref uint count,IntPtr names,
        ref uint bufferLength,IntPtr buffer);

    internal bool UsesPackagedProfile()=>string.Equals(Library(Load()),WindowsProfileLibrary(true),StringComparison.OrdinalIgnoreCase);

    private static bool PrefersPackagedProfile()
    {
        uint count=0,length=0;
        int result=GetPackagesByPackageFamily("Claude_pzs8sxrjxfjjc",ref count,IntPtr.Zero,ref length,IntPtr.Zero);
        return result==122 && count>0;
    }
    // Certificate pinning authenticates the gateway directly; proxy CONNECT tunnels only add a failure point.
    internal static HttpClientHandler RemoteHandler(Ticket t)
    {
        var pin=Convert.FromHexString(t.Pin);
        return new HttpClientHandler{AllowAutoRedirect=false,UseCookies=false,UseProxy=false,
            ServerCertificateCustomValidationCallback=(_,cert,_,_)=>MatchesPin(pin,cert)};
    }
    private static HttpClient Remote(Ticket t)
    {
        var handler=RemoteHandler(t);
        var c=new HttpClient(handler){BaseAddress=new Uri(t.Url),Timeout=TimeSpan.FromMinutes(11),MaxResponseContentBufferSize=1024*1024};
        c.DefaultRequestHeaders.Authorization=new AuthenticationHeaderValue("Bearer",t.Key);
        c.DefaultRequestHeaders.Add("X-SafeCode-Platform",OperatingSystem.IsMacOS()?"macos":"windows");
        c.DefaultRequestHeaders.Add("X-SafeCode-Client","safecode-desktop");
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
        return (await RequestDeviceControlAsync(client,request,ct))!.Value;
    }

    private static async Task<JsonElement?> RequestDeviceControlAsync(HttpClient client,object request,CancellationToken ct,bool optional=false)
    {
        using var body=new ByteArrayContent(JsonSerializer.SerializeToUtf8Bytes(request,Json));
        body.Headers.ContentType=new MediaTypeHeaderValue("application/json");
        using var response=await client.PostAsync("/v1/device-control",body,ct).ConfigureAwait(false);
        if(!response.IsSuccessStatusCode)
        {
            if(response.StatusCode==HttpStatusCode.Unauthorized)
                throw new InvalidOperationException("Key 无效、已过期或已撤销。");
            if(optional&&response.StatusCode==HttpStatusCode.NotFound)return null;
            try
            {
                using var error = JsonDocument.Parse(await response.Content.ReadAsByteArrayAsync(ct).ConfigureAwait(false));
                if(error.RootElement.ValueKind==JsonValueKind.Object &&
                    error.RootElement.TryGetProperty("error",out var details) && details.ValueKind==JsonValueKind.Object)
                {
                    if(optional && response.StatusCode==HttpStatusCode.Forbidden &&
                        details.TryGetProperty("message",out var message) &&
                        message.GetString()=="Device control is unavailable for this Key")return null;
                    if(details.TryGetProperty("code",out var code) && code.ValueKind==JsonValueKind.String)
                    {
                        string? explanation = code.GetString() switch
                        {
                            "device_not_configured" => "此 VPS 尚未安装私有设备服务。请在「网关设置」升级或修复网关。",
                            "device_not_allowed" => "此 Key 没有设备配对权限。请让签发人在「管理网关」将接入用途改为「Safe Code 桌面接入」。",
                            "device_already_bound" => DeviceAlreadyBoundMessage,
                            "device_auth_unavailable" => "VPS 的设备鉴权服务未就绪。请在「网关设置」运行故障修复。",
                            "device_frp_unavailable" => "VPS 的私有 FRP 服务未就绪。请在「网关设置」运行故障修复。",
                            "device_ssh_verification_failed" => "网关未能通过 SSH 登录本机。请检查本机远程控制服务及登录权限，SafeCode 会重新验证。",
                            _ => null
                        };
                        if(explanation!=null)throw new InvalidOperationException(explanation);
                    }
                }
            }
            catch(JsonException){}
            if(response.StatusCode==HttpStatusCode.BadRequest && JsonSerializer.SerializeToElement(request,Json).TryGetProperty("action",out var action) && action.GetString()=="renew")
                throw new InvalidOperationException("网关未认可本机保存的设备授权，请检查 Key 的设备绑定并重新授权。");
            if(response.StatusCode==HttpStatusCode.Conflict)
                throw new InvalidOperationException(DeviceAlreadyBoundMessage);
            throw new InvalidOperationException("网关未能完成本机远程控制操作（HTTP "+(int)response.StatusCode+"）。请检查该 Key 的设备授权与 FRP 状态。");
        }
        using var document=JsonDocument.Parse(await response.Content.ReadAsByteArrayAsync(ct).ConfigureAwait(false));
        return document.RootElement.Clone();
    }

    private async Task Forward(HttpContext ctx,HttpClient client,string token,RelayConnection connection)
    {
        string path=ctx.Request.Path.Value??"";
        if(!(ctx.Request.Method=="GET"&&path=="/v1/models"||
             ctx.Request.Method=="POST"&&path is ("/v1/messages" or "/v1/messages/count_tokens")))
        {ctx.Response.StatusCode=404;return;}
        string auth=ctx.Request.Headers.Authorization.ToString();
        string key=auth.StartsWith("Bearer ",StringComparison.Ordinal)?auth[7..]:ctx.Request.Headers["x-api-key"].ToString();
        bool launchProbe=path=="/v1/models" && ctx.Request.Headers.ContainsKey("X-SafeCode-Launch-Probe");
        if(!CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(key),Encoding.UTF8.GetBytes(token)))
        {
            const string message="本机接入凭据无效，请在 SafeCode 重新启用接入并重开 Claude。";
            connection.RequestError=message;
            await RelayError(ctx,401,"authentication_error","local_auth_invalid",message);
            return;
        }
        // Old gateways do not expose this namespace, so downgrades cannot dispatch a native CLI turn.
        using var req=new HttpRequestMessage(new HttpMethod(ctx.Request.Method),"/v1/cloudcli"+path[3..]+ctx.Request.QueryString.Value);
        if(ctx.Request.Method=="POST")
        {
            // The remote Messages endpoint requires Content-Length, including for a chunked local request.
            using var body=new MemoryStream();
            await ctx.Request.Body.CopyToAsync(body,ctx.RequestAborted);
            if(body.Length>MaxMessagesRequest){ctx.Response.StatusCode=413;return;}
            req.Content=new ByteArrayContent(body.ToArray());
            req.Content.Headers.ContentType=new MediaTypeHeaderValue("application/json");
        }
        foreach(var h in new[]{"anthropic-version","anthropic-beta","x-claude-code-session-id"})
            if(ctx.Request.Headers.TryGetValue(h,out var v))req.Headers.TryAddWithoutValidation(h,v.ToArray());
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
            if(path is "/v1/messages" or "/v1/messages/count_tokens")
            {
                try
                {
                    await connection.CheckMessageAsync(ctx.RequestAborted);
                }
                catch(Exception e) when(!ctx.RequestAborted.IsCancellationRequested &&
                    e is InvalidOperationException or OperationCanceledException or IOException or HttpRequestException)
                {
                    var reason=e is InvalidOperationException?e.Message:"设备回连验证未完成，请检查 SafeCode 的连接状态后重试。";
                    await RelayError(ctx,503,"api_error","device_offline",reason);
                    return;
                }
            }
            int epoch=connection.Generation;
            using var res=await client.SendAsync(req,HttpCompletionOption.ResponseHeadersRead,ctx.RequestAborted);
            if(gateway!=null)
            {
                res.Headers.TryGetValues("X-SafeCode-Gateway-Diagnostic",out var headers);
                gateway.Response((int)res.StatusCode,headers);
                if(!res.IsSuccessStatusCode && !await gateway.IsUnsupportedTokenCountAsync(res,ctx.RequestAborted))
                    failure=new HttpRequestException(null,null,res.StatusCode);
            }
            if(!launchProbe && res.StatusCode==HttpStatusCode.Unauthorized)
                connection.RequestError="网关未接受当前 Key，请核对 Key 是否过期或已被撤销。";
            ctx.Response.StatusCode=(int)res.StatusCode;
            ctx.Response.ContentType=res.Content.Headers.ContentType?.ToString()??"application/json";
            if(res.Content.Headers.ContentType?.MediaType=="text/event-stream")
            {
                var streamStatus=gateway??new GatewayDiagnostic("messages");
                await using var input=await res.Content.ReadAsStreamAsync(ctx.RequestAborted);
                var buffer=new byte[16384];
                int count;
                while((count=await input.ReadAsync(buffer,ctx.RequestAborted))>0)
                {
                    bytes+=count;
                    streamStatus.ObserveSse(buffer.AsSpan(0,count));
                    await ctx.Response.Body.WriteAsync(buffer.AsMemory(0,count),ctx.RequestAborted);
                    await ctx.Response.Body.FlushAsync(ctx.RequestAborted);
                }
                if(streamStatus.SawError)failure=new HttpRequestException(null,null,(HttpStatusCode)(streamStatus.FailureStatus??502));
                else if(!streamStatus.Ended)
                {
                    connection.RequestError="网关连接在这条回复结束前中断，请检查网络后重试。";
                    failure=new HttpRequestException(null,null,HttpStatusCode.BadGateway);
                }
            }
            else
            {
                bytes=res.Content.Headers.ContentLength??0;
                await res.Content.CopyToAsync(ctx.Response.Body,ctx.RequestAborted);
            }
            if(path=="/v1/messages" && res.IsSuccessStatusCode && failure==null)
                connection.ObserveClient(epoch);
        }
        catch(Exception e) when(e is HttpRequestException or OperationCanceledException or IOException)
        {
            failure??=e is OperationCanceledException&&!ctx.RequestAborted.IsCancellationRequested?new TimeoutException():e;
            if(ctx.RequestAborted.IsCancellationRequested){ctx.Abort();return;}
            string reason=e switch {
                HttpRequestException {HttpRequestError:HttpRequestError.SecureConnectionError}=>"与网关的 TLS 握手未完成。请先检查网络，若持续失败再核对网关证书。",
                OperationCanceledException=>"等待网关响应超时，请检查网络和网关状态。",
                _=>"SafeCode 无法连接网关，请检查网络和网关地址。"
            };
            if(!launchProbe)connection.RequestError=reason;
            if(ctx.Response.HasStarted){ctx.Abort();return;}
            await RelayError(ctx,502,"api_error","gateway_unavailable",reason);
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

    private static Task RelayError(HttpContext ctx,int status,string type,string code,string message)
    {
        ctx.Response.StatusCode=status;
        return ctx.Response.WriteAsJsonAsync(new {type="error",error=new {type,code,message}},ctx.RequestAborted);
    }

    private async Task<Relay> Listen(Ticket t,string local,int port,CancellationToken ct,DeviceBinding? device=null,Func<bool>? hostReady=null,DevicePath? devicePath=null)
    {
        var builder=WebApplication.CreateSlimBuilder(new WebApplicationOptions{Args=[],ContentRootPath=AppContext.BaseDirectory});
        builder.Configuration.Sources.Clear();
        builder.Logging.ClearProviders();
        builder.WebHost.ConfigureKestrel(k=>{k.Limits.MaxRequestBodySize=MaxMessagesRequest;k.Limits.MaxConcurrentConnections=32;k.Listen(IPAddress.Loopback,port);});
        var app=builder.Build();var client=Remote(t);
        if(device!=null)
        {
            client.DefaultRequestHeaders.Add("X-SafeCode-Device",device.DeviceId);
            client.DefaultRequestHeaders.Add("X-SafeCode-Device-Proof",device.DeviceProof);
        }
        var connection=new RelayConnection(devicePath,hostReady);
        app.Run(ctx=>Forward(ctx,client,local,connection));
        try
        {
            await app.StartAsync(ct);
            var addresses=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses;
            return new(app,client,new Uri(addresses.Single()).Port,hostReady,DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),connection);
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
    private sealed record LegacyMacProfile(string Id,string Token,int Port);

    internal void RemoveLegacyMacProfiles(JsonObject meta,string library)
    {
        if(meta["hybridPointer"]!=null)return;
        var entries=meta["entries"]!.AsArray();
        foreach(var entry in entries.OfType<JsonObject>().Where(entry=>ProfileText(entry,"name")=="SafeCode SSH").ToArray())
        {
            var id=ProfileText(entry,"id");
            if(!Guid.TryParseExact(id,"D",out _))continue;
            var path=Path.Combine(library,id+".json");
            var data=ReadOptionalFile(path);
            if(data==null||!OwnsLegacyMacProfile(id!,data))continue;
            entries.Remove(entry);
            if(ProfileText(meta,"appliedId")==id)meta["appliedId"]="";
        }
    }

    private bool OwnsLegacyMacProfile(string id,byte[] data)
    {
        try{return JsonNode.Parse(data) is JsonObject profile&&MatchesLegacyMacProfile(id,profile);}
        catch(Exception error)when(error is JsonException or CryptographicException){return false;}
    }

    private bool MatchesLegacyMacProfile(string id,JsonObject profile)
    {
        var url=ProfileText(profile,"inferenceGatewayBaseUrl");
        if(!Uri.TryCreate(url,UriKind.Absolute,out var endpoint)||endpoint.Port is <1024 or >65535
            ||url!="http://127.0.0.1:"+endpoint.Port)return false;
        var credential=ProfileText(profile,"inferenceGatewayApiKey");
        var encrypted=ReadOptionalFile(Path.Combine(dataDirectory,"desktop-ssh.aes"));
        if(encrypted==null)return false;
        byte[] plain=unprotect(encrypted);
        try
        {
            var state=JsonSerializer.Deserialize<LegacyMacProfile>(plain);
            if(state==null || state.Id!=id || state.Port!=endpoint.Port || string.IsNullOrEmpty(state.Token))return false;
            if(state.Token==credential)return true;
            return credential is {Length:>14}&&credential.StartsWith("__sealed__:v1:",StringComparison.Ordinal)
                &&ProfileText(profile,"inferenceProvider")=="gateway"&&ProfileText(profile,"inferenceGatewayAuthScheme")=="bearer"
                &&ProfileText(profile,"inferenceCredentialKind")=="static"&&ProfileText(profile,"deploymentDisplayName")=="SafeCode";
        }
        finally{CryptographicOperations.ZeroMemory(plain);}
    }

    private static string? ProfileText(JsonObject obj,string name)=>
        obj[name] is JsonValue value&&value.TryGetValue<string>(out var text)?text:null;

    private Saved Load()=>JsonSerializer.Deserialize<Saved>(unprotect(File.ReadAllBytes(StatePath)),Json)??throw new IOException("无法读取保存的接入配置。");
    private void Save(Saved value)
    {
        byte[] data=JsonSerializer.SerializeToUtf8Bytes(value,Json);
        try{WriteProfileFile(StatePath,protect(data));}
        finally{CryptographicOperations.ZeroMemory(data);}
    }

    private sealed record KeyEntry(string Id,string Name,Ticket Remote,bool Authorized);
    private string KeyListPath=>Path.Combine(dataDirectory,"desktop-key-list.state");
    private List<KeyEntry> ReadEntries()
    {
        var entries=File.Exists(KeyListPath)
            ? JsonSerializer.Deserialize<List<KeyEntry>>(unprotect(File.ReadAllBytes(KeyListPath)),Json)
                ?? throw new IOException("无法读取已保存的 Key。")
            : new List<KeyEntry>();
        if(Configured)
        {
            var current=Load();
            if(entries.All(item=>item.Remote!=current.Remote))
            {
                entries.Add(NewEntry(current.Remote,new Uri(current.Remote.Url).Authority,current.Device!=null));
                WriteEntries(entries);
            }
        }
        return entries;
    }
    private static KeyEntry NewEntry(Ticket ticket,string name,bool authorized)=>
        new(Guid.NewGuid().ToString("N"),name,ticket,authorized);
    private void WriteEntries(List<KeyEntry> entries)
    {
        var bytes=JsonSerializer.SerializeToUtf8Bytes(entries,Json);
        try{WriteProfileFile(KeyListPath,protect(bytes));}
        finally{CryptographicOperations.ZeroMemory(bytes);}
    }
    private static KeyEntry FindEntry(List<KeyEntry> entries,string id)=>
        entries.FirstOrDefault(entry=>entry.Id==id)??throw new InvalidOperationException("找不到该 Key，请重新打开列表。");

    internal async Task<object> ListKeysAsync(CancellationToken ct)
    {
        await gate.WaitAsync(ct);
        try
        {
            var current=Configured?Load().Remote:null;
            return ReadEntries().Select(e=>new {e.Id,e.Name,current=e.Remote==current,running=e.Remote==current&&Running}).ToArray();
        }
        finally{gate.Release();}
    }
    private static string KeyName(string name)
    {
        name=name.Trim();
        if(name.Length is 0 or >64)throw new InvalidOperationException("请输入 1 至 64 个字符的 Key 名称。");
        return name;
    }
    internal async Task RenameKeyAsync(string id,string name,CancellationToken ct)
    {
        name=KeyName(name);
        await gate.WaitAsync(ct);
        try
        {
            var entries=ReadEntries();
            var item=FindEntry(entries,id);
            entries[entries.IndexOf(item)]=item with {Name=name};
            WriteEntries(entries);
        }
        finally{gate.Release();}
    }
    internal async Task SaveKeyAsync(string code,string name,CancellationToken ct)
    {
        name=KeyName(name);
        var ticket=Ticket.Parse(code.Trim());
        await gate.WaitAsync(ct);
        try
        {
            var entries=ReadEntries();
            var existing=entries.FindIndex(e=>e.Remote==ticket);
            if(existing<0)entries.Add(NewEntry(ticket,name,false));
            else entries[existing]=entries[existing] with {Name=name};
            WriteEntries(entries);
        }
        finally{gate.Release();}
    }
    internal async Task ForgetKeyAsync(string id,CancellationToken ct)
    {
        await gate.WaitAsync(ct);
        try
        {
            var entries=ReadEntries();
            var item=FindEntry(entries,id);
            if(Configured&&Load().Remote==item.Remote)
                throw new InvalidOperationException("请先切换到其他 Key 或停用当前接入。");
            entries.Remove(item);
            WriteEntries(entries);
        }
        finally{gate.Release();}
    }
    internal async Task<KeyBalance> SavedBalanceAsync(string id,CancellationToken ct)
    {
        Ticket ticket;
        await gate.WaitAsync(ct);
        try{ticket=FindEntry(ReadEntries(),id).Remote;}
        finally{gate.Release();}
        return await FetchBalanceAsync(ticket,ct);
    }
    internal async Task<bool> HasDeviceConsentAsync(Ticket ticket,CancellationToken ct)
    {
        await gate.WaitAsync(ct);
        try{return ReadEntries().Any(e=>e.Remote==ticket&&e.Authorized);}
        finally{gate.Release();}
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

    private sealed record LegacyWindowsPolicy(int Version,Dictionary<string,PolicyValue> Written,Dictionary<string,PolicyValue?> Original);

    [SupportedOSPlatform("windows")]
    private Action? RemoveLegacyWindowsPolicy()
    {
        var directory=Path.Combine(dataDirectory,"ClaudeDesktop");
        var encrypted=ReadOptionalFile(Path.Combine(directory,"backup.dpapi"));
        if(encrypted==null)return null;
        byte[] plain=unprotect(encrypted);
        LegacyWindowsPolicy? backup;
        try{backup=JsonSerializer.Deserialize<LegacyWindowsPolicy>(plain);}
        finally{CryptographicOperations.ZeroMemory(plain);}
        if(backup is not {Version:2})return null;
        using var policy=OpenWindowsPolicy(Registry.CurrentUser);
        var current=backup.Written.Where(pair=>MatchesPolicyValue(ReadPolicyValue(policy,pair.Key),pair.Value))
            .ToDictionary(pair=>pair.Key,pair=>pair.Value);
        // The URL and token together identify the legacy relay, even if other settings changed.
        if(!current.ContainsKey("inferenceGatewayBaseUrl")||!current.ContainsKey("inferenceGatewayApiKey"))
            return null;
        var changes=ClaudeDesktopPolicy.Restore(current,backup.Written,backup.Original)
            .ToDictionary(pair=>pair.Key,pair=>new PolicyChange(current[pair.Key],pair.Value));
        return ApplyWindowsPolicy(changes);
    }

    private void RestoreLegacyWindowsPermissions()
    {
        var settings=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),".claude","settings.json");
        var backup=Path.Combine(dataDirectory,"ClaudeDesktop","permissions-backup.json");
        new ClaudePermissionSettings(settings,backup).Restore();
    }

    private ClaudePermissionSettings WindowsLaunchPermissions()
    {
        var settings=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),".claude","settings.json");
        var backup=Path.Combine(dataDirectory,"ClaudeDesktop","launch-permissions-backup.json");
        return new ClaudePermissionSettings(settings,backup);
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
        var connection=ConnectionStatus;
        var saved=Configured?Load():null;
        bool unbindPending=saved?.UnbindPending==true;
        return new{configured=saved!=null,running=relay!=null&&!unbindPending,
            deviceAlias=saved?.Device?.Alias,connection.StartedAt,connection.ClientConnected,connection.RequestError,connection.DeviceProblem,unbindPending,
            error=error??(unbindPending?UnbindPendingMessage:null)};
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

    internal async Task<long> PrepareLaunchAsync(CancellationToken ct)
    {
        await gate.WaitAsync(ct);
        try
        {
            if(!Configured||relay==null)throw new InvalidOperationException("请先启用 SafeCode 的 API Key 接入。");
            var saved=Load();
            if(saved.UnbindPending)throw new InvalidOperationException(UnbindPendingMessage);
            if(relay.Port!=saved.Port)
                throw new InvalidOperationException("本机网关连接已中断，请重新启用接入后再打开 Claude Desktop。");
            var library=Library(saved);
            var profile=PrepareProfile(library,saved.Profile,saved.Port,saved.Local);
            await relay.Connection.CheckDeviceAsync(true,ct);
            var available=await CheckLaunchRouteAsync(saved.Port,saved.Local,ct);
            ValidateProfileModels(profile,available);
            if(OperatingSystem.IsWindows())
            {
                if(PlanWindowsPolicy(profile,saved).Changes.Count!=0)
                    throw new InvalidOperationException("Windows 网关策略已变更，请重新启用 SafeCode 接入。");
                WindowsLaunchPermissions().Enable();
            }
            return relay.StartedAt;
        }
        catch(Exception error)when(IsProfileAccessError(error)){throw ProfileError(error);}
        finally{gate.Release();}
    }

    internal static void ValidateProfileModels(JsonObject profile,JsonArray available)
    {
        var names=available.Select(row=>ProfileText(row!.AsObject(),"name")).ToHashSet(StringComparer.Ordinal);
        if(profile["inferenceModels"] is not JsonArray {Count:>0} configured ||
            configured.Any(row=>row is not JsonObject entry || !names.Contains(ProfileText(entry,"name"))))
            throw new InvalidOperationException("Claude 配置里的模型已变更，请在 SafeCode 接入设置中刷新模型列表，再打开 Claude。");
    }

    internal static async Task<JsonArray> CheckLaunchRouteAsync(int port,string bearer,CancellationToken ct)
    {
        using var client=new HttpClient(new HttpClientHandler{AllowAutoRedirect=false,UseProxy=false,UseCookies=false})
            {Timeout=TimeSpan.FromSeconds(10),MaxResponseContentBufferSize=1024*1024};
        using var request=new HttpRequestMessage(HttpMethod.Get,$"http://127.0.0.1:{port}/v1/models");
        request.Headers.Authorization=new AuthenticationHeaderValue("Bearer",bearer);
        request.Headers.Add("X-SafeCode-Launch-Probe","1");
        try
        {
            using var response=await client.SendAsync(request,ct);
            if(response.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden)
                throw new InvalidOperationException("模型查询被拒绝，请检查 Key 权限并重新启用 SafeCode 接入。");
            if(!response.IsSuccessStatusCode)
                throw new InvalidOperationException($"本机转发器查询网关模型时收到 HTTP {(int)response.StatusCode}。请检查 SafeCode 接入后重试。");
            var body=JsonNode.Parse(await response.Content.ReadAsStringAsync(ct));
            if(body is not JsonObject root || root["data"] is not JsonArray rows)
                throw new JsonException();
            try{return ReadModelCatalog(rows).Models;}
            catch(IOException e){throw new InvalidOperationException("网关没有返回可用的聊天模型，请检查 Key 的模型权限。",e);}
            catch(Exception e) when(e is InvalidOperationException or FormatException or InvalidCastException){throw new JsonException("Invalid model",e);}
        }
        catch(JsonException){throw new InvalidOperationException("网关返回的模型目录格式无效，请检查网关地址及登录页重定向。");}
        catch(HttpRequestException)
        {throw new InvalidOperationException("无法通过 SafeCode 本机转发器查询网关模型，请检查接入后重试。");}
        catch(OperationCanceledException) when(!ct.IsCancellationRequested)
        {throw new InvalidOperationException("通过 SafeCode 查询网关模型超时，请检查连接后重试。");}
    }

    internal static JsonObject PrepareProfile(string library,string id,int port,string local)
    {
        if(ProfileText(ReadProfileMeta(library),"appliedId")!=id)
            throw new InvalidOperationException("Claude Desktop 当前选中了其他配置。请重新启用 SafeCode 接入后再打开。");
        var path=Path.Combine(library,id+".json");
        var profile=JsonNode.Parse(File.ReadAllBytes(path)) as JsonObject;
        var credential=profile==null?null:ProfileText(profile,"inferenceGatewayApiKey");
        // Desktop may seal its local bearer after reading the profile.
        if(profile==null||ProfileText(profile,"inferenceProvider")!="gateway"||
            ProfileText(profile,"inferenceGatewayAuthScheme")!="bearer"||
            ProfileText(profile,"inferenceGatewayBaseUrl")!="http://127.0.0.1:"+port||
            (credential!=local && !(credential?.StartsWith("__sealed__:v1:",StringComparison.Ordinal)==true && credential.Length>14)))
            throw new InvalidOperationException("Claude Desktop 接入配置已变更，请重新启用 SafeCode 接入。");
        if(ProfileText(profile,"codeDefaultPermissionMode")!="auto")
        {
            profile["codeDefaultPermissionMode"]="auto";
            WriteProfileFile(path,JsonSerializer.SerializeToUtf8Bytes(profile));
        }
        return profile;
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
                Exception? failure=null;
                var trace=diagnostics?.BeginDiagnostic("desktop_key_restore",background:!interactive);
                try
                {
                    var saved=Load();
                    if(saved.UnbindPending)return Status();
                    return Status(saved.Device==null
                        ?"旧接入未绑定本机设备。为防止把 VPS 目录误认为你的电脑，请重新启用并完成本机授权。"
                        :"请重新启用本机远程控制，验证设备连接后恢复接入。");
                }
                catch(Exception error)
                {
                    failure=error;
                    if(error is OperationCanceledException)throw;
                    return Status(ProfileError(error).Message);
                }
                finally
                {
                    if(trace!=null&&failure!=null)_=diagnostics!.SendDiagnosticAsync(trace,failure,ct);
                }
            }
            return Status();
        }
        finally{gate.Release();}
    }
    internal async Task<object> EnableAsync(string code,CancellationToken ct,bool replace=false,bool useSaved=false,DeviceSetup? verified=null,DeviceBinding? device=null,Func<bool>? hostReady=null,
        bool checkDevice=false,SshSession? deviceSession=null,string savedId="",DevicePath? devicePath=null)
    {
        await gate.WaitAsync(ct);
        try
        {
            var previous=Configured?Load():null;
            if(previous?.UnbindPending==true&&(savedId==""||FindEntry(ReadEntries(),savedId).Remote==previous.Remote))throw new InvalidOperationException(UnbindPendingMessage);
            if(useSaved&&previous==null)throw new InvalidOperationException("没有保存的接入 Key，请输入新的 Key。");
            if(previous!=null&&!replace&&!useSaved)throw new InvalidOperationException("请使用「更换 Key」切换已保存的接入。");
            var t=verified?.Remote??(savedId.Length>0?FindEntry(ReadEntries(),savedId).Remote:useSaved?previous!.Remote:Ticket.Parse(code.Trim()));
            bool controlReady=verified?.HostSession==null?device!=null:device!=null&&hostReady?.Invoke()==true;
            if(verified!=null&&(!controlReady||replace!=verified.Replace||useSaved!=verified.UseSaved||
               useSaved&&t!=previous!.Remote))
                throw new InvalidOperationException("接入配置已变更，请重新验证设备。");
            using var client=Remote(t);using var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeout.CancelAfter(TimeSpan.FromSeconds(45));
            var (models,classifier)=await FetchModelCatalogAsync(client,timeout.Token);
            if(checkDevice)
            {
                var priorDevice=previous?.Remote==t?previous.Device:null;
                if(priorDevice?.DeviceId.StartsWith("ssh-",StringComparison.Ordinal)==true)
                {
                    if(deviceSession!=null&&await deviceSession.CanReuseHostControlForKeyAsync(t,timeout.Token))
                        return new DeviceSetup(t,replace,useSaved,null,deviceSession);
                    throw new InvalidOperationException("请连接签发此 Key 的 VPS，恢复已授权的本机远程控制。");
                }
                // The catalog response can close its TLS connection before device validation.
                using var deviceClient=Remote(t);
                var capability=await RequestDeviceControlAsync(deviceClient,new{action="capability"},timeout.Token);
                if(capability is {} available)
                {
                    if(!available.TryGetProperty("httpsTunnel",out var tunnel)||tunnel.ValueKind!=JsonValueKind.True)
                        throw new InvalidOperationException("此网关尚未支持通过 HTTPS 建立本机远程控制。");
                    return new DeviceSetup(t,replace,useSaved,priorDevice,Switching:savedId.Length>0&&previous!=null&&previous.Remote!=t);
                }
                if(priorDevice!=null)throw new InvalidOperationException("该 Key 的设备授权已不可用，请联系网关管理员。");
            }
            string lib=Library(previous),profileId=previous?.Profile??Guid.NewGuid().ToString();
            string metaPath=Path.Combine(lib,"_meta.json"),profilePath=Path.Combine(lib,profileId+".json");
            byte[]? originalMeta=ReadOptionalFile(metaPath),originalProfile=ReadOptionalFile(profilePath);
            var meta=ParseProfileMeta(originalMeta);
            if(OperatingSystem.IsMacOS())RemoveLegacyMacProfiles(meta,lib);
            var entries=meta["entries"]!.AsArray();
            string applied=(string?)meta["appliedId"]??"";
            bool wasApplied=previous!=null&&applied==profileId;
            string local=previous?.Local??Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
            if(hostReady?.Invoke()==false)throw new InvalidOperationException("本机远程控制连接已中断，请重新验证。");
            var next=await Listen(t,local,0,ct,device,hostReady,devicePath);
            Action? undoPolicy=null,undoLegacyPolicy=null;
            bool profileWritten=false,metaWritten=false;
            try
            {
                var s=new Saved(t,local,next.Port,profileId,wasApplied?previous!.Previous:applied,
                    (wasApplied?previous!.Hybrid:meta["hybridPointer"])?.DeepClone(),wasApplied?previous!.CreatedMeta:originalMeta==null,Device:device);
                var profile=new JsonObject{["inferenceProvider"]="gateway",["inferenceGatewayBaseUrl"]="http://127.0.0.1:"+next.Port,
                    ["inferenceGatewayAuthScheme"]="bearer",["inferenceCredentialKind"]="static",["inferenceGatewayApiKey"]=local,
                    ["deploymentMode"]="3p",["disableDeploymentModeChooser"]=true,["deploymentDisplayName"]="SafeCode",
                    ["inferenceModels"]=models,["modelDiscoveryEnabled"]=false,["autoModeEnabled"]=true,
                    ["codeDefaultPermissionMode"]="auto",["chatTabEnabled"]=true};
                if(classifier!=null)SetClassifier(profile,classifier);
                if(OperatingSystem.IsWindows())
                {
                    undoLegacyPolicy=RemoveLegacyWindowsPolicy();
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
                if(undoLegacyPolicy!=null)RestoreLegacyWindowsPermissions();
                if(device!=null)
                {
                    var storedKeys=ReadEntries();
                    var index=storedKeys.FindIndex(e=>e.Remote==t);
                    if(index<0)storedKeys.Add(NewEntry(t,new Uri(t.Url).Authority,true));
                    else storedKeys[index]=storedKeys[index] with {Authorized=true};
                    WriteEntries(storedKeys);
                }
                Save(s);
            }
            catch
            {
                try{undoPolicy?.Invoke();}
                finally
                {
                    try{undoLegacyPolicy?.Invoke();}
                    finally
                    {
                        try{if(metaWritten)RestoreFile(metaPath,originalMeta);}
                        finally
                        {
                            try{if(profileWritten)RestoreFile(profilePath,originalProfile);}
                            finally{await next.DisposeAsync();}
                        }
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

    internal async Task<object> RefreshModelsAsync(CancellationToken ct)
    {
        await gate.WaitAsync(ct);
        try
        {
            if(relay==null||!Configured)throw new InvalidOperationException("请先启用 API Key 接入。");
            var saved=Load();
            using var client=Remote(saved.Remote);
            using var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeout.CancelAfter(TimeSpan.FromSeconds(20));
            var (models,classifier)=await FetchModelCatalogAsync(client,timeout.Token);

            string library=Library(saved), path=Path.Combine(library,saved.Profile+".json");
            var original=File.ReadAllBytes(path);
            var profile=JsonNode.Parse(original) as JsonObject;
            if((string?)ReadProfileMeta(library)["appliedId"]!=saved.Profile||
                (string?)profile?["inferenceGatewayBaseUrl"]!="http://127.0.0.1:"+saved.Port)
                throw new InvalidOperationException("Claude Desktop 已切换接入配置，请重新启用 Safe Code API Key。");

            profile!["inferenceModels"]=models;
            profile.Remove("autoModeClassifierModel");
            if(classifier!=null)SetClassifier(profile,classifier);
            else if(profile["claudeCodeSettings"]?["env"] is JsonObject env)
                env.Remove("CLAUDE_CODE_AUTO_MODE_MODEL");
            Action? undoPolicy=null;
            bool profileWritten=false;
            try
            {
                if(OperatingSystem.IsWindows())
                {
                    var (owned,changes)=PlanWindowsPolicy(profile,saved);
                    undoPolicy=ApplyWindowsPolicy(changes);
                    saved=saved with{WindowsPolicy=owned};
                }
                WriteProfileFile(path,JsonSerializer.SerializeToUtf8Bytes(profile));
                profileWritten=true;
                if(undoPolicy!=null)Save(saved);
            }
            catch
            {
                try{if(profileWritten)WriteProfileFile(path,original);}
                finally{undoPolicy?.Invoke();}
                throw;
            }
            return Status();
        }
        catch(Exception error)when(IsProfileAccessError(error)){throw ProfileError(error);}
        finally{gate.Release();}
    }

    private static async Task<(JsonArray Models,string? Classifier)> FetchModelCatalogAsync(HttpClient client,CancellationToken ct)
    {
        using var response=await VerifyRemoteAsync(client,ct);
        if(response.StatusCode==HttpStatusCode.Unauthorized)throw new InvalidOperationException("Key 无效、已过期或已撤销。");
        response.EnsureSuccessStatusCode();
        var catalog=JsonNode.Parse(await response.Content.ReadAsStringAsync(ct))?["data"]?.AsArray()
            ??throw new IOException("模型列表无效。");
        return ReadModelCatalog(catalog);
    }

    private static readonly Regex ModelId=new(@"\A[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}(?:\[1m\])?\z");
    private static readonly string[] EffortDescending=["max","xhigh","high","medium","low"];
    internal static (JsonArray Models,string? Classifier) ReadModelCatalog(JsonArray catalog)
    {
        var models=new JsonArray();string? classifier=null;
        foreach(var m in catalog)
        {
            string? id=(string?)m?["id"];if(id==null||!ModelId.IsMatch(id))continue;
            if((bool?)m?["auto_mode_classifier"]==true){classifier=id;continue;}
            string label=(string?)m?["display_name"]??id;
            string? alias=(string?)m?["desktop_model_id"];
            var entry=new JsonObject{["name"]=alias!=null&&ModelId.IsMatch(alias)?alias:id,["labelOverride"]=label};
            foreach(var (src,dst) in new[]{("supports_1m","supports1m"),("prefer_1m","prefer1m"),("anthropic_family_tier","anthropicFamilyTier"),("is_family_default","isFamilyDefault"),("behaves_as","behavesAs")})
                if(m?[src] is {} value)entry[dst]=value.DeepClone();
            if(m?["supported_effort_levels"] is JsonArray levels &&
                Array.Find(EffortDescending,effort=>levels.Any(level=>level is JsonValue value&&
                    value.TryGetValue<string>(out var supported)&&supported==effort)) is {} maximum)
                entry["maxEffort"]=maximum;
            models.Add(entry);
            if((string?)m?["desktop_ultracode_model_id"] is {} ultracode&&ModelId.IsMatch(ultracode))
            {
                var workflow=new JsonObject{["name"]=ultracode,["labelOverride"]=label+" · Ultracode",["maxEffort"]="xhigh"};
                if(m?["ultracode_supports_1m"] is {} extended)workflow["supports1m"]=extended.DeepClone();
                if(entry["prefer1m"] is {} preferred)workflow["prefer1m"]=preferred.DeepClone();
                if(entry["behavesAs"] is {} resolved)workflow["behavesAs"]=resolved.DeepClone();
                models.Add(workflow);
            }
        }
        if(models.Count==0)throw new IOException("远端 CLI 未提供可用模型。");
        return (models,classifier);
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
            if(relay!=null)
            {
                var current=relay;
                relay=null;
                await current.DisposeAsync();
            }
            if(Configured)
            {
                var s=Load();
                if(s.Device is {} binding)
                {
                    if(!s.UnbindPending)
                    {
                        s=s with { UnbindPending=true };
                        Save(s);
                    }
                    try
                    {
                        var response=await DeviceControlAsync(s.Remote,
                            new{action="revoke",binding.DeviceId,binding.DeviceProof},ct);
                        if(response.ValueKind!=JsonValueKind.Object||
                           !response.TryGetProperty("revoked",out var revoked)||revoked.ValueKind!=JsonValueKind.True)
                            return Status("网关返回的解绑确认无效，请核对网关版本后重试。");
                    }
                    catch(Exception error) when(error is IOException or HttpRequestException or InvalidOperationException
                                                or OperationCanceledException or JsonException)
                    {
                        return Status("解绑未确认："+error.Message);
                    }
                }
                var lib=Library(s);
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
            if(OperatingSystem.IsWindows())WindowsLaunchPermissions().Restore();
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
