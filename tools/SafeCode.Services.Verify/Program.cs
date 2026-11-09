using System.Net;
using System.Text;
using System.Text.Json;
using System.Security.Cryptography;
using SafeCode.Desktop;

void Assert(bool value,string message){if(!value)throw new Exception(message);}
var directory=Path.Combine(Path.GetTempPath(),"safe-code-services-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(directory);
string config=Path.Combine(AppContext.BaseDirectory,"service-endpoints.json");File.WriteAllText(config,"{\"feedbackBaseUrl\":\"https://collector.invalid/\"}");
if(args.Contains("--live")){
 File.WriteAllText(config,"{\"feedbackBaseUrl\":\"https://safecode.tommukan.dpdns.org/\"}");
 try{using var live=new ClientServices(directory);await live.SetConsentAsync(true,true);await live.SetConsentAsync(true,true);using var saved=JsonDocument.Parse(File.ReadAllText(Path.Combine(directory,"usage-preferences.json")));string sid=saved.RootElement.GetProperty("installationId").GetString()!;string marker="SafeCode integration test "+Guid.NewGuid().ToString();await live.SetConsentAsync(false,false);await live.SendFeedbackAsync(marker,default);Console.WriteLine(JsonSerializer.Serialize(new{sid,marker,feedbackSent=true}));}
 finally{Directory.Delete(directory,true);File.Delete(config);}return;
}
var handler=new FakeHandler();
try{
 using(var client=new ClientServices(directory,handler)){
  Assert(handler.Bodies.Count==0,"sent before consent");await client.SetConsentAsync(false,false);await client.SetConsentAsync(false,true);Assert(handler.Bodies.Count==0,"opt-out sent telemetry");
  await client.SetConsentAsync(true,true);await client.SetConsentAsync(true,true);Assert(handler.Bodies.Count==1,"launch was not deduplicated");
  using var doc=JsonDocument.Parse(handler.Bodies[0]);Assert(doc.RootElement.EnumerateObject().Select(p=>p.Name).Order().SequenceEqual(new[]{"schema","eventId","installationId","version","os","eventType"}.Order()),"unexpected telemetry fields");
  await client.SetConsentAsync(false,false);await client.SendFeedbackAsync("Explicit user feedback",default);Assert(handler.Bodies.Count==2,"feedback failed after optout");using var feedback=JsonDocument.Parse(handler.Bodies[1]);Assert(!feedback.RootElement.TryGetProperty("installationId",out _),"feedback contains install identifier");
 }
 using(var next=new ClientServices(directory,new FakeHandler())){Assert(JsonSerializer.Serialize(next.Preferences()).Contains("\"enabled\":false"),"optout did not survive restart");}
 var blocking=new BlockingHandler();using(var client=new ClientServices(Path.Combine(directory,"cancel"),blocking)){var sending=client.SetConsentAsync(true,true);await blocking.Started.Task;await client.SetConsentAsync(false,false);await sending;Assert(blocking.Cancelled,"optout did not cancel in-flight reporting");}
 Assert(ReleaseNumber.Parse("Beta-0.4.3.2")!.CompareTo(ReleaseNumber.Parse("Beta-0.4.3.1"))>0,"beta revision not increasing");
 Assert(ReleaseNumber.Parse("0.4.3")!.CompareTo(ReleaseNumber.Parse("Beta-0.4.3.9"))>0,"stable should supersede beta");
 Assert(ReleaseNumber.Parse("Beta-0.5.0.1")!.CompareTo(ReleaseNumber.Parse("0.4.3"))>0,"new beta core comparison failed");
 using(var beta=JsonDocument.Parse("""{"tag_name":"Beta-0.4.3.2","draft":false,"prerelease":true,"assets":[{"name":"SafeCode-Beta-0.4.3.2-Setup-x64.exe","browser_download_url":"https://github.com/Muchuaner/safe-code/releases/download/Beta-0.4.3.2/SafeCode-Beta-0.4.3.2-Setup-x64.exe","digest":"sha256:0000000000000000000000000000000000000000000000000000000000000000","size":123}]}""")){Assert(ClientServices.ParseRelease(beta.RootElement,"Beta-0.4.3.1").CanInstall,"beta update rejected");bool rejected=false;try{ClientServices.ParseRelease(beta.RootElement,"0.4.2");}catch(IOException){rejected=true;}Assert(rejected,"stable channel accepted beta");}
 var bytes=Encoding.UTF8.GetBytes("test installer fixture, never executed");string sha=Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
 string Feed(string digest,string url="https://github.com/Muchuaner/safe-code/releases/download/v9.0.0/SafeCode-9.0.0-Setup-x64.exe")=>JsonSerializer.Serialize(new{tag_name="v9.0.0",draft=false,prerelease=false,assets=new[]{new{name="SafeCode-9.0.0-Setup-x64.exe",browser_download_url=url,digest="sha256:"+digest,size=bytes.Length}}});
 using(var doc=JsonDocument.Parse(Feed(sha))){Assert(ClientServices.ParseRelease(doc.RootElement,"0.4.0").CanInstall,"valid release rejected");Assert(ClientServices.ParseRelease(doc.RootElement,"10.0.0").Version==null,"downgrade offered");}
 using(var doc=JsonDocument.Parse(Feed(sha,"https://evil.invalid/payload.exe")))Assert(!ClientServices.ParseRelease(doc.RootElement,"0.4.0").CanInstall,"external installer accepted");
 using(var doc=JsonDocument.Parse(Feed("")))Assert(!ClientServices.ParseRelease(doc.RootElement,"0.4.0").CanInstall,"unverified installer offered");
 using(var client=new ClientServices(Path.Combine(directory,"download"),new FakeHandler(Feed(sha)),new FakeHandler(bytes:bytes))){await client.CheckUpdateAsync(true,default);var file=await client.DownloadUpdateAsync((_,_)=>{},default);await ClientServices.VerifyInstallerAsync(file,default);File.AppendAllText(file.Path,"tampered");bool refused=false;try{await ClientServices.VerifyInstallerAsync(file,default);}catch(IOException){refused=true;}Assert(refused,"modified installer accepted");}
 using(var client=new ClientServices(Path.Combine(directory,"bad-hash"),new FakeHandler(Feed(new string('0',64))),new FakeHandler(bytes:bytes))){await client.CheckUpdateAsync(true,default);bool refused=false;try{await client.DownloadUpdateAsync((_,_)=>{},default);}catch(IOException){refused=true;}Assert(refused,"bad digest accepted");Assert(!Directory.EnumerateFiles(Path.Combine(directory,"bad-hash"),"*.exe",SearchOption.AllDirectories).Any(),"invalid executable retained");}
 using(var client=new ClientServices(Path.Combine(directory,"redirect"),new FakeHandler(Feed(sha)),new FakeHandler(redirect:true))){await client.CheckUpdateAsync(true,default);bool refused=false;try{await client.DownloadUpdateAsync((_,_)=>{},default);}catch(IOException){refused=true;}Assert(refused,"unsafe redirect accepted");}
 Console.WriteLine("CONSENT_OPTOUT_RESTART_CANCELLATION_FEEDBACK_RELEASE_DIGEST_TAMPER_AND_REDIRECT_CHECKS_PASSED");
}finally{Directory.Delete(directory,true);File.Delete(config);}
sealed class FakeHandler(string? feed=null,byte[]? bytes=null,bool redirect=false):HttpMessageHandler
{
 public List<string> Bodies{get;}=[];
 protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,CancellationToken ct){if(request.Content!=null)Bodies.Add(await request.Content.ReadAsStringAsync(ct));if(redirect){var response=new HttpResponseMessage(HttpStatusCode.Found);response.Headers.Location=new Uri("https://evil.invalid/payload.exe");return response;}return new HttpResponseMessage(HttpStatusCode.OK){Content=bytes!=null?new ByteArrayContent(bytes):new StringContent(feed??"{}")};}
}
sealed class BlockingHandler:HttpMessageHandler
{
 public TaskCompletionSource Started{get;}=new(TaskCreationOptions.RunContinuationsAsynchronously);public bool Cancelled;
 protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,CancellationToken ct){Started.SetResult();try{await Task.Delay(Timeout.Infinite,ct);}catch(OperationCanceledException){Cancelled=true;throw;}return new HttpResponseMessage(HttpStatusCode.OK);}
}
