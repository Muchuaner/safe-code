using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using SafeCode.Core;
namespace SafeCode.Desktop;
internal static class MacSelfTest
{
    public static Task RunAsync()
    {
        LocalSecrets.Initialize(RandomNumberGenerator.GetBytes(32));byte[] plain=Encoding.UTF8.GetBytes("fixture-password-not-a-real-secret");byte[] cipher=LocalSecrets.Protect(plain);
        if(!LocalSecrets.Unprotect(cipher).SequenceEqual(plain))throw new Exception("加密保存往返失败。");
        cipher[^1]^=1;try{LocalSecrets.Unprotect(cipher);throw new Exception("接受了被改写的密文。");}catch(CryptographicException){}
        if(MacClients.Quote("a'b")!="'a'\\''b'")throw new Exception("Shell 参数转义失败。");
        string Feed(string suffix)=>JsonSerializer.Serialize(new{tag_name="Beta-0.4.4.2",draft=false,prerelease=true,assets=new[]{new{name="SafeCode-Beta-0.4.4.2-"+suffix,browser_download_url="https://github.com/Muchuaner/safe-code/releases/download/Beta-0.4.4.2/SafeCode-Beta-0.4.4.2-"+suffix,digest="sha256:"+new string('a',64),size=123}}});
        using(var win=JsonDocument.Parse(Feed("Setup-x64.exe")))if(ClientServices.ParseRelease(win.RootElement,"Beta-0.4.4.1","macOS-arm64.dmg").CanInstall)throw new Exception("Mac 更新误选 Windows 安装包。");
        using(var mac=JsonDocument.Parse(Feed("macOS-arm64.dmg")))if(!ClientServices.ParseRelease(mac.RootElement,"Beta-0.4.4.1","macOS-arm64.dmg").CanInstall||ClientServices.ParseRelease(mac.RootElement,"Beta-0.4.4.1","macOS-x64.dmg").CanInstall)throw new Exception("架构筛选失败。");
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
        Console.WriteLine("MAC_AES_GCM_TAMPER_SHELL_QUOTING_AND_PLATFORM_UPDATE_SELECTION_OK");return Task.CompletedTask;
    }
}
