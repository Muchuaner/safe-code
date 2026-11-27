using Microsoft.AspNetCore.Http;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using SafeCode.Core;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.Extensions.Logging;
namespace SafeCode.Desktop;
internal static class MacSelfTest
{
    public static async Task RunAsync()
    {
        LocalSecrets.Initialize(RandomNumberGenerator.GetBytes(32));byte[] plain=Encoding.UTF8.GetBytes("fixture-password-not-a-real-secret");byte[] cipher=LocalSecrets.Protect(plain);
        if(!LocalSecrets.Unprotect(cipher).SequenceEqual(plain))throw new Exception("加密保存往返失败。");
        cipher[^1]^=1;try{LocalSecrets.Unprotect(cipher);throw new Exception("接受了被改写的密文。");}catch(CryptographicException){}
        if(MacClients.Quote("a'b")!="'a'\\''b'")throw new Exception("Shell 参数转义失败。");
        string Feed(string suffix)=>JsonSerializer.Serialize(new{tag_name="Beta-0.4.4.2",draft=false,prerelease=true,assets=new[]{new{name="SafeCode-Beta-0.4.4.2-"+suffix,browser_download_url="https://github.com/Muchuaner/safe-code/releases/download/Beta-0.4.4.2/SafeCode-Beta-0.4.4.2-"+suffix,digest="sha256:"+new string('a',64),size=123}}});
        using(var win=JsonDocument.Parse(Feed("Setup-x64.exe")))if(ClientServices.ParseRelease(win.RootElement,"Beta-0.4.4.1","macOS-arm64.dmg").CanInstall)throw new Exception("Mac 更新误选 Windows 安装包。");
        using(var mac=JsonDocument.Parse(Feed("macOS-arm64.dmg")))if(!ClientServices.ParseRelease(mac.RootElement,"Beta-0.4.4.1","macOS-arm64.dmg").CanInstall||ClientServices.ParseRelease(mac.RootElement,"Beta-0.4.4.1","macOS-x64.dmg").CanInstall)throw new Exception("架构筛选失败。");
        var catalog=JsonNode.Parse("""
            [
                {"id":"default","display_name":"Default (recommended)","desktop_model_id":"safecode-cloudcli-default"},
                {"id":"opus","display_name":"Opus","anthropic_family_tier":"opus","behaves_as":"claude-fixture-opus",
                 "supported_effort_levels":["max","low","ultracode"],"supports_1m":true,
                 "desktop_model_id":"safecode-cloudcli-opus","desktop_ultracode_model_id":"safecode-ultracode/opus","ultracode_supports_1m":true},
                {"id":"vendor/custom-model","supported_effort_levels":["high","low"]},
                {"id":"opusplan","desktop_model_id":"safecode-cloudcli-opusplan"},
                {"id":"claude-fixture-classifier","auto_mode_classifier":true}
            ]
            """)!.AsArray();
        var (models,classifier)=DesktopKeyAccess.ReadModelCatalog(catalog);
        string[] names=["safecode-cloudcli-default","safecode-cloudcli-opus","safecode-ultracode/opus",
            "vendor/custom-model","safecode-cloudcli-opusplan"];
        if(!models.Select(model=>(string?)model?["name"]).SequenceEqual(names)||
           (string?)models[1]!["maxEffort"]!="max"||(bool?)models[1]!["supports1m"]!=true||
           (string?)models[2]!["maxEffort"]!="xhigh"||(bool?)models[2]!["supports1m"]!=true||
           (string?)models[2]!["behavesAs"]!="claude-fixture-opus"||
           (string?)models[3]!["maxEffort"]!="high"||classifier!="claude-fixture-classifier")
            throw new Exception("Claude Desktop API Key 模型、思考强度或分类器配置错误。");
        using(var transport=DesktopKeyAccess.RemoteHandler(new("https://gateway.example",new string('0',64),new string('a',43))))
            if(transport.UseProxy)throw new Exception("固定证书网关不应经过系统代理的 CONNECT 隧道。");
        Console.WriteLine("MAC_PINNED_GATEWAY_DIRECT_TRANSPORT_OK");
        Console.WriteLine("MAC_CLAUDE_PROFILE_CHAT_AND_CLASSIFIER_CONFIGURATION_OK");
        string profileTest = Directory.CreateTempSubdirectory("safecode-mac-profile-").FullName;
        try
        {
            string library = Path.Combine(profileTest, "configLibrary");
            string sealedId = Guid.NewGuid().ToString("D");
            string tokenId = Guid.NewGuid().ToString("D");
            string otherId = Guid.NewGuid().ToString("D");
            var meta = new JsonObject
            {
                ["appliedId"] = sealedId,
                ["entries"] = new JsonArray(
                    new JsonObject { ["id"] = sealedId, ["name"] = "SafeCode SSH" },
                    new JsonObject { ["id"] = tokenId, ["name"] = "SafeCode SSH" },
                    new JsonObject { ["id"] = otherId, ["name"] = "SafeCode SSH" })
            };
            var profile = new JsonObject
            {
                ["inferenceGatewayBaseUrl"] = "http://127.0.0.1:18899",
                ["inferenceGatewayApiKey"] = "__sealed__:v1:fixture-only",
                ["inferenceProvider"] = "gateway",
                ["inferenceGatewayAuthScheme"] = "bearer",
                ["inferenceCredentialKind"] = "static",
                ["deploymentDisplayName"] = "SafeCode"
            };
            void WriteProfile(string id) => DesktopKeyAccess.WriteProfileFile(
                Path.Combine(library,id+".json"), JsonSerializer.SerializeToUtf8Bytes(profile));
            WriteProfile(sealedId);
            profile["inferenceGatewayApiKey"] = "fixture-local-token";
            WriteProfile(tokenId);
            profile["inferenceGatewayBaseUrl"] = "https://other.example";
            WriteProfile(otherId);
            using var access = new DesktopKeyAccess(profileTest,LocalSecrets.Protect,LocalSecrets.Unprotect,library);
            string originalMeta = meta.ToJsonString();
            void SaveBetaState(string id, int port)
            {
                var state = new { Server = new { Host = "gateway.example" }, Id = id, Token = "fixture-local-token", Port = port };
                DesktopKeyAccess.WriteProfileFile(Path.Combine(profileTest,"desktop-ssh.aes"),
                    LocalSecrets.Protect(JsonSerializer.SerializeToUtf8Bytes(state)));
            }
            access.RemoveLegacyMacProfiles(meta,library);
            SaveBetaState(sealedId, 18900);
            access.RemoveLegacyMacProfiles(meta,library);
            if(meta.ToJsonString()!=originalMeta)throw new Exception("缺少匹配的本机状态时改动了旧配置。");
            SaveBetaState(tokenId, 18899);
            access.RemoveLegacyMacProfiles(meta,library);
            if((string?)meta["appliedId"]!=sealedId || meta["entries"]!.AsArray().Count!=2 ||
                meta["entries"]!.AsArray().Any(entry=>(string?)entry?["id"]==tokenId))
                throw new Exception("迁移明文配置时误动了同名加密配置。");
            SaveBetaState(sealedId, 18899);
            access.RemoveLegacyMacProfiles(meta,library);
            if ((string?)meta["appliedId"]!="" || (string?)meta["entries"]!.AsArray().Single()!["id"]!=otherId)
                throw new Exception("未迁移有匹配本机记录的加密 SSH 配置。");
            Console.WriteLine("MAC_LEGACY_SSH_PROFILE_MIGRATION_OK");

            var savedKey = new
            {
                Remote = new PublicGatewayTicket("https://gateway.example", new string('0',64), new string('a',43)),
                Local = "fixture-local-token", Port = 18899, Profile = tokenId, Previous = otherId
            };
            DesktopKeyAccess.WriteProfileFile(Path.Combine(profileTest,"desktop-key.state"),
                LocalSecrets.Protect(JsonSerializer.SerializeToUtf8Bytes(savedKey,new JsonSerializerOptions(JsonSerializerDefaults.Web))));
            string metaPath = Path.Combine(library,"_meta.json");
            string activePath = Path.Combine(library,tokenId+".json");
            string otherPath = Path.Combine(library,otherId+".json");
            string otherContents = File.ReadAllText(otherPath);
            meta["appliedId"] = otherId;
            DesktopKeyAccess.WriteProfileFile(metaPath,JsonSerializer.SerializeToUtf8Bytes(meta));
            try
            {
                DesktopKeyAccess.PrepareProfile(library,tokenId,18899,"fixture-local-token");
                throw new Exception("启动时接受了非当前 SafeCode 配置。");
            }
            catch(InvalidOperationException){}
            if (File.ReadAllText(otherPath)!=otherContents || JsonNode.Parse(File.ReadAllText(activePath))?["codeDefaultPermissionMode"]!=null)
                throw new Exception("打开 Claude Desktop 时改写了非当前 Safe Code 配置。");
            meta["appliedId"] = tokenId;
            meta["entries"]!.AsArray().Add(new JsonObject { ["id"] = tokenId, ["name"] = "SafeCode" });
            DesktopKeyAccess.WriteProfileFile(metaPath,JsonSerializer.SerializeToUtf8Bytes(meta));
            DesktopKeyAccess.PrepareProfile(library,tokenId,18899,"fixture-local-token");
            string prepared = File.ReadAllText(activePath);
            var preparedProfile = JsonNode.Parse(prepared);
            if ((string?)preparedProfile?["codeDefaultPermissionMode"]!="auto" ||
                (bool?)preparedProfile?["inferenceGatewayAutoModeServerChecks"] != false ||
                (bool?)preparedProfile?["autoModeEnabled"] != true ||
                (string?)preparedProfile?["inferenceGatewayApiKey"]!="fixture-local-token")
                throw new Exception("打开 Claude Desktop 前未保留原接入并设为 Auto。");
            DesktopKeyAccess.PrepareProfile(library,tokenId,18899,"fixture-local-token");
            if (File.ReadAllText(activePath)!=prepared)
                throw new Exception("重复打开 Claude Desktop 改写了相同配置。");
            Console.WriteLine("MAC_CLAUDE_LAUNCH_AUTO_DEFAULT_OK");
        }
        finally { Directory.Delete(profileTest, true); }
        using (var client = new HttpClient(new HistoryRetryFixture()) { BaseAddress = new Uri("https://history.invalid") })
        using (var history = await DesktopWebHistory.GetAsync(client,"/v1/desktop-history",false,CancellationToken.None))
        {
            if (history?.RootElement.GetProperty("developer").GetBoolean() != true)
                throw new Exception("历史同步重试后没有读到原响应。");
        }
        Console.WriteLine("MAC_HISTORY_RETRIES_TRANSIENT_READS_OK");
        CheckMirroredHistoryMigration();
        await CheckKeyHistoryAsync();
        string fingerprint=new('A',64);
        var rdp=MacClients.FreeRdpArguments(new(22119,"fixture",fingerprint),new("workspace","fixture password"),
            new(2560,1600),RdpResizeMode.Dynamic,true);
        if(!rdp.Contains("/cert:deny,fingerprint:sha256:"+fingerprint)||!rdp.Contains("/p:fixture password")||
           !rdp.Contains("/f")||!rdp.Contains("/dynamic-resolution")||rdp.Contains("/smart-sizing"))
            throw new Exception("Mac RDP 指纹和动态分辨率参数错误。");
        var scaled=MacClients.FreeRdpArguments(new(22119,"fixture",fingerprint),new("workspace","fixture password"),
            new(1920,1080),RdpResizeMode.Scale,false);
        if(!scaled.Contains("/smart-sizing")||scaled.Contains("/dynamic-resolution")||scaled.Contains("/f"))
            throw new Exception("Mac RDP 固定分辨率缩放参数错误。");
        Console.WriteLine("MAC_RDP_PINNED_CERTIFICATE_AND_DISPLAY_MODES_OK");
        Console.WriteLine("MAC_AES_GCM_TAMPER_SHELL_QUOTING_AND_PLATFORM_UPDATE_SELECTION_OK");
        string folder=Directory.CreateTempSubdirectory("safecode-host-resume-").FullName;
        try
        {
            var saved=new GatewayPreference(true,"vps.example",2222,"operator","SHA256:fixture");
            var settings=new ConnectionSettings("vps.example","operator","fixture",2222);
            string preference=Path.Combine(folder,"host-control-resume.aes");
            var bytes=JsonSerializer.SerializeToUtf8Bytes(new{Server=saved,WorkspaceUser="work",Alias="fixture",Client=new string('a',20)});
            DesktopKeyAccess.WriteProfileFile(preference,LocalSecrets.Protect(bytes));
            using var host=new MacHostControl(folder);
            if(!host.CanResume(settings)||host.CanResume(settings with{Port=22})||
                host.CanResume(settings with{Username="another"})||host.CanResume(settings with{Host="different.example"})||
                saved.Matches(settings,"SHA256:different"))
                throw new Exception("本机控制的恢复范围有误。");
            await host.SuspendAsync(CancellationToken.None);
            if(!host.CanResume(settings))throw new Exception("安全退出丢失本机控制恢复设置。");
            await host.DisableAsync(CancellationToken.None);
            if(host.CanResume(settings)||File.Exists(preference))throw new Exception("停用后保留了本机控制授权。");
            Console.WriteLine("MAC_HOST_CONTROL_RESUME_SCOPE_AND_OPT_OUT_OK");
        }
        finally{Directory.Delete(folder,true);}
    }

