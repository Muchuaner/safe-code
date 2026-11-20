using System.Net;
using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Security.Cryptography;
using SafeCode.Desktop;

void Assert(bool value,string message){if(!value)throw new Exception(message);}
if(args.Length==1&&args[0]=="--diagnostic-operations"){
 Console.WriteLine(JsonSerializer.Serialize(DiagnosticTrace.Operations));return;
}
if(args.Length==2&&args[0]=="--diagnostic-fixture"){
 var trace=new DiagnosticTrace("chrome",0);trace.Observe("Waiting for cache lock: password=must-not-upload 203.0.113.7");
 File.WriteAllText(args[1],JsonSerializer.Serialize(new{schema=2,eventId=trace.Id,version=ClientServices.Version,context=new{installationId=Guid.NewGuid().ToString(),sessionId=Guid.NewGuid().ToString(),operationId=trace.Id},report=trace.Finish(new InvalidOperationException("Waiting for cache lock /var/lib/dpkg/lock-frontend"))},SafeCode.Core.JsonDefaults.Options));return;
}
var directory=Path.Combine(Path.GetTempPath(),"safe-code-services-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(directory);
string config=Path.Combine(AppContext.BaseDirectory,"service-endpoints.json");File.WriteAllText(config,"{\"feedbackBaseUrl\":\"https://collector.invalid/\"}");
if(args.Contains("--update-live")){
 try{using var live=new ClientServices(directory);var update=await live.CheckUpdateAsync(true,default);Assert(update.CanInstall,"No update found for older Beta test client");var file=await live.DownloadUpdateAsync((_,_)=>{},default);await ClientServices.VerifyInstallerAsync(file,default);Console.WriteLine(JsonSerializer.Serialize(new{current=ClientServices.Version,offered=update.Version,downloadedBytes=new FileInfo(file.Path).Length,sha256=file.Digest,verified=true,executed=false}));}
 finally{Directory.Delete(directory,true);File.Delete(config);}return;
}
if(args.Contains("--live")){
 File.WriteAllText(config,"{\"feedbackBaseUrl\":\"https://safecode.tommukan.dpdns.org/\"}");
 try{using var live=new ClientServices(directory);await live.SetConsentAsync(true,true);await live.SetConsentAsync(true,true);using var saved=JsonDocument.Parse(File.ReadAllText(Path.Combine(directory,"usage-preferences.json")));string sid=saved.RootElement.GetProperty("installationId").GetString()!;string marker="SafeCode integration test "+Guid.NewGuid().ToString();await live.SetConsentAsync(false,false);await live.SendFeedbackAsync(marker,default);Console.WriteLine(JsonSerializer.Serialize(new{sid,marker,feedbackSent=true}));}
 finally{Directory.Delete(directory,true);File.Delete(config);}return;
}
var handler=new FakeHandler();
try{
 using(var client=new ClientServices(directory,handler)){
  using var defaults=JsonDocument.Parse(JsonSerializer.Serialize(client.Preferences()));
  Assert(!defaults.RootElement.GetProperty("enabled").GetBoolean()&&!defaults.RootElement.GetProperty("diagnosticsEnabled").GetBoolean(),"reporting was selected by default");
  await client.SetConsentAsync(true,false);Assert(handler.Bodies.Count==0,"sent before consent confirmation");
  await client.SetConsentAsync(false,true);Assert(handler.Bodies.Count==0,"opt-out sent telemetry");
  await client.SetConsentAsync(true,true);await client.SetConsentAsync(true,true);Assert(handler.Bodies.Count==1,"launch was not deduplicated");
  using var doc=JsonDocument.Parse(handler.Bodies[0]);Assert(doc.RootElement.EnumerateObject().Select(p=>p.Name).Order().SequenceEqual(new[]{"schema","eventId","installationId","version","os","eventType"}.Order()),"unexpected telemetry fields");
  await client.SetConsentAsync(false,false);await client.SendFeedbackAsync("Explicit user feedback",default);Assert(handler.Bodies.Count==2,"feedback failed after optout");using var feedback=JsonDocument.Parse(handler.Bodies[1]);Assert(!feedback.RootElement.TryGetProperty("installationId",out _),"feedback contains install identifier");
 }
 var localKey=RandomNumberGenerator.GetBytes(32);
 byte[] Seal(byte[] value)
 {
  var encrypted=new byte[28+value.Length];
  RandomNumberGenerator.Fill(encrypted.AsSpan(0,12));
  using var aes=new AesGcm(localKey,16);
  aes.Encrypt(encrypted.AsSpan(0,12),value,encrypted.AsSpan(28),encrypted.AsSpan(12,16));
  return encrypted;
 }
 byte[] Unseal(byte[] encrypted)
 {
  if(encrypted.Length<28)throw new CryptographicException();
  var value=new byte[encrypted.Length-28];
  using var aes=new AesGcm(localKey,16);
  aes.Decrypt(encrypted.AsSpan(0,12),encrypted.AsSpan(28),encrypted.AsSpan(12,16),value);
  return value;
 }
 var savedPath=Path.Combine(directory,"saved-login");
 var remembered=new SafeCode.Core.ConnectionSettings("fixture.example","fixture-user","fixture-password",2222);
 File.WriteAllBytes(savedPath,Seal(JsonSerializer.SerializeToUtf8Bytes(remembered,SafeCode.Core.JsonDefaults.Options)));
 var login=new SavedLoginStore(savedPath,Seal,Unseal);
 Assert(login.Load()==remembered,"prior-version encrypted login did not load");
 login.Checkpoint();
 File.WriteAllBytes(savedPath,[0x12,0x34]);
 Assert(new SavedLoginStore(savedPath,Seal,Unseal).Load()==remembered,"encrypted backup did not recover after update");
 var newer=remembered with{Password="updated-fixture"};
 login.Save(newer);
 Assert(login.Load()==newer,"recovered login could not be replaced");
 Assert(!Encoding.UTF8.GetString(File.ReadAllBytes(savedPath)).Contains(newer.Password),"saved password was written in clear text");

 var relay=new GatewayDiagnostic("messages");
 relay.Response(200,null);
 var requestId=Guid.NewGuid().ToString();
 var fakeFrame=$"event: error\r\ndata: "+JsonSerializer.Serialize(new {safeCodeDiagnostic=new
  {protocol=1,id=requestId,backend="cloudcli",stage="anthropic_response",gatewayStatus=400,
   cloudcliStatus=200,anthropicStatus=400,anthropicRequestId="req_Example1234",
   message="private diagnostic body",modelFamily="https://private.invalid",requestBytes=128}})+"\r\n\r\n";
 var frame=Encoding.UTF8.GetBytes(fakeFrame);
 relay.ObserveSse(frame.AsSpan(0,18));relay.ObserveSse(frame.AsSpan(18));
 var details=relay.Finish(frame.Length);
 Assert(relay.SawError&&relay.FailureStatus==400&&(string)details["id"]==requestId,"streamed gateway failure was not correlated");
 Assert(!details.ContainsKey("message")&&!details.ContainsKey("modelFamily"),"unrecognized gateway values reached diagnostic");
 var diagnosticPackets=new FakeHandler();
 using(var reporting=new ClientServices(Path.Combine(directory,"gateway-diagnostic"),diagnosticPackets)){
  await reporting.SetConsentAsync(false,true);
  reporting.SetDiagnosticConsent(true);
  var trace=reporting.BeginDiagnostic("desktop_key_request")!;
  trace.Gateway=details;
  await reporting.SendDiagnosticAsync(trace,new HttpRequestException(null,null,HttpStatusCode.BadRequest),default);
  await reporting.SendFeedbackAsync("Test failure correlation",default);
  Assert(diagnosticPackets.Bodies.Count==2,"gateway error did not attach to diagnostics and feedback");
  foreach(var body in diagnosticPackets.Bodies){
   using var packet=JsonDocument.Parse(body);
   Assert(packet.RootElement.GetProperty("schema").GetInt32()==3&&body.Contains(requestId)&&!body.Contains("private"),"invalid diagnostic envelope");
  }
 }
 using(var next=new ClientServices(directory,new FakeHandler())){Assert(JsonSerializer.Serialize(next.Preferences()).Contains("\"enabled\":false"),"optout did not survive restart");}
 var blocking=new BlockingHandler();using(var client=new ClientServices(Path.Combine(directory,"cancel"),blocking)){var sending=client.SetConsentAsync(true,true);await blocking.Started.Task;await client.SetConsentAsync(false,false);await sending;Assert(blocking.Cancelled,"optout did not cancel in-flight reporting");}
 Assert(ReleaseNumber.Parse("Beta-0.4.3.2")!.CompareTo(ReleaseNumber.Parse("Beta-0.4.3.1"))>0,"beta revision not increasing");
 Assert(ReleaseNumber.Parse("0.4.3")!.CompareTo(ReleaseNumber.Parse("Beta-0.4.3.9"))>0,"stable should supersede beta");
 Assert(ReleaseNumber.Parse("Beta-0.5.0.1")!.CompareTo(ReleaseNumber.Parse("0.4.3"))>0,"new beta core comparison failed");
 using(var beta=JsonDocument.Parse("""{"tag_name":"Beta-0.4.3.2","draft":false,"prerelease":true,"assets":[{"name":"SafeCode-Beta-0.4.3.2-Setup-x64.exe","browser_download_url":"https://github.com/Muchuaner/safe-code/releases/download/Beta-0.4.3.2/SafeCode-Beta-0.4.3.2-Setup-x64.exe","digest":"sha256:0000000000000000000000000000000000000000000000000000000000000000","size":123}]}""")){Assert(ClientServices.ParseRelease(beta.RootElement,"Beta-0.4.3.1").CanInstall,"beta update rejected");bool rejected=false;try{ClientServices.ParseRelease(beta.RootElement,"0.4.2");}catch(IOException){rejected=true;}Assert(rejected,"stable channel accepted beta");}
 var bytes=Encoding.UTF8.GetBytes("test installer fixture, never executed");string sha=Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
 string Feed(string digest,string url="https://github.com/Muchuaner/safe-code/releases/download/v9.0.0/SafeCode-9.0.0-Setup-x64.exe")=>JsonSerializer.Serialize(new{tag_name="v9.0.0",draft=false,prerelease=false,assets=new[]{new{name="SafeCode-9.0.0-Setup-x64.exe",browser_download_url=url,digest="sha256:"+digest,size=bytes.Length}}});
 string ReleaseFeed(string tag,bool prerelease,string digest){string name="SafeCode-"+tag+"-Setup-x64.exe";return JsonSerializer.Serialize(new{tag_name=tag,draft=false,prerelease,assets=new[]{new{name,browser_download_url="https://github.com/Muchuaner/safe-code/releases/download/"+tag+"/"+name,digest="sha256:"+digest,size=bytes.Length}}});}
 string MissingAssetFeed(string tag)=>JsonSerializer.Serialize(new[]{new{tag_name=tag,draft=false,prerelease=true,assets=Array.Empty<object>()}});
 string AtomFeed(params string[] tags)=>"<?xml version=\"1.0\" encoding=\"utf-8\"?><feed xmlns=\"http://www.w3.org/2005/Atom\"><id>tag:github.com,2008:https://github.com/Muchuaner/safe-code/releases</id><title>Release notes from safe-code</title><updated>2026-11-19T21:05:45Z</updated>"+string.Join("",tags.Select(tag=>$"<entry><id>tag:github.com,2008:Repository/1450215833/{tag}</id><updated>2026-11-19T21:05:45Z</updated><link rel=\"alternate\" type=\"text/html\" href=\"https://github.com/Muchuaner/safe-code/releases/tag/{Uri.EscapeDataString(tag)}\"/><title>{tag}</title></entry>"))+"</feed>";
 void ExpireUpdateCache(ClientServices client)=>typeof(ClientServices).GetField("lastCheck",BindingFlags.Instance|BindingFlags.NonPublic)!.SetValue(client,DateTimeOffset.UtcNow-TimeSpan.FromDays(2));
 using(var doc=JsonDocument.Parse(Feed(sha))){Assert(ClientServices.ParseRelease(doc.RootElement,"0.4.0").CanInstall,"valid release rejected");Assert(ClientServices.ParseRelease(doc.RootElement,"10.0.0").Version==null,"downgrade offered");}
 using(var doc=JsonDocument.Parse(Feed(sha,"https://evil.invalid/payload.exe")))Assert(!ClientServices.ParseRelease(doc.RootElement,"0.4.0").CanInstall,"external installer accepted");
 using(var doc=JsonDocument.Parse(Feed("")))Assert(!ClientServices.ParseRelease(doc.RootElement,"0.4.0").CanInstall,"unverified installer offered");
 using(var client=new ClientServices(Path.Combine(directory,"download"),new FakeHandler(Feed(sha)),new FakeHandler(bytes:bytes),updateAssetSuffix:"Setup-x64.exe")){await client.CheckUpdateAsync(true,default);var file=await client.DownloadUpdateAsync((_,_)=>{},default);await ClientServices.VerifyInstallerAsync(file,default);File.AppendAllText(file.Path,"tampered");bool refused=false;try{await ClientServices.VerifyInstallerAsync(file,default);}catch(IOException){refused=true;}Assert(refused,"modified installer accepted");}
 using(var client=new ClientServices(Path.Combine(directory,"bad-hash"),new FakeHandler(Feed(new string('0',64))),new FakeHandler(bytes:bytes),updateAssetSuffix:"Setup-x64.exe")){await client.CheckUpdateAsync(true,default);bool refused=false;try{await client.DownloadUpdateAsync((_,_)=>{},default);}catch(IOException){refused=true;}Assert(refused,"bad digest accepted");Assert(!Directory.EnumerateFiles(Path.Combine(directory,"bad-hash"),"*.exe",SearchOption.AllDirectories).Any(),"invalid executable retained");}
 using(var client=new ClientServices(Path.Combine(directory,"redirect"),new FakeHandler(Feed(sha)),new FakeHandler(redirect:true),updateAssetSuffix:"Setup-x64.exe")){await client.CheckUpdateAsync(true,default);bool refused=false;try{await client.DownloadUpdateAsync((_,_)=>{},default);}catch(IOException){refused=true;}Assert(refused,"unsafe redirect accepted");}
 using(var client=new ClientServices(Path.Combine(directory,"missing-asset"),new FakeHandler(MissingAssetFeed("Beta-9.9.9.9")),updateAssetSuffix:"Setup-x64.exe",currentVersion:"Beta-0.4.4.17"))
 {
  var update=await client.CheckUpdateAsync(true,default);
  Assert(update.Version=="Beta-9.9.9.9"&&!update.CanInstall,"newer release without a verified asset was not reported safely");
  Assert(update.Page=="https://github.com/Muchuaner/safe-code/releases/tag/Beta-9.9.9.9","missing-asset release did not use its official page");
  bool refused=false;try{await client.DownloadUpdateAsync((_,_)=>{},default);}catch(InvalidOperationException){refused=true;}Assert(refused,"newer release without an asset enabled auto install");
 }
 var stableLimited=new UpdateHandler{ApiStatus=HttpStatusCode.TooManyRequests,LatestLocation="/Muchuaner/safe-code/releases/tag/v0.4.5"};
 using(var client=new ClientServices(Path.Combine(directory,"rate-stable"),stableLimited,currentVersion:"0.4.4"))
 {
  var update=await client.CheckUpdateAsync(true,default);
  Assert(update.Version=="0.4.5"&&!update.CanInstall&&update.Page=="https://github.com/Muchuaner/safe-code/releases/tag/v0.4.5","stable channel failed to identify the latest official release");
  Assert(stableLimited.ApiRequests==1&&stableLimited.AtomRequests==0&&stableLimited.LatestRequests==1,"stable channel requested the wrong fallback");
  Assert(await client.CheckUpdateAsync(true,default)==update&&stableLimited.ApiRequests==1&&stableLimited.LatestRequests==1,"stable rate-limit window retried a cached result");
 }
 foreach(var location in new[]{"/Muchuaner/safe-code/releases/tag/Beta-9.9.9.9","https://evil.invalid/Muchuaner/safe-code/releases/tag/9.9.9","/Muchuaner/safe-code/releases"})
 {
  var stableUnknown=new UpdateHandler{ApiStatus=HttpStatusCode.TooManyRequests,LatestLocation=location};
  using var client=new ClientServices(Path.Combine(directory,"rate-stable-unknown"),stableUnknown,currentVersion:"0.4.4");
  var update=await client.CheckUpdateAsync(true,default);
  Assert(update.Version==null&&!update.CanInstall&&update.Status.Contains("无法"),"stable fallback accepted an unconfirmed release");
 }
 var longLimited=new UpdateHandler{ApiStatus=HttpStatusCode.TooManyRequests,RetryAfter="7200"};
 using(var client=new ClientServices(Path.Combine(directory,"rate-long"),longLimited,currentVersion:"0.4.4"))
 {
  await client.CheckUpdateAsync(true,default);
  var blockedUntil=(DateTimeOffset)typeof(ClientServices).GetField("apiBlockedUntil",BindingFlags.Instance|BindingFlags.NonPublic)!.GetValue(client)!;
  Assert(blockedUntil-DateTimeOffset.UtcNow>TimeSpan.FromMinutes(90),"valid Retry-After was capped to one hour");
 }
 var concurrentHandler=new GatedUpdateHandler("["+ReleaseFeed("Beta-9.9.9.9",true,sha)+"]");
 using(var client=new ClientServices(Path.Combine(directory,"update-concurrent"),concurrentHandler,updateAssetSuffix:"Setup-x64.exe",currentVersion:"Beta-0.4.4.17"))
 {
  var first=client.CheckUpdateAsync(true,default);await concurrentHandler.Started.Task;
  var second=client.CheckUpdateAsync(true,default);concurrentHandler.Release.TrySetResult();
  var updates=await Task.WhenAll(first,second);
  Assert(concurrentHandler.ApiRequests==1,"concurrent update checks called the API twice");
  Assert(updates.All(x=>x.Version=="Beta-9.9.9.9"&&x.CanInstall),"concurrent update check lost the cached result");
 }
 var slowFallback=new SlowFallbackHandler();
 using(var client=new ClientServices(Path.Combine(directory,"update-timeout"),slowFallback,currentVersion:"Beta-0.4.4.17"))
 {
  var http=(HttpClient)typeof(ClientServices).GetField("http",BindingFlags.Instance|BindingFlags.NonPublic)!.GetValue(client)!;http.Timeout=TimeSpan.FromMilliseconds(250);
  var timedOut=await client.CheckUpdateAsync(true,default).WaitAsync(TimeSpan.FromSeconds(3));
  Assert(timedOut.Version==null&&timedOut.Page=="https://github.com/Muchuaner/safe-code/releases","fallback body escaped the HttpClient timeout");
 }
 var rateBeta=new UpdateHandler{ApiStatus=HttpStatusCode.Forbidden,AtomFeed=AtomFeed("Beta-9.9.9.9").Replace("https://github.com/Muchuaner/safe-code/releases/tag/","https://evil.invalid/releases/tag/")};
 using(var client=new ClientServices(Path.Combine(directory,"rate-beta"),rateBeta,updateAssetSuffix:"Setup-x64.exe",currentVersion:"Beta-0.4.4.17"))
 {
  var unknown=await client.CheckUpdateAsync(true,default);
  Assert(unknown.Version==null&&!unknown.CanInstall&&unknown.Status!="已是最新版本","invalid fallback reported a version");
  Assert(unknown.Page=="https://github.com/Muchuaner/safe-code/releases","invalid fallback did not use the official page");
  bool refused=false;try{await client.DownloadUpdateAsync((_,_)=>{},default);}catch(InvalidOperationException){refused=true;}Assert(refused,"invalid fallback enabled auto install");
  Assert(rateBeta.ApiRequests==1&&rateBeta.AtomRequests==1,"unexpected fallback requests");
  rateBeta.AtomFeed=AtomFeed("Beta-0.4.4.18").Replace("tag:github.com,2008:https://github.com/Muchuaner/safe-code/releases","tag:github.com,2008:https://evil.invalid/releases");
  var forgedFeed=await client.CheckUpdateAsync(true,default);
  Assert(forgedFeed.Version==null&&forgedFeed.Status!="已是最新版本","untrusted fallback feed was accepted");
  Assert(rateBeta.ApiRequests==1&&rateBeta.AtomRequests==2,"forged fallback feed retried the API");
  rateBeta.AtomFeed=AtomFeed("Beta-0.4.4.18","Beta-0.4.4.17","0.4.3");
  var update=await client.CheckUpdateAsync(true,default);
  Assert(update.Version=="Beta-0.4.4.18"&&!update.CanInstall,"rate-limited beta fallback missed newer release");
  Assert(update.Page=="https://github.com/Muchuaner/safe-code/releases/tag/Beta-0.4.4.18","fallback page is not the official release tag");
  Assert(update.Status.Contains("发布页"),"fallback did not direct to the release page");
  Assert(rateBeta.ApiRequests==1,"rate-limited beta check retried the API");
  Assert(rateBeta.AtomRequests==3,"fallback was not retried after an unknown result");
  bool noDownload=false;try{await client.DownloadUpdateAsync((_,_)=>{},default);}catch(InvalidOperationException){noDownload=true;}Assert(noDownload,"unverified fallback enabled auto install");
 }
 var wrongNamespace=new UpdateHandler{ApiStatus=HttpStatusCode.Forbidden,AtomFeed=AtomFeed("Beta-9.9.9.9").Replace("http://www.w3.org/2005/Atom","http://evil.invalid/atom")};
 using(var client=new ClientServices(Path.Combine(directory,"atom-namespace"),wrongNamespace,currentVersion:"Beta-0.4.4.17"))
 {
  var unknown=await client.CheckUpdateAsync(true,default);
  Assert(unknown.Version==null&&unknown.Page=="https://github.com/Muchuaner/safe-code/releases","fallback accepted a nonstandard Atom namespace");
 }
 var olderBeta=new UpdateHandler{ApiStatus=HttpStatusCode.Forbidden,AtomFeed=AtomFeed("Beta-0.4.4.16","0.4.3")};
 using(var client=new ClientServices(Path.Combine(directory,"rate-older"),olderBeta,currentVersion:"Beta-0.4.4.17"))
 {
  var older=await client.CheckUpdateAsync(true,default);
  Assert(older.Version==null&&older.Status.Contains("无法")&&older.Page=="https://github.com/Muchuaner/safe-code/releases","older fallback releases were reported as current");
 }
 var latestBeta=new UpdateHandler{ApiStatus=HttpStatusCode.Forbidden,AtomFeed=AtomFeed("Beta-0.4.4.17","Beta-0.4.4.16","0.4.3","٩.٩.٩","9.9.9\n")};
 using(var client=new ClientServices(Path.Combine(directory,"rate-latest"),latestBeta,currentVersion:"Beta-0.4.4.17"))
 {
  var latest=await client.CheckUpdateAsync(true,default);
  Assert(latest.Version==null&&latest.Status=="官方发布订阅未发现更新"&&latest.Page=="https://github.com/Muchuaner/safe-code/releases"&&!latest.CanInstall,"fallback latest result is wrong");
  Assert(latestBeta.AtomRequests==1,"latest fallback was not requested");
 }
 var formalRelease=new UpdateHandler{ApiStatus=HttpStatusCode.Forbidden,AtomFeed=AtomFeed("v0.4.4","Beta-0.4.4.18","Beta-0.4.4.17")};
 using(var client=new ClientServices(Path.Combine(directory,"rate-beta-to-formal"),formalRelease,currentVersion:"Beta-0.4.4.17"))
 {
  var update=await client.CheckUpdateAsync(true,default);
  Assert(update.Version=="0.4.4"&&!update.CanInstall&&update.Page=="https://github.com/Muchuaner/safe-code/releases/tag/v0.4.4","beta fallback missed a newer formal release");
  Assert(await client.CheckUpdateAsync(true,default)==update&&formalRelease.AtomRequests==1,"beta fallback success was not cached");
  typeof(ClientServices).GetField("apiBlockedUntil",BindingFlags.Instance|BindingFlags.NonPublic)!.SetValue(client,DateTimeOffset.UtcNow-TimeSpan.FromSeconds(1));
  formalRelease.ApiStatus=HttpStatusCode.OK;
  formalRelease.ApiFeed=MissingAssetFeed("Beta-0.4.5.1");
  var recovered=await client.CheckUpdateAsync(false,default);
  Assert(recovered.Version=="Beta-0.4.5.1"&&formalRelease.ApiRequests==2,"fallback cache prevented the API from recovering");
 }
 var staleHandler=new UpdateHandler{ApiFeed="["+ReleaseFeed("Beta-9.9.9.9",true,sha)+"]"};
 using(var client=new ClientServices(Path.Combine(directory,"stale-cache"),staleHandler,updateAssetSuffix:"Setup-x64.exe",currentVersion:"Beta-0.4.4.17"))
 {
  var fresh=await client.CheckUpdateAsync(true,default);Assert(fresh.Version=="Beta-9.9.9.9"&&fresh.CanInstall,"valid beta API result was rejected");
  staleHandler.ApiStatus=HttpStatusCode.Forbidden;staleHandler.AtomFeed="<feed><entry>";
  ExpireUpdateCache(client);
  var failedRefresh=await client.CheckUpdateAsync(true,default);Assert(failedRefresh.Version==null&&!failedRefresh.CanInstall,"failed refresh returned a stale success");
  var retried=await client.CheckUpdateAsync(true,default);Assert(retried.Version==null&&!retried.CanInstall,"failed refresh left a stale success cached");
  Assert(staleHandler.ApiRequests==2&&staleHandler.AtomRequests==2,"failed refresh did not respect the rate-limit window");
  bool staleDownload=false;try{await client.DownloadUpdateAsync((_,_)=>{},default);}catch(InvalidOperationException){staleDownload=true;}Assert(staleDownload,"failed refresh left a downloadable installer");
 }
 var cancelUpdate=new BlockingHandler();
 using(var client=new ClientServices(Path.Combine(directory,"update-cancel"),cancelUpdate,currentVersion:"Beta-0.4.4.17"))
 {
  using var cts=new CancellationTokenSource();var checking=client.CheckUpdateAsync(true,cts.Token);await cancelUpdate.Started.Task;cts.Cancel();
  bool canceled=false;try{await checking;}catch(OperationCanceledException){canceled=true;}
  Assert(canceled&&cancelUpdate.Cancelled,"update check cancellation was not honored");
 }
 var slowDownload=new SlowDownloadHandler();
 using(var client=new ClientServices(Path.Combine(directory,"download-cancel"),new FakeHandler("["+ReleaseFeed("Beta-9.9.9.9",true,sha)+"]"),slowDownload,updateAssetSuffix:"Setup-x64.exe",currentVersion:"Beta-0.4.4.17"))
 {
  await client.CheckUpdateAsync(true,default);
  using var cts=new CancellationTokenSource();var downloading=client.DownloadUpdateAsync((_,_)=>{},cts.Token);await slowDownload.Started.Task;cts.Cancel();
  bool canceled=false;try{await downloading;}catch(OperationCanceledException){canceled=true;}
  Assert(canceled,"download cancellation was not honored");
  Assert(!Directory.EnumerateFiles(Path.Combine(directory,"download-cancel"),"*.exe",SearchOption.AllDirectories).Any(),"canceled download retained a partial file");
 }
 var diagnosticDirectory=Path.Combine(directory,"diagnostics");var diagnosticHandler=new FakeHandler();
 using(var diagnosticClient=new ClientServices(diagnosticDirectory,diagnosticHandler))
 {
  Assert(!diagnosticClient.DiagnosticsAllowed&&JsonSerializer.Serialize(diagnosticClient.Preferences()).Contains("\"diagnosticsEnabled\":false"),"diagnostics must be off by default");
  var beforeConsent=new DiagnosticTrace("chrome",diagnosticClient.DiagnosticGeneration);beforeConsent.Observe("Waiting for cache lock /var/lib/dpkg/lock-frontend password=super-secret");
  await diagnosticClient.SendDiagnosticAsync(beforeConsent,new DirectoryNotFoundException("C:\\Users\\private\\account-drafts\\secret.dpapi"),default);
  Assert(diagnosticHandler.Bodies.Count==0,"diagnostics sent before consent");
  diagnosticClient.SetDiagnosticConsent(true);await diagnosticClient.SendDiagnosticAsync(beforeConsent,null,default);
  Assert(diagnosticHandler.Bodies.Count==0,"diagnostics sent before notice confirmation");
  await diagnosticClient.SetConsentAsync(false,true);
  var trace=new DiagnosticTrace("chrome",diagnosticClient.DiagnosticGeneration);
  foreach(string secret in new[]{"password=super-secret","token=sk-private-key","203.0.113.7","user@example.com","ssh-ed25519 PRIVATEKEY","C:\\Users\\private","/home/private/project","https://private.invalid/?token=secret"}){trace.Observe(secret);trace.Observe("Waiting for cache lock: "+secret);}
  await diagnosticClient.SendDiagnosticAsync(trace,new DirectoryNotFoundException("private file path super-secret"),default);
  Assert(diagnosticHandler.Bodies.Count==1,"explicit diagnostics opt-in failed with usage disabled");
  string payload=diagnosticHandler.Bodies[0];using var packet=JsonDocument.Parse(payload);
  Assert(packet.RootElement.EnumerateObject().Select(x=>x.Name).Order().SequenceEqual(new[]{"schema","eventId","version","report","context"}.Order()),"unexpected diagnostic fields");
  foreach(string secret in new[]{"super-secret","sk-private-key","203.0.113.7","user@example.com","PRIVATEKEY","private.invalid","private file path"})Assert(!payload.Contains(secret),"private text entered diagnostic payload");
  Assert(packet.RootElement.GetProperty("report").GetProperty("errorKind").GetString()=="directory_not_found","exception category missing");
  Assert(packet.RootElement.GetProperty("report").GetProperty("signals")[0].GetProperty("code").GetString()=="package_lock_wait","lock diagnostic missing");
  var oldTrace=new DiagnosticTrace("chrome",diagnosticClient.DiagnosticGeneration);diagnosticClient.SetDiagnosticConsent(false);diagnosticClient.SetDiagnosticConsent(true);
  await diagnosticClient.SendDiagnosticAsync(oldTrace,null,default);Assert(diagnosticHandler.Bodies.Count==1,"old report survived consent revocation");
  var bounded=new DiagnosticTrace("account",diagnosticClient.DiagnosticGeneration);for(int i=0;i<200;i++)bounded.Observe(i%2==0?"Unpacking package-private":"Setting up package-private");
  Assert(bounded.Finish(null).Signals.Length==64,"diagnostic log is unbounded");
  diagnosticClient.SetDiagnosticConsent(false);
 }
 using(var restarted=new ClientServices(diagnosticDirectory,new FakeHandler()))Assert(!restarted.DiagnosticsAllowed&&!JsonSerializer.Serialize(restarted.Preferences()).Contains("\"diagnosticsEnabled\":true"),"diagnostic optout not persisted");
 Assert(DiagnosticTrace.ErrorKind(new InvalidOperationException("中文提示",new Renci.SshNet.Common.SshAuthenticationException("private")))=="ssh_auth_failed","friendly wrapper hid SSH cause");
 Assert(DiagnosticTrace.ErrorKind(new JsonException("private"))=="json_invalid","JSON error unclassified");
 Assert(DiagnosticTrace.ErrorKind(new Renci.SshNet.Common.SshAuthenticationException("private"))=="ssh_auth_failed","SSH auth unclassified");
 Assert(DiagnosticTrace.ErrorKind(new Renci.SshNet.Common.SshOperationTimeoutException("private"))=="ssh_timeout","SSH timeout unclassified");
 Assert(DiagnosticTrace.ErrorKind(new SafeCode.Core.RemoteOperationException("private","mount_failed"))=="mount_failed","remote error lost");
 Assert(DiagnosticTrace.ErrorKind(new SafeCode.Core.RemoteOperationException("private","mount_target_invalid"))=="mount_target_invalid","remote mount target code lost");
 Assert(DiagnosticTrace.ErrorKind(new SafeCode.Core.RemoteOperationException("private","invalid_argument"))=="invalid_argument","remote argument code lost");
 Assert(DiagnosticTrace.ErrorKind(new SafeCode.Core.RemoteOperationException("private","deployment_busy"))=="deployment_busy","remote busy code lost");
 foreach(var (status,kind) in new (int,string)[]{(400,"gateway_invalid_request"),(401,"gateway_auth_failed"),(403,"gateway_auth_failed"),(409,"gateway_conflict"),(429,"gateway_rate_limited"),(501,"gateway_not_supported"),(503,"gateway_upstream_failed")}){
  Assert(DiagnosticTrace.ErrorKind(new HttpRequestException("secret header",(Exception?)null,(HttpStatusCode)status))==kind,"gateway HTTP status lost");
 }
 Assert(DiagnosticTrace.ErrorKind(new HttpRequestException(HttpRequestError.SecureConnectionError,"private endpoint"))=="crypto_error","gateway TLS failure lost");
 using(var argsDocument=JsonDocument.Parse("""{"action":"issue","activation_code":"private","step":"gateway_key_issue","operation":"gateway_key_issue"}""")){
  var argsValue=argsDocument.RootElement;
  Assert(DiagnosticTrace.OperationFor("safeCodeApi.manage",argsValue)=="gateway_key_issue","gateway creation not instrumented");
  Assert(DiagnosticTrace.OperationFor("deploy",argsValue)==null&&DiagnosticTrace.OperationFor("job.resume",argsValue)==null,"unsafe operation alias accepted");
 }
 using(var argsDocument=JsonDocument.Parse("""{"action":"usage","background":true,"keyId":"private"}""")){
  var argsValue=argsDocument.RootElement;
  Assert(DiagnosticTrace.GatewayOperation(argsValue)=="gateway_usage"&&DiagnosticTrace.IsBackgroundOperation("gateway_usage",argsValue),"gateway poll not sampled");
 }
 var claudeTrace=new DiagnosticTrace("claude",0);
 claudeTrace.Observe("正在通过 VPS 网络获取 Claude Code 官方版本和校验信息…");
 claudeTrace.Observe("VPS 下载完成，官方 Claude Code 文件校验通过。");
 var claudeReport=claudeTrace.Finish(null);
 Assert(claudeReport.Stage=="claude_download_verified"&&claudeReport.Signals.Length==2,"Claude verification milestones lost");
 Assert(DiagnosticTrace.FailureSite("mount_apply",new DirectoryNotFoundException("local only"))=="directory_mount","local mount error marked remote");
 Assert(DiagnosticTrace.FailureSite("mount_apply",new SafeCode.Core.RemoteOperationException("private","mount_target_invalid"))=="remote_task","remote mount error lost");
 Assert(DiagnosticTrace.FailureSite("ip_policy",new InvalidOperationException("local guard"))=="unknown","local IP guard marked remote");
 Assert(DiagnosticTrace.FailureSite("ip_policy",new TimeoutException("verification"))=="remote_task","IP verification timeout marked local");
 Assert(DiagnosticTrace.FailureSite("connection",new Renci.SshNet.Common.SshConnectionException("private"))=="connection","SSH connection site lost");
 var localMountTrace=new DiagnosticTrace("mount_apply",0);var localMountReport=localMountTrace.Finish(new DirectoryNotFoundException("local only"));
 Assert(localMountReport.FailureSite=="directory_mount"&&localMountReport.ErrorKind=="directory_not_found","local mount report misclassified");
 var errorStageTrace=new DiagnosticTrace("localization",0);errorStageTrace.Observe("任务请求已上传，正在启动远端操作…");
 var errorStageReport=errorStageTrace.Finish(new InvalidOperationException("DNS 域名解析失败"));
 Assert(errorStageReport.ErrorKind=="dns_failure"&&errorStageReport.Stage=="request_uploaded","error code leaked into stage");
 var mountStageTrace=new DiagnosticTrace("mount_apply",0);mountStageTrace.Observe("任务请求已上传，正在启动远端操作…");mountStageTrace.Observe("开始应用目录挂载配置…");
 var mountStageReport=mountStageTrace.Finish(new SafeCode.Core.RemoteOperationException("private","mount_failed"));
 Assert(mountStageReport.Stage=="remote_started"&&mountStageReport.ErrorKind=="mount_failed","mount phase stage lost");
 var startedOnly=new DiagnosticTrace("localization",0).Finish(new InvalidOperationException("DNS 域名解析失败"));
 Assert(startedOnly.Stage=="started","error-only trace invented a phase");
 var noRegress=new DiagnosticTrace("ip_policy",0);noRegress.Observe("任务请求已上传，正在启动远端操作…");noRegress.Observe("服务器正在独立应用设置，断线不会中止自动回退…");noRegress.Observe("任务请求已上传，正在启动远端操作…");
 Assert(noRegress.Finish(new TimeoutException("verification")).Stage=="remote_started","later request regressed stage");
 var ipPhase=new DiagnosticTrace("ip_policy",0);ipPhase.Observe("任务请求已上传，正在启动远端操作…");ipPhase.Observe("所需组件已安装，跳过重复下载。");ipPhase.Observe("服务器正在独立应用设置，断线不会中止自动回退…");
 Assert(ipPhase.Finish(new TimeoutException("verification")).Stage=="remote_started","IP apply phase did not advance");
 var packagePhase=new DiagnosticTrace("account",0);packagePhase.Observe("开始部署：account");packagePhase.Observe("所需组件已安装，跳过重复下载。");packagePhase.Observe("Unpacking package-fixture");
 Assert(packagePhase.Finish(new InvalidOperationException("dpkg fixture")).Stage=="package_unpacking","package phase did not advance");
 var priorityHandler=new FakeHandler();var priorityDirectory=Path.Combine(directory,"priority");
 using(var priority=new ClientServices(priorityDirectory,priorityHandler)){
  await priority.SetConsentAsync(false,true);
  priority.SetDiagnosticConsent(true);
  for(int i=0;i<25;i++)await priority.SendDiagnosticAsync(new DiagnosticTrace("chrome",priority.DiagnosticGeneration),null,default);
  Assert(priorityHandler.Bodies.Count==1,"duplicate successful reports were sent");
  for(int i=0;i<25;i++)await priority.SendDiagnosticAsync(priority.BeginDiagnostic("resource_stats",background:true)!,null,default);
  Assert(priorityHandler.Bodies.Count==2,"poll blocked a separate feature or consumed success quota");
  var failure=new DiagnosticTrace("mount_apply",priority.DiagnosticGeneration);failure.Observe("任务请求已上传，正在启动远端操作…");
  string? issue=priority.RecordFailure(failure,new SafeCode.Core.RemoteOperationException("private","mount_failed"));
  await priority.SendDiagnosticAsync(failure,new SafeCode.Core.RemoteOperationException("private","mount_failed"),default);
  await priority.SendFeedbackAsync("用户描述最近失败",default);
  using var diagnosticPacket=JsonDocument.Parse(priorityHandler.Bodies[2]);using var feedbackPacket=JsonDocument.Parse(priorityHandler.Bodies[3]);
  var correlation=diagnosticPacket.RootElement.GetProperty("context");
  Assert(issue==correlation.GetProperty("operationId").GetString(),"displayed issue lost");
  Assert(JsonElement.DeepEquals(feedbackPacket.RootElement.GetProperty("context").GetProperty("correlation"),correlation),"feedback not linked to diagnostic");
  Assert(feedbackPacket.RootElement.GetProperty("context").GetProperty("report").GetProperty("stage").GetString()=="request_uploaded","failure stage lost");
  for(int i=0;i<25;i++){
   var pollTrace=priority.BeginDiagnostic("gateway_status",background:true)!;
   Assert(priority.RecordFailure(pollTrace,new IOException("private endpoint"))==null,"background error replaced foreground issue");
   await priority.SendDiagnosticAsync(pollTrace,new IOException("private endpoint"),default);
  }
  Assert(priorityHandler.Bodies.Count==5,"duplicate poll failures exhausted reporting quota");
  await priority.SendFeedbackAsync("后台轮询后保留用户操作",default);
  using var afterBackground=JsonDocument.Parse(priorityHandler.Bodies[5]);
  Assert(JsonElement.DeepEquals(afterBackground.RootElement.GetProperty("context").GetProperty("correlation"),correlation),"background failure replaced feedback correlation");
  string install=correlation.GetProperty("installationId").GetString()!;
  using(var reload=new ClientServices(priorityDirectory,new FakeHandler())){
   await reload.SetConsentAsync(false,true);var h=new DiagnosticTrace("chrome",reload.DiagnosticGeneration);
   // 标识持久化由偏好文件核验，会话编号每次启动变化。
   using var prefs=JsonDocument.Parse(File.ReadAllText(Path.Combine(priorityDirectory,"usage-preferences.json")));
   Assert(prefs.RootElement.GetProperty("diagnosticInstallationId").GetString()==install,"diagnostic installation not persisted");
  }
  priority.SetDiagnosticConsent(false);await priority.SendFeedbackAsync("关闭诊断后的反馈",default);
  using var optedOut=JsonDocument.Parse(priorityHandler.Bodies[6]);Assert(optedOut.RootElement.GetProperty("context").ValueKind==JsonValueKind.Null,"optout feedback leaked correlation");
  using var cleared=JsonDocument.Parse(File.ReadAllText(Path.Combine(priorityDirectory,"usage-preferences.json")));Assert(cleared.RootElement.GetProperty("diagnosticInstallationId").GetString()=="","optout retained diagnostic identifier");
 }
 var migratedDirectory=Path.Combine(directory,"diagnostic-migration");Directory.CreateDirectory(migratedDirectory);
 File.WriteAllText(Path.Combine(migratedDirectory,"usage-preferences.json"),"{\"enabled\":true,\"diagnosticsEnabled\":true,\"confirmed\":true,\"diagnosticNoticeVersion\":2}");
 var legacyHandler=new FakeHandler();
 using(var migrated=new ClientServices(migratedDirectory,legacyHandler)){
  await migrated.ConfirmConsentAsync();
  await migrated.SendDiagnosticAsync(new DiagnosticTrace("chrome",migrated.DiagnosticGeneration),null,default);
  Assert(migrated.NoticeWasConfirmed&&!migrated.DiagnosticsAllowed&&legacyHandler.Bodies.Count==0,"legacy defaults re-enabled reporting at startup");
  migrated.SetDiagnosticConsent(true);
 }
 using(var optedIn=new ClientServices(migratedDirectory,new FakeHandler()))
  Assert(optedIn.DiagnosticsAllowed,"explicit selection did not survive restart");
 var previousScope=Path.Combine(directory,"previous-diagnostic-scope");Directory.CreateDirectory(previousScope);
 File.WriteAllText(Path.Combine(previousScope,"usage-preferences.json"),"{\"enabled\":true,\"diagnosticsEnabled\":true,\"confirmed\":true,\"diagnosticNoticeVersion\":2,\"optInVersion\":1,\"diagnosticInstallationId\":\""+Guid.NewGuid()+"\"}");
 var scopeHandler=new FakeHandler();
 using(var expanded=new ClientServices(previousScope,scopeHandler)){
  Assert(expanded.BeginDiagnostic("gateway_enable")==null,"old installation opt-in authorized wider scope");
  await expanded.ConfirmConsentAsync();
  Assert(scopeHandler.Bodies.Count==1,"scope migration lost separately authorized launch statistics");
  using var launch=JsonDocument.Parse(scopeHandler.Bodies[0]);
  Assert(launch.RootElement.GetProperty("eventType").GetString()=="launch","unexpected initial report after scope migration");
  expanded.SetDiagnosticConsent(true);
  await expanded.SendDiagnosticAsync(expanded.BeginDiagnostic("gateway_enable")!,null,default);
  Assert(scopeHandler.Bodies.Count==2,"new diagnostic opt-in not applied");
  using var newDiagnostic=JsonDocument.Parse(scopeHandler.Bodies[1]);
  Assert(newDiagnostic.RootElement.GetProperty("report").GetProperty("operation").GetString()=="gateway_enable","unexpected new diagnostic operation");
 }
 var saveFailureDirectory=Path.Combine(directory,"diagnostic-save-failure");
 using(var saveFailure=new ClientServices(saveFailureDirectory,new FakeHandler()))
 {
  await saveFailure.SetConsentAsync(false,true);saveFailure.SetDiagnosticConsent(false);
  Directory.CreateDirectory(Path.Combine(saveFailureDirectory,"usage-preferences.json.new"));bool failed=false;
  try{saveFailure.SetDiagnosticConsent(true);}catch(Exception e)when(e is IOException or UnauthorizedAccessException){failed=true;}
  Assert(failed&&!saveFailure.DiagnosticsAllowed,"failed preference write enabled diagnostics");
 }
 var diagnosticBlocking=new BlockingHandler();using(var cancelling=new ClientServices(Path.Combine(directory,"diagnostic-cancel"),diagnosticBlocking))
 {
  await cancelling.SetConsentAsync(false,true);cancelling.SetDiagnosticConsent(true);
  var sending=cancelling.SendDiagnosticAsync(new DiagnosticTrace("chrome",cancelling.DiagnosticGeneration),null,default);
  await diagnosticBlocking.Started.Task;cancelling.SetDiagnosticConsent(false);await sending;Assert(diagnosticBlocking.Cancelled,"diagnostic optout did not cancel in-flight request");
 }
 Console.WriteLine("DIAGNOSTIC_PRIVACY_CONSENT_BOUNDS_RESTART_AND_CANCELLATION_CHECKS_PASSED");
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
sealed class UpdateHandler:HttpMessageHandler
{
 public int ApiRequests,AtomRequests,LatestRequests;public HttpStatusCode ApiStatus{get;set;}=HttpStatusCode.OK;public string ApiFeed{get;set;}="{}";public string AtomFeed{get;set;}="";public string? RetryAfter{get;set;}
 public string? LatestLocation{get;set;}
 protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,CancellationToken ct)
 {
  if(request.RequestUri!.Host=="api.github.com"){ApiRequests++;if(ApiStatus==HttpStatusCode.OK)return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK){Content=new StringContent(ApiFeed)});var response=new HttpResponseMessage(ApiStatus);if(RetryAfter!=null)response.Headers.TryAddWithoutValidation("Retry-After",RetryAfter);return Task.FromResult(response);}
  if(request.RequestUri.AbsoluteUri=="https://github.com/Muchuaner/safe-code/releases.atom"){AtomRequests++;return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK){Content=new StringContent(AtomFeed,Encoding.UTF8,"application/atom+xml")});}
  if(request.RequestUri.AbsoluteUri=="https://github.com/Muchuaner/safe-code/releases/latest"&&LatestLocation!=null){LatestRequests++;var response=new HttpResponseMessage(HttpStatusCode.Found);response.Headers.Location=new Uri(LatestLocation,UriKind.RelativeOrAbsolute);return Task.FromResult(response);}
  return Task.FromResult(new HttpResponseMessage(HttpStatusCode.NotFound));
 }
}
sealed class GatedUpdateHandler(string feed):HttpMessageHandler
{
 int apiRequests;
 public int ApiRequests=>Volatile.Read(ref apiRequests);
 public TaskCompletionSource Started{get;}=new(TaskCreationOptions.RunContinuationsAsynchronously);
 public TaskCompletionSource Release{get;}=new(TaskCreationOptions.RunContinuationsAsynchronously);
 protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,CancellationToken ct)
 {
  if(request.RequestUri!.Host!="api.github.com")return new HttpResponseMessage(HttpStatusCode.NotFound);
  Interlocked.Increment(ref apiRequests);Started.TrySetResult();await Release.Task.WaitAsync(ct);
  return new HttpResponseMessage(HttpStatusCode.OK){Content=new StringContent(feed)};
 }
}
sealed class SlowFallbackHandler:HttpMessageHandler
{
 public TaskCompletionSource Started{get;}=new(TaskCreationOptions.RunContinuationsAsynchronously);
 protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,CancellationToken ct)
 {
  if(request.RequestUri!.Host=="api.github.com")return Task.FromResult(new HttpResponseMessage(HttpStatusCode.Forbidden));
  return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK){Content=new StreamContent(new SlowReadStream(Started))});
 }
}
sealed class SlowDownloadHandler:HttpMessageHandler
{
 public TaskCompletionSource Started{get;}=new(TaskCreationOptions.RunContinuationsAsynchronously);
 protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,CancellationToken ct)=>Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK){Content=new StreamContent(new SlowReadStream(Started))});
}
sealed class SlowReadStream(TaskCompletionSource started):Stream
{
 int reads;
 public override bool CanRead=>true;public override bool CanSeek=>false;public override bool CanWrite=>false;public override long Length=>throw new NotSupportedException();
 public override long Position{get=>throw new NotSupportedException();set=>throw new NotSupportedException();}
 public override void Flush(){}
 public override int Read(byte[] buffer,int offset,int count)=>ReadAsync(buffer.AsMemory(offset,count),default).AsTask().GetAwaiter().GetResult();
 public override async ValueTask<int> ReadAsync(Memory<byte> buffer,CancellationToken ct){if(reads++==0){started.TrySetResult();buffer.Span[0]=0x4d;return 1;}await Task.Delay(Timeout.Infinite,ct);return 0;}
 public override long Seek(long offset,SeekOrigin origin)=>throw new NotSupportedException();public override void SetLength(long value)=>throw new NotSupportedException();public override void Write(byte[] buffer,int offset,int count)=>throw new NotSupportedException();
}
