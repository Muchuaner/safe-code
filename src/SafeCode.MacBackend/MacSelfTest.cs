using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using SafeCode.Core;
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
            await access.PrepareLaunchAsync(CancellationToken.None);
            if (File.ReadAllText(otherPath)!=otherContents || JsonNode.Parse(File.ReadAllText(activePath))?["codeDefaultPermissionMode"]!=null)
                throw new Exception("打开 Claude Desktop 时改写了非当前 Safe Code 配置。");
            meta["appliedId"] = tokenId;
            meta["entries"]!.AsArray().Add(new JsonObject { ["id"] = tokenId, ["name"] = "SafeCode" });
            DesktopKeyAccess.WriteProfileFile(metaPath,JsonSerializer.SerializeToUtf8Bytes(meta));
            await access.PrepareLaunchAsync(CancellationToken.None);
            string prepared = File.ReadAllText(activePath);
            var preparedProfile = JsonNode.Parse(prepared);
            if ((string?)preparedProfile?["codeDefaultPermissionMode"]!="auto" ||
                (string?)preparedProfile?["inferenceGatewayApiKey"]!="fixture-local-token")
                throw new Exception("打开 Claude Desktop 前未保留原接入并设为 Auto。");
            await access.PrepareLaunchAsync(CancellationToken.None);
            if (File.ReadAllText(activePath)!=prepared)
                throw new Exception("重复打开 Claude Desktop 改写了相同配置。");
            Console.WriteLine("MAC_CLAUDE_LAUNCH_AUTO_DEFAULT_OK");
        }
        finally { Directory.Delete(profileTest, true); }
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
}
