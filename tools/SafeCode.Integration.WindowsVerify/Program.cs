using Microsoft.Win32;
using SafeCode.Core;
using SafeCode.Desktop;
using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

void Assert(bool value,string message){if(!value)throw new Exception(message);}
Assert(OperatingSystem.IsWindows(),"Windows only");
Assert(args.Length>=2&&args[0]=="--allow-vm"&&args[1]=="192.168.50.66","explicit 214 VM scope required");
string root=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode");
var settings=LocalSecrets.Load()??throw new Exception("existing VM login missing");
Assert(settings.Host==args[1]&&settings.Username=="root","saved login does not match authorized VM");
string pin=new HostTrustStore(root).Describe(settings.Host,settings.Port,"").PreviousFingerprint??throw new Exception("verified VM fingerprint missing");
using var session=new SshSession(root);await session.ConnectAsync(settings,key=>key.Fingerprint==pin);
Assert(session.Server?.Username=="workspace","expected ordinary VM work user");
string directory=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode-DesktopVps-Verify");
using var bridge=new ClaudeDesktopBridge(directory);
try
{
    await bridge.StopAsync();
    var status=await bridge.StartAsync(session,true);
    Assert(status.Active&&status.Endpoint.StartsWith("http://127.0.0.1:"),"VPS bridge not active on loopback");
    using var key=Registry.CurrentUser.OpenSubKey(ClaudeDesktopPolicy.RegistryPath,false);
    string token=key?.GetValue("inferenceGatewayApiKey") as string??throw new Exception("local bridge token missing");
    Assert(!JsonSerializer.Serialize(status).Contains(token),"status leaked local token");
    using var http=new HttpClient(new HttpClientHandler{UseProxy=false}){Timeout=TimeSpan.FromSeconds(190)};
    using(var denied=await http.PostAsync(status.Endpoint+"/v1/messages",new StringContent("{}",Encoding.UTF8,"application/json")))Assert(denied.StatusCode==HttpStatusCode.Unauthorized,"unauthenticated endpoint accepted");
    http.DefaultRequestHeaders.Authorization=new AuthenticationHeaderValue("Bearer",token);
    JsonObject Body(JsonArray messages,JsonArray? tools=null,bool stream=false)=>new(){["model"]="claude-opus-5-5",["max_tokens"]=4096,["stream"]=stream,["thinking"]=new JsonObject{["type"]="adaptive"},["output_config"]=new JsonObject{["effort"]="high"},["messages"]=messages.DeepClone(),["tools"]=tools?.DeepClone()??new JsonArray()};
    async Task<JsonObject> Post(JsonObject body)
    {
        using var response=await http.PostAsync(status.Endpoint+"/v1/messages",new StringContent(body.ToJsonString(),Encoding.UTF8,"application/json"));
        string raw=await response.Content.ReadAsStringAsync();
        if(!response.IsSuccessStatusCode)throw new Exception("Messages request failed: "+(int)response.StatusCode+" "+raw[..Math.Min(raw.Length,400)]);
        return JsonNode.Parse(raw)!.AsObject();
    }
    static string Text(JsonObject response)=>string.Concat(response["content"]!.AsArray().Where(b=>b?["type"]?.GetValue<string>()=="text").Select(b=>b!["text"]!.GetValue<string>()));
    var probeBody=Body(new JsonArray(new JsonObject{["role"]="user",["content"]="Hi"}));probeBody["max_tokens"]=1;
    var probe=await Post(probeBody);Assert(probe["type"]!.GetValue<string>()=="message","tiny native probe failed");Console.WriteLine("VPS_NATIVE_TINY_PROBE_OK");
    var simple=await Post(Body(new JsonArray(new JsonObject{["role"]="user",["content"]="Reply with exactly OK. Do not use tools."})));
    Assert(Text(simple).Trim()=="OK","plain Messages reply failed");Console.WriteLine("VPS_NATIVE_MESSAGES_OK");
    string marker="MARKER_"+Guid.NewGuid().ToString("N");
    var tools=new JsonArray(new JsonObject{["name"]="bridge_marker",["description"]="Fetch the private test marker. Call this tool with an empty object. You cannot know the marker without this tool.",["input_schema"]=new JsonObject{["type"]="object",["properties"]=new JsonObject(),["additionalProperties"]=false}});
    var messages=new JsonArray(new JsonObject{["role"]="user",["content"]="Call bridge_marker to obtain the private marker, then reply with only its exact value."});
    var called=await Post(Body(messages,tools));
    Assert(called["stop_reason"]!.GetValue<string>()=="tool_use","VPS CLI did not return a client tool call");
    var call=called["content"]!.AsArray().Single(b=>b?["type"]?.GetValue<string>()=="tool_use")!;
    Assert(call["name"]!.GetValue<string>()=="bridge_marker","wrong client tool name");
    messages.Add(new JsonObject{["role"]="assistant",["content"]=called["content"]!.DeepClone()});
    messages.Add(new JsonObject{["role"]="user",["content"]=new JsonArray(new JsonObject{["type"]="tool_result",["tool_use_id"]=call["id"]!.GetValue<string>(),["content"]=marker})});
    var answered=await Post(Body(messages,tools));Assert(Text(answered).Trim()==marker,"client tool result did not return to the same native CLI turn");
    Console.WriteLine("VPS_CLIENT_TOOL_ROUNDTRIP_OK");
    messages.Add(new JsonObject{["role"]="assistant",["content"]=answered["content"]!.DeepClone()});
    messages.Add(new JsonObject{["role"]="user",["content"]="Repeat the exact marker from the previous turn. No tools."});
    using(var streamed=await http.PostAsync(status.Endpoint+"/v1/messages",new StringContent(Body(messages,tools,true).ToJsonString(),Encoding.UTF8,"application/json")))
    {
        string raw=await streamed.Content.ReadAsStringAsync();Assert(streamed.IsSuccessStatusCode&&raw.Contains(marker)&&raw.Contains("event: message_stop"),"native resume/SSE failed");
    }
    Console.WriteLine("VPS_NATIVE_RESUME_SSE_OK");
    status=await bridge.RefreshAsync();Assert(status.Usage.Available&&status.Usage.Total>0&&status.Usage.Output>0,"native usage missing or stale zero output snapshot");
    Console.WriteLine(JsonSerializer.Serialize(status,JsonDefaults.Options));
    if(args.Length>=4&&args[2]=="--hold")
    {
        string stop=args[3];File.WriteAllText(stop+".ready",JsonSerializer.Serialize(status,JsonDefaults.Options));
        Console.WriteLine("DESKTOP_UI_GATEWAY_READY");
        var until=DateTime.UtcNow.AddMinutes(30);
        while(!File.Exists(stop)&&DateTime.UtcNow<until){await Task.Delay(4000);status=await bridge.RefreshAsync();File.WriteAllText(stop+".ready",JsonSerializer.Serialize(status,JsonDefaults.Options));}
    }
    Console.WriteLine("WINDOWS_VPS_BRIDGE_ACCEPTANCE_OK");
}
finally
{
    await bridge.StopAsync();
    Console.WriteLine("VPS_BRIDGE_STOPPED_NO_DIRECT_API_RESTORE");
}
