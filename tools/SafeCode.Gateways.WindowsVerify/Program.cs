using Microsoft.Win32;
using SafeCode.Core;
using SafeCode.Desktop;
using System.Text.Json;
void Assert(bool value,string message){if(!value)throw new Exception(message);}
Assert(Environment.MachineName=="DESKTOP-RO8H16T"&&args.SequenceEqual(new[]{"--allow-vm","192.168.50.66"}),"214 VM scope required");
string root=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode");
var settings=LocalSecrets.Load()??throw new Exception("saved login missing");
Assert(settings.Host==args[1]&&settings.Username=="root","saved connection is outside test scope");
string pin=new HostTrustStore(root).Describe(settings.Host,settings.Port,"").PreviousFingerprint??throw new Exception("VM fingerprint missing");
using var session=new SshSession(root);await session.ConnectAsync(settings,k=>k.Fingerprint==pin);
string testRoot=Path.Combine(Path.GetTempPath(),"SafeCode-GatewayLifecycle-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(testRoot);
string registry=@"SOFTWARE\SafeCode\Tests\"+Guid.NewGuid().ToString("N"),permissionPath=Path.Combine(testRoot,"settings.json");
ClaudeDesktopBridge Make()=>new(testRoot,registry,permissionPath);
string Read(string field){using var key=Registry.CurrentUser.OpenSubKey(registry);return key?.GetValue(field)?.ToString()??"";}
try
{
    string endpoint,token;
    using(var first=Make())
    {
        var status=await first.StartAsync(session,true);Assert(status.Active,"first start failed");endpoint=status.Endpoint;token=Read("inferenceGatewayApiKey");
        Assert(first.Preference.Matches(session),"consent not saved for verified VM");
        Assert(Read("deploymentDisplayName")=="SafeCode"&&Read("autoModeEnabled")=="1"&&Read("autoModeClassifierModel")=="claude-sonnet-5","display/Auto/classifier policy not configured");
        await first.StopAsync(suspend:true);Assert(!first.Status.Active&&first.Preference.Enabled,"safe exit cleared intent");
        Assert(Read("inferenceGatewayBaseUrl")==endpoint&&Read("inferenceGatewayApiKey")==token&&Read("deploymentDisplayName")=="SafeCode","safe exit removed stable policy");
    }
    using(var second=Make())
    {
        Assert(second.Preference.Matches(session)&&!second.Status.Active,"reopen lost intent or reported false active");
        var resumed=await second.StartAsync(session,true);Assert(resumed.Active&&resumed.Endpoint==endpoint&&Read("inferenceGatewayApiKey")==token,"reopen changed endpoint or credential");
        await second.StopAsync();Assert(!second.Preference.Enabled&&!second.Status.Managed&&Read("deploymentDisplayName")=="","explicit stop did not clear intent and restore policy");
    }
    using(var third=Make())
    {
        await third.StartAsync(session,true);
        using(var key=Registry.CurrentUser.OpenSubKey(registry,true))key!.SetValue("inferenceGatewayBaseUrl","http://127.0.0.1:12345");
        await third.RefreshAsync();Assert(!third.Preference.Enabled&&!third.Status.Active,"external route edit auto-restored without consent");
        await third.StopAsync();Assert(Read("inferenceGatewayBaseUrl")=="http://127.0.0.1:12345","stop overwrote external route");
    }
    Console.WriteLine(JsonSerializer.Serialize(new{ok=true,isolatedPolicy=true,persistedConsent=true,stableEndpoint=true,stableCredential=true,safeCodeDisplay=true,autoClassifier="claude-sonnet-5",explicitDisable=true,externalEditsPreserved=true}));
}
finally{Registry.CurrentUser.DeleteSubKeyTree(registry,false);Directory.Delete(testRoot,true);}
