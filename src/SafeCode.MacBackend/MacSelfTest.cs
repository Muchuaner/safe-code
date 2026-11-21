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
        var profile=MacClaudeDesktopBridge.MakeProfile("http://127.0.0.1:18899","fixture-local-token",
            [new("claude-fixture-opus","Opus",["low","high"],true,"opus"),
             new("claude-fixture-classifier","Classifier",["low"],false,null,true)]);
        var model=profile["inferenceModels"]!.AsArray().Single()!;
        if((string?)model["name"]!="claude-fixture-opus"||(string?)model["maxEffort"]!="high"||
           (string?)profile["claudeCodeSettings"]?["env"]?["CLAUDE_CODE_AUTO_MODE_MODEL"]!="claude-fixture-classifier")
            throw new Exception("Mac Claude Desktop 模型和分类器配置错误。");
        Console.WriteLine("MAC_CLAUDE_PROFILE_CHAT_AND_CLASSIFIER_CONFIGURATION_OK");
        string profileTest = Directory.CreateTempSubdirectory("safecode-mac-profile-").FullName;
        try
        {
            string library = Path.Combine(profileTest, "configLibrary");
            string id = Guid.NewGuid().ToString("D");
            string profilePath = Path.Combine(library, id + ".json");
            string metaPath = Path.Combine(library, "_meta.json");
            string statePath = Path.Combine(profileTest, "desktop-ssh.aes");
            var meta = new JsonObject
            {
                ["appliedId"] = id,
                ["entries"] = new JsonArray(new JsonObject { ["id"] = id, ["name"] = "SafeCode SSH" })
            };
            profile["inferenceGatewayApiKey"] = "__sealed__:v1:fixture-only";
            DesktopKeyAccess.WriteProfileFile(profilePath, JsonSerializer.SerializeToUtf8Bytes(profile));
            DesktopKeyAccess.WriteProfileFile(metaPath, JsonSerializer.SerializeToUtf8Bytes(meta));
            using var bridge = new MacClaudeDesktopBridge(profileTest, false, library);
            var orphan = JsonSerializer.SerializeToElement(await bridge.RefreshAsync(CancellationToken.None));
            if (orphan.GetProperty("oldProfile").GetString() != id)
                throw new Exception("未识别旧版遗留的 SafeCode SSH 配置。");
            meta["hybridPointer"] = "fixture";
            DesktopKeyAccess.WriteProfileFile(metaPath, JsonSerializer.SerializeToUtf8Bytes(meta));
            var protectedProfile = JsonSerializer.SerializeToElement(await bridge.RefreshAsync(CancellationToken.None));
            if (protectedProfile.GetProperty("oldProfile").ValueKind != JsonValueKind.Null)
                throw new Exception("将 Claude 混合模式误判为可替换的旧配置。");
            meta.Remove("hybridPointer");
            DesktopKeyAccess.WriteProfileFile(metaPath, JsonSerializer.SerializeToUtf8Bytes(meta));

            var state = new { Server = new GatewayPreference(), Id = id, Token = "fixture-local-token", Port = 18899 };
            DesktopKeyAccess.WriteProfileFile(statePath, LocalSecrets.Protect(JsonSerializer.SerializeToUtf8Bytes(state)));
            await bridge.StopAsync(CancellationToken.None);
            var after = DesktopKeyAccess.ReadProfileMeta(library);
            if (File.Exists(profilePath) || File.Exists(statePath) || after["appliedId"]?.GetValue<string>() != "")
                throw new Exception("停止 SSH 接入后遗留了加密过的本机配置。");
            if (MacClaudeDesktopBridge.IsSealedGatewayProfile(profile, 18900))
                throw new Exception("密文前缀匹配时也必须校验原端口。");
            Console.WriteLine("MAC_SEALED_SSH_PROFILE_DETECTION_AND_STOP_OK");
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
