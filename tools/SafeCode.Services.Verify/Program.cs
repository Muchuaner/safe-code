using System.Net;
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
 using(var client=new ClientServices(Path.Combine(directory,"download"),new FakeHandler(Feed(sha)),new FakeHandler(bytes:bytes),updateAssetSuffix:"Setup-x64.exe")){await client.CheckUpdateAsync(true,default);var file=await client.DownloadUpdateAsync((_,_)=>{},default);await ClientServices.VerifyInstallerAsync(file,default);File.AppendAllText(file.Path,"tampered");bool refused=false;try{await ClientServices.VerifyInstallerAsync(file,default);}catch(IOException){refused=true;}Assert(refused,"modified installer accepted");}
 using(var client=new ClientServices(Path.Combine(directory,"bad-hash"),new FakeHandler(Feed(new string('0',64))),new FakeHandler(bytes:bytes),updateAssetSuffix:"Setup-x64.exe")){await client.CheckUpdateAsync(true,default);bool refused=false;try{await client.DownloadUpdateAsync((_,_)=>{},default);}catch(IOException){refused=true;}Assert(refused,"bad digest accepted");Assert(!Directory.EnumerateFiles(Path.Combine(directory,"bad-hash"),"*.exe",SearchOption.AllDirectories).Any(),"invalid executable retained");}
 using(var client=new ClientServices(Path.Combine(directory,"redirect"),new FakeHandler(Feed(sha)),new FakeHandler(redirect:true),updateAssetSuffix:"Setup-x64.exe")){await client.CheckUpdateAsync(true,default);bool refused=false;try{await client.DownloadUpdateAsync((_,_)=>{},default);}catch(IOException){refused=true;}Assert(refused,"unsafe redirect accepted");}
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
