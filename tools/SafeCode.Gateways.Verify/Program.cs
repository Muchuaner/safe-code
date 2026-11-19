using System.Text.Json.Nodes;
using SafeCode.Core;
void Assert(bool value,string message){if(!value)throw new Exception(message);}
string root=Path.Combine(Path.GetTempPath(),"safe-code-gateway-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(root);
try
{
    string path=Path.Combine(root,"enabled.json");
    var settings=new ConnectionSettings("test-vm","workspace","unused",22);
    var pref=new GatewayPreference(true,"test-vm",22,"workspace","SHA256:verified",18888,"opus",true);
    pref.Save(path);var loaded=GatewayPreference.Load(path);
    Assert(loaded==pref&&loaded.Matches(settings),"opt-in/options lost on reopen");
    Assert(!loaded.Matches(settings with{Host="other-vm"})&&!loaded.Matches(settings with{Username="other"})&&!loaded.Matches(settings with{Port=2222}),"consent reused for another server/account");
    Assert(loaded.Matches(settings,"SHA256:verified")&&!loaded.Matches(settings,"SHA256:changed"),"fingerprint not bound to consent");
    (loaded with{Enabled=false}).Save(path);Assert(!GatewayPreference.Load(path).Matches(settings),"explicit stop re-enabled on reopen");
    File.WriteAllText(path,"invalid");Assert(!GatewayPreference.Load(path).Enabled,"corrupt intent auto-enabled");
    string config=Path.Combine(root,"settings.json"),backup=Path.Combine(root,"backup.json");
    File.WriteAllText(config,"{\"theme\":\"dark\",\"env\":{\"KEEP\":\"value\"},\"permissions\":{\"defaultMode\":\"default\",\"deny\":[\"Read(secret)\"]}}");
    var permission=new ClaudePermissionSettings(config,backup);permission.Enable();permission.Enable();
    var json=JsonNode.Parse(File.ReadAllText(config))!;
    Assert(json["permissions"]!["defaultMode"]!.ToString()=="auto"&&json["permissions"]!["deny"]![0]!.ToString()=="Read(secret)"&&json["env"]!["KEEP"]!.ToString()=="value","Auto default damaged unrelated settings");
    permission.Restore();json=JsonNode.Parse(File.ReadAllText(config))!;
    Assert(json["permissions"]!["defaultMode"]!.ToString()=="default"&&!File.Exists(backup),"original mode not restored");
    permission.Enable();json=JsonNode.Parse(File.ReadAllText(config))!;json["permissions"]!["defaultMode"]="plan";File.WriteAllText(config,json.ToJsonString());permission.Restore();
    Assert(JsonNode.Parse(File.ReadAllText(config))!["permissions"]!["defaultMode"]!.ToString()=="plan","external permission edit overwritten");
    File.WriteAllText(config,"{}");permission.Enable();permission.Restore();Assert(JsonNode.Parse(File.ReadAllText(config))!["permissions"]?["defaultMode"]==null,"absent original mode not removed");
    File.WriteAllText(config,"invalid");bool rejected=false;try{permission.Enable();}catch(System.Text.Json.JsonException){rejected=true;}Assert(rejected&&File.ReadAllText(config)=="invalid","malformed settings overwritten");
    Console.WriteLine("GATEWAY_CONSENT_REOPEN_IDENTITY_DISABLE_AUTO_RESTORE_OK");
}
finally{Directory.Delete(root,true);}