    private static void CheckMirroredHistoryMigration()
    {
        using var directory = new TempMirrorDirectory();
        var scope = Guid.NewGuid().ToString("D");
        var staged = Path.Combine(directory.Path,scope+".jsonl");
        var meta = Path.Combine(directory.Path,"local_"+scope+".json");
        string user = Guid.NewGuid().ToString("D"), tool = Guid.NewGuid().ToString("D"), assistant = Guid.NewGuid().ToString("D");
        string Line(string id, string? parent, string role, object content) => JsonSerializer.Serialize(new {
            uuid=id,parentUuid=parent,sessionId=scope,type=role,userType="external",
            message=new {id="msg_"+id.Replace("-",""),role,content,stop_reason="end_turn",usage=new {input_tokens=0,output_tokens=0}}
        });
        File.WriteAllLines(staged,new[] {
            Line(user,null,"user","Keep this user message."),
            Line(tool,user,"assistant",new[] {new {type="text",text="**TaskStop**\n{\"isError\":false,\"toolUseResult\":{}}"}}),
            Line(assistant,tool,"assistant",new[] {new {type="text",text="Keep this answer."}})
        });
        File.WriteAllText(meta,JsonSerializer.Serialize(new {permissionMode="default",completedTurns=3,lastAssistantUuid=assistant}));
        DesktopWebHistory.MigrateMirror(meta,directory.Path,scope,directory.Path);
        var frames = File.ReadLines(staged).Select(row=>JsonNode.Parse(row)!).ToArray();
        var updated = JsonNode.Parse(File.ReadAllText(meta))!;
        if (frames.Length != 2 || (string?)frames[1]["uuid"] != assistant ||
            (string?)frames[1]["parentUuid"] != user || (string?)updated["permissionMode"] != "auto" ||
            (int?)updated["completedTurns"] != 3)
            throw new Exception("历史更新未保留原消息、会话信息及 Auto 权限模式。");
        Console.WriteLine("MAC_MIRRORED_HISTORY_HIDES_TOOLS_AND_PRESERVES_TURNS_OK");
    }

    private sealed class TempMirrorDirectory : IDisposable
    {
        internal string Path { get; } = Directory.CreateTempSubdirectory("safecode-mirror-format-").FullName;
        public void Dispose() => Directory.Delete(Path,true);
    }

    private sealed class HistoryRetryFixture : HttpMessageHandler
    {
        private int attempts;

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct) =>
            ++attempts switch
            {
                1 => throw new HttpRequestException(HttpRequestError.ConnectionError,"Fixture connection reset"),
                2 => Task.FromResult(new HttpResponseMessage(HttpStatusCode.BadGateway)),
                3 => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
                    { Content=new StringContent("{\"developer\":true}") }),
                _ => throw new InvalidOperationException("Too many history requests")
            };
    }

    private static async Task CheckKeyHistoryAsync()
    {
        using var key=RSA.Create(2048);
        var request=new CertificateRequest("CN=localhost",key,HashAlgorithmName.SHA256,RSASignaturePadding.Pkcs1);
        using var certificate=request.CreateSelfSigned(DateTimeOffset.UtcNow.AddMinutes(-1),DateTimeOffset.UtcNow.AddMinutes(5));
        var builder=WebApplication.CreateSlimBuilder();
        builder.Logging.ClearProviders();
        builder.WebHost.ConfigureKestrel(options=>options.Listen(IPAddress.Loopback,0,listener=>listener.UseHttps(certificate)));
        await using var gateway=builder.Build();
        gateway.MapGet("/v1/cloudcli/models",()=>new {data=new[]{new {id="claude-fixture"}}});
        gateway.MapGet("/v1/models",()=>new {data=new[]{new {id="claude-fixture"}}});
        gateway.MapPost("/v1/cloudcli/messages", () => Results.StatusCode(503));
        await gateway.StartAsync();
        string directory=Directory.CreateTempSubdirectory("safecode-key-history-").FullName;
        try
        {
            string library=Path.Combine(directory,"configLibrary");
            var json=new JsonSerializerOptions(JsonSerializerDefaults.Web);
            var pin=Convert.ToHexStringLower(SHA256.HashData(certificate.RawData));
            var first=new PublicGatewayTicket(gateway.Urls.Single(),pin,new string('a',43));
            var second=first with {Key=new string('b',43)};
            string Encode(PublicGatewayTicket ticket)=>"sc1."+Convert.ToBase64String(JsonSerializer.SerializeToUtf8Bytes(ticket,json))
                .TrimEnd('=').Replace('+','-').Replace('/','_');
            string legacyProfile=Guid.NewGuid().ToString("D");
            using var portCheck=new TcpListener(IPAddress.Loopback,0);
            portCheck.Start();
            int originalPort=((IPEndPoint)portCheck.LocalEndpoint).Port;
            portCheck.Stop();
            DesktopKeyAccess.WriteProfileFile(Path.Combine(directory,"desktop-key.state"),
                LocalSecrets.Protect(JsonSerializer.SerializeToUtf8Bytes(
                    new {Remote=first,Local="fixture-local",Port=originalPort,Profile=legacyProfile,Previous=""},json)));
            using var access=new DesktopKeyAccess(directory,LocalSecrets.Protect,LocalSecrets.Unprotect,library);
            using var deadline=new CancellationTokenSource(TimeSpan.FromSeconds(20));
            string CurrentHistory()
            {
                string id=(string)DesktopKeyAccess.ReadProfileMeta(library)["appliedId"]!;
                var profile=JsonNode.Parse(File.ReadAllText(Path.Combine(library,id+".json")))!;
                if((bool?)profile["claudeAiImport"]?["automatic3pImport"]!=false)
                    throw new Exception("切换 Key 时允许了共享历史的自动导入。");
                return (string)profile["deploymentOrganizationUuid"]!;
            }
            await access.EnableAsync("",deadline.Token,useSaved:true);
            async Task WaitForConnectivity(bool expected)
            {
                using var wait = CancellationTokenSource.CreateLinkedTokenSource(deadline.Token);
                wait.CancelAfter(TimeSpan.FromSeconds(7));
                while (access.ConnectionStatus.GatewayConnected != expected)
                    await Task.Delay(50, wait.Token);
            }

            // No call to the Desktop relay is needed for the first green state.
            await WaitForConnectivity(true);
            if (await DesktopKeyAccess.CheckGatewayHandshakeAsync(new Uri(first.Url), new byte[32], deadline.Token))
                throw new Exception("网关证书指纹不匹配时仍报告连通。");
            var firstHistory=CurrentHistory();
            if(firstHistory!=legacyProfile)throw new Exception("已接入 Key 的历史标识没有保留。");
            var savedProfile=Path.Combine(library,legacyProfile+".json");
            var originalProfile=JsonNode.Parse(File.ReadAllText(savedProfile))!;
            var endpoint=new Uri((string)originalProfile["inferenceGatewayBaseUrl"]!);
            var bearer=(string)originalProfile["inferenceGatewayApiKey"]!;
            using (var client = new HttpClient { BaseAddress = endpoint })
            {
                client.DefaultRequestHeaders.Authorization = new("Bearer", bearer);
                using var failure = await client.PostAsync("/v1/messages", new StringContent("{}"), deadline.Token);
                if ((int)failure.StatusCode != 503 || !access.ConnectionStatus.GatewayConnected)
                    throw new Exception("聊天失败影响了网关连通状态。");
            }
            Console.WriteLine("MAC_GATEWAY_PINNED_TLS_WITHOUT_CHAT_OK");
            await DesktopKeyAccess.CheckLaunchRouteAsync(endpoint.Port,bearer,deadline.Token);
            await access.StopAsync();

            // Simulate a SafeCode restart while Desktop keeps its original URL and bearer.
            using(var recovered=new DesktopKeyAccess(directory,LocalSecrets.Protect,LocalSecrets.Unprotect,library))
            {
                using(var blocker=new TcpListener(IPAddress.Loopback,endpoint.Port))
                {
                    blocker.Start();
                    try
                    {
                        await recovered.EnableAsync("",deadline.Token,useSaved:true);
                        throw new Exception("已保存的本机端口被占用时更改了接入地址。");
                    }
                    catch(InvalidOperationException error)when(error.Message.Contains("已被占用"))
                    {
                        if(!JsonNode.DeepEquals(originalProfile,JsonNode.Parse(File.ReadAllText(savedProfile))))
                            throw new Exception("接入失败时覆盖了桌面原有配置。");
                    }
                }
                await recovered.EnableAsync("",deadline.Token,useSaved:true);
                var models=await DesktopKeyAccess.CheckLaunchRouteAsync(endpoint.Port,bearer,deadline.Token);
                if(models.Count!=1||CurrentHistory()!=firstHistory)
                    throw new Exception("恢复同一把 Key 后，原桌面地址或历史失效。");
            }
            Console.WriteLine("MAC_DESKTOP_RELAY_RESTART_PRESERVES_ENDPOINT_OK");
            await access.DisableAsync(deadline.Token);
            await access.EnableAsync(Encode(second),deadline.Token);
            var secondHistory=CurrentHistory();
            if(secondHistory==firstHistory)throw new Exception("同一个网关的不同 Key 共用了历史标识。");
            await access.DisableAsync(deadline.Token);
            await access.EnableAsync(Encode(first),deadline.Token);
            if(CurrentHistory()!=firstHistory)throw new Exception("切回原 Key 没有加载原来的历史标识。");
            await WaitForConnectivity(true);
            await gateway.StopAsync();
            await WaitForConnectivity(false);
            Console.WriteLine("MAC_GATEWAY_DISCONNECT_DETECTED_OK");
            Console.WriteLine("MAC_DESKTOP_KEY_HISTORY_ISOLATION_AND_RESTORE_OK");
        }
        finally { await gateway.StopAsync(); Directory.Delete(directory,true); }
    }
}
