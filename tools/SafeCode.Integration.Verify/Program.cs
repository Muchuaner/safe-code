using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using SafeCode.Core;

void Assert(bool condition,string message){if(!condition)throw new Exception(message);}
static void AppendBytes(string path,byte[] bytes){using var stream=new FileStream(path,FileMode.Append,FileAccess.Write,FileShare.ReadWrite);stream.Write(bytes);}
static void Append(string path,string text)=>AppendBytes(path,Encoding.UTF8.GetBytes(text));
static int FindBytes(byte[] haystack,byte[] needle){for(int i=0;i<=haystack.Length-needle.Length;i++){bool match=true;for(int j=0;j<needle.Length;j++)if(haystack[i+j]!=needle[j]){match=false;break;}if(match)return i;}return -1;}
var relaxedJson=new JsonSerializerOptions{Encoder=JavaScriptEncoder.UnsafeRelaxedJsonEscaping};
string Assistant(string id,long input,long output,long cacheRead,long cacheWrite,string? note=null)
    =>JsonSerializer.Serialize(new{type="assistant",note,message=new{id,role="assistant",usage=new{input_tokens=input,output_tokens=output,cache_read_input_tokens=cacheRead,cache_creation_input_tokens=cacheWrite}}},relaxedJson);
string TokenCount(long input,long cached,long output,string? note=null)
    =>JsonSerializer.Serialize(new{type="event_msg",note,payload=new{type="token_count",info=new{total_token_usage=new{input_tokens=input,cached_input_tokens=cached,output_tokens=output}}}},relaxedJson);
static string SessionMeta(string id)=>JsonSerializer.Serialize(new{type="session_meta",payload=new{id,model_provider="safe_code_claude"}});

string root=Path.Combine(Path.GetTempPath(),"safe-code-integration-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(root);
try
{
    // Anthropic：同一 message.id 的流式快照只計一次，欄位只接受增長。
    var anthropic=new AnthropicUsageLedger();
    anthropic.Observe("m1",100,10,900,50);anthropic.Observe("m1",100,10,900,50);anthropic.Observe("m1",100,12,900,50);
    var a=anthropic.Snapshot();
    Assert(a.Available&&a.Input==100&&a.Output==12&&a.CacheRead==900&&a.CacheWrite==50,"anthropic dedup/cache counters wrong");
    Assert(a.Total==1062,"anthropic total must add input+cache read+cache write+output");
    // OpenAI/Codex：cached 已含在 input 中，總量不再加 cached。
    var codexLedger=new CodexUsageLedger();codexLedger.Observe("s1",1000,800,200);codexLedger.Observe("s1",1000,800,200);
    var c=codexLedger.Snapshot();
    Assert(c.Total==1200&&c.Input==1000&&c.CacheRead==800,"codex cached double-counted");
    codexLedger.Observe("s1",1500,1000,300);c=codexLedger.Snapshot();
    Assert(c.Total==1800&&c.Input==1500&&c.CacheRead==1000,"codex cumulative delta wrong");

    // 計時：停止後凍結；重新啟動載入同一狀態不重設開始時間。
    var start=new DateTimeOffset(2026,11,17,1,0,0,TimeSpan.Zero);
    var session=new IntegrationSession(start);
    Assert(session.ElapsedSeconds(start.AddSeconds(65))==65,"session elapsed wrong");
    var stopped=session.Stop(start.AddSeconds(70));
    Assert(!stopped.Active&&stopped.ElapsedSeconds(start.AddHours(2))==70,"stopped session did not freeze");
    Assert(new IntegrationSession(start).Active,"active session state wrong");
    Assert(IntegrationUsage.Unavailable("無來源").Available==false,"unavailable usage reported available");
    var localMidnight=new DateTime(2026,11,17,0,30,0,DateTimeKind.Unspecified);
    var localOffset=TimeZoneInfo.Local.GetUtcOffset(localMidnight);
    var localDto=new DateTimeOffset(localMidnight,localOffset);
    string dateFolder=CodexUsageSource.SessionDateFolder(root,localDto.LocalDateTime);
    Assert(dateFolder.EndsWith(Path.Combine("2026","11","17")),"Codex folder did not use local date");
    if(localDto.UtcDateTime.Date!=localMidnight.Date)Assert(!dateFolder.EndsWith(Path.Combine("2026","11","16")),"Codex folder used UTC date");
    Assert(!CodexUsageSource.TryParseTokenCount(TokenCount(1,0,1).Replace("\"input_tokens\":1","\"input_tokens\":9223372036854775808"),out _,out _,out _),"malformed Codex usage accepted");

    // 策略回滾：只回滾本工具寫過且當前仍等於寫入值的欄位。
    var original=new Dictionary<string,PolicyValue?>{["inferenceGatewayBaseUrl"]=PolicyValue.FromString("https://old.example/anthropic"),["inferenceGatewayApiKey"]=PolicyValue.FromString("old-key")};
    var written=new Dictionary<string,PolicyValue>{["inferenceGatewayBaseUrl"]=PolicyValue.FromString("https://new.example/anthropic"),["inferenceGatewayApiKey"]=PolicyValue.FromString("new-key")};
    var current=new Dictionary<string,PolicyValue>{["inferenceGatewayBaseUrl"]=PolicyValue.FromString("https://new.example/anthropic"),["inferenceGatewayApiKey"]=PolicyValue.FromString("new-key"),["unrelated"]=PolicyValue.FromDWord(1)};
    var restore=ClaudeDesktopPolicy.Restore(current,written,original);
    Assert(restore["inferenceGatewayBaseUrl"]?.Text=="https://old.example/anthropic"&&restore["inferenceGatewayApiKey"]?.Text=="old-key","policy restore did not restore managed fields");
    current["inferenceGatewayBaseUrl"]=PolicyValue.FromString("https://user-changed.example/anthropic");
    restore=ClaudeDesktopPolicy.Restore(current,written,original);
    Assert(!restore.ContainsKey("inferenceGatewayBaseUrl"),"policy restore overwrote later user change");
    Assert(restore["inferenceGatewayApiKey"]?.Text=="old-key","policy restore lost untouched key");
    Assert(ClaudeDesktopPolicy.Restore(new Dictionary<string,PolicyValue>(),new Dictionary<string,PolicyValue>(),original).Count==0,"takeover restore must not write fields");
    Assert(ClaudeDesktopPolicy.NormalizeEndpoint(" HTTPS://Gateway.Example:443/anthropic/ ")=="https://gateway.example/anthropic","endpoint normalization/default port failed");
    Assert(ClaudeDesktopPolicy.CanReuseCredential("https://gateway.example/anthropic","HTTPS://GATEWAY.EXAMPLE:443/anthropic/"),"same normalized endpoint should reuse credential");
    Assert(!ClaudeDesktopPolicy.CanReuseCredential("https://gateway.example/anthropic","https://gateway.example/other"),"path change reused credential");
    Assert(!ClaudeDesktopPolicy.CanReuseCredential("https://gateway.example/anthropic","https://evil.example/anthropic"),"host change reused credential");
    Assert(!ClaudeDesktopPolicy.CanReuseCredential("https://gateway.example:8443/anthropic","https://gateway.example/anthropic"),"port change reused credential");
    Assert(ClaudeDesktopPolicy.IsTakeover(true,true,true,true,"")&&!ClaudeDesktopPolicy.IsTakeover(true,true,false,true,""),"takeover model-change semantics wrong");
    Assert(ClaudeDesktopPolicy.CanReuseCredential("https://gateway.example/anthropic","https://gateway.example/anthropic")&&!ClaudeDesktopPolicy.IsTakeover(true,true,false,true,""),"same endpoint model change should reuse key but not takeover");
    Assert(ClaudeDesktopPolicy.StopNote(true,true).Contains("管理/統計")&&ClaudeDesktopPolicy.StopNote(true,true).Contains("仍有效"),"takeover stop note implied gateway shutdown");
    Assert(ClaudeDesktopPolicy.StopNote(false,true).Contains("恢復原配置")&&ClaudeDesktopPolicy.StopNote(false,true).Contains("仍可能繼續使用"),"restored gateway stop note misleading");
    Assert(ClaudeDesktopPolicy.StopNote(false,false).Contains("移除")&&ClaudeDesktopPolicy.StopNote(false,false).Contains("重新啟動"),"no-original stop note misleading");

    // Codex 真實 JSONL 夾具：基線、去重、增量、重新啟動後不重複。
    string codexRoot=Path.Combine(root,"codex","sessions");Directory.CreateDirectory(codexRoot);
    var codexStart=DateTimeOffset.Now.AddSeconds(-2);
    var codexSource=new CodexUsageSource(codexRoot,codexStart);
    await codexSource.InitializeAsync();
    Assert(!codexSource.Snapshot().Available,"codex source without provider session must be unavailable");
    string day=Path.Combine(codexRoot,DateTime.Today.ToString("yyyy"),DateTime.Today.ToString("MM"),DateTime.Today.ToString("dd"));Directory.CreateDirectory(day);
    string codexFile=Path.Combine(day,"s1.jsonl");
    File.WriteAllLines(codexFile,[SessionMeta("s1"),TokenCount(1000,800,200)]);
    await codexSource.SampleAsync();
    var cu=codexSource.Snapshot();
    Assert(cu.Available&&cu.Total==1200&&cu.CacheRead==800,"codex source first sample wrong");
    File.AppendAllLines(codexFile,[TokenCount(1000,800,200)]);
    await codexSource.SampleAsync();
    Assert(codexSource.Snapshot().Total==1200,"codex duplicate snapshot counted twice");
    File.AppendAllLines(codexFile,[TokenCount(1500,1000,300)]);
    await codexSource.SampleAsync();
    Assert(codexSource.Snapshot().Total==1800,"codex increment wrong");
    string partialToken=TokenCount(2000,1200,400);
    File.AppendAllText(codexFile,partialToken);
    await codexSource.SampleAsync();
    Assert(codexSource.Snapshot().Total==1800,"codex partial trailing line counted early");
    File.AppendAllText(codexFile,"\n");
    await codexSource.SampleAsync();
    Assert(codexSource.Snapshot().Total==2400,"codex partial trailing line lost");
    var codexState=codexSource.Export();
    var resumedCodex=new CodexUsageSource(codexRoot,codexStart,codexState);
    File.AppendAllLines(codexFile,[TokenCount(2000,1200,400)]);
    await resumedCodex.SampleAsync();
    Assert(resumedCodex.Snapshot().Total==2400,"codex resume double counted");

    // Claude 本地會話：接入前的舊行不計數；同一 id 快照只計一次；cache 計入總量。
    string claudeLocal=Path.Combine(root,"claude","local-agent-mode-sessions","s1");Directory.CreateDirectory(Path.Combine(claudeLocal,".claude","projects","session"));
    string claudeProjects=Path.Combine(root,"claude","projects");Directory.CreateDirectory(claudeProjects);
    string claudeFile=Path.Combine(claudeLocal,".claude","projects","session","c1.jsonl");
    File.WriteAllLines(claudeFile,[Assistant("old",9999,999,999,999)]);
    var claudeStart=DateTimeOffset.Now;
    var claudeSource=new ClaudeUsageSource(Path.Combine(root,"claude"),claudeProjects,claudeStart);
    await claudeSource.InitializeAsync();
    Assert(!claudeSource.Snapshot().Available&&claudeSource.Snapshot().Total==0,"claude pre-existing usage must be baselined and not faked as zero");
    File.AppendAllLines(claudeFile,[Assistant("m1",100,10,900,50),Assistant("m1",100,10,900,50),Assistant("m1",100,12,900,50),Assistant("m2",50,5,0,0)]);
    await claudeSource.SampleAsync();
    var cl=claudeSource.Snapshot();
    Assert(cl.Available&&cl.Input==150&&cl.Output==17&&cl.CacheRead==900&&cl.CacheWrite==50&&cl.Total==1117,"claude local usage/cache/dedup wrong");
    string partialAssistant=Assistant("m3",10,1,0,0);
    File.AppendAllText(claudeFile,partialAssistant);
    await claudeSource.SampleAsync();
    Assert(claudeSource.Snapshot().Total==1117,"claude partial trailing line counted early");
    File.AppendAllText(claudeFile,"\n");
    await claudeSource.SampleAsync();
    Assert(claudeSource.Snapshot().Total==1128,"claude partial trailing line lost");
    var claudeState=claudeSource.Export();
    var resumedClaude=new ClaudeUsageSource(Path.Combine(root,"claude"),claudeProjects,claudeStart,claudeState);
    File.AppendAllLines(claudeFile,[Assistant("m1",100,12,900,50)]);
    await resumedClaude.SampleAsync();
    Assert(resumedClaude.Snapshot().Total==1128,"claude resume double counted");
    string malformedClaude=Assistant("bad",1,1,0,0).Replace("\"input_tokens\":1","\"input_tokens\":9223372036854775808");
    File.AppendAllText(claudeFile,malformedClaude+"\n");await resumedClaude.SampleAsync();
    Assert(resumedClaude.Snapshot().Total==1128,"malformed Claude usage was counted or crashed");

    // 多檔案交錯半行、UTF8 跨位元組、匯出重建後追加半行：只計一次且不漏。
    string boundaryRoot=Path.Combine(root,"codex-boundary","sessions");Directory.CreateDirectory(boundaryRoot);
    var boundaryStart=DateTimeOffset.Now.AddSeconds(-2);
    var boundarySource=new CodexUsageSource(boundaryRoot,boundaryStart);await boundarySource.InitializeAsync();
    string boundaryDay=Path.Combine(boundaryRoot,DateTime.Today.ToString("yyyy"),DateTime.Today.ToString("MM"),DateTime.Today.ToString("dd"));Directory.CreateDirectory(boundaryDay);
    string bf1=Path.Combine(boundaryDay,"bf1.jsonl"),bf2=Path.Combine(boundaryDay,"bf2.jsonl"),bf3=Path.Combine(boundaryDay,"bf3.jsonl");
    File.WriteAllText(bf1,SessionMeta("bf1")+"\n");File.WriteAllText(bf2,SessionMeta("bf2")+"\n");
    string bl1=TokenCount(100,10,1),bl2=TokenCount(200,20,2);byte[] b1=Encoding.UTF8.GetBytes(bl1),b2=Encoding.UTF8.GetBytes(bl2);
    AppendBytes(bf1,b1[..(b1.Length/2)]);AppendBytes(bf2,b2[..(b2.Length/2)]);await boundarySource.SampleAsync();
    Assert(boundarySource.Snapshot().Total==0,"interleaved partial lines counted early");
    // 本地跨日：今天啟動，仍要掃到昨天最後兩分鐘的日期目錄。
    string crossRoot=Path.Combine(root,"codex-cross","sessions");Directory.CreateDirectory(crossRoot);
    var crossStart=new DateTimeOffset(DateTime.Today.AddDays(-1).AddHours(23).AddMinutes(58),TimeZoneInfo.Local.GetUtcOffset(DateTime.Today.AddDays(-1).AddHours(23).AddMinutes(58)));
    var crossSource=new CodexUsageSource(crossRoot,crossStart);await crossSource.InitializeAsync();
    string crossDay=Path.Combine(crossRoot,DateTime.Today.AddDays(-1).ToString("yyyy"),DateTime.Today.AddDays(-1).ToString("MM"),DateTime.Today.AddDays(-1).ToString("dd"));Directory.CreateDirectory(crossDay);
    string crossFile=Path.Combine(crossDay,"cross.jsonl");File.WriteAllLines(crossFile,[SessionMeta("cross"),TokenCount(10,0,1)]);await crossSource.SampleAsync();
    Assert(crossSource.Snapshot().Total==11,"local cross-day date folder not scanned");
    AppendBytes(bf2,b2[(b2.Length/2)..]);Append(bf2,"\n");AppendBytes(bf1,b1[(b1.Length/2)..]);Append(bf1,"\n");await boundarySource.SampleAsync();
    Assert(boundarySource.Snapshot().Total==303,"interleaved files lost usage");
    string utf8Line=TokenCount(300,30,3,"中文🙂");byte[] utf8=Encoding.UTF8.GetBytes(utf8Line);int mid=FindBytes(utf8,Encoding.UTF8.GetBytes("中"))+1;
    Assert(mid>0,"utf8 fixture missing");File.WriteAllText(bf3,SessionMeta("bf3")+"\n");AppendBytes(bf3,utf8[..mid]);await boundarySource.SampleAsync();Assert(boundarySource.Snapshot().Total==303,"utf8 prefix counted early");
    AppendBytes(bf3,utf8[mid..]);Append(bf3,"\n");await boundarySource.SampleAsync();Assert(boundarySource.Snapshot().Total==606,"utf8 split line lost");
    string restartLine=TokenCount(400,40,4);byte[] rb=Encoding.UTF8.GetBytes(restartLine);AppendBytes(bf1,rb[..(rb.Length/2)]);await boundarySource.SampleAsync();
    var boundaryState=boundarySource.Export();var resumedBoundary=new CodexUsageSource(boundaryRoot,boundaryStart,boundaryState);AppendBytes(bf1,rb[(rb.Length/2)..]);Append(bf1,"\n");await resumedBoundary.SampleAsync();
    Assert(resumedBoundary.Snapshot().Total==909,"restart half-line lost or double counted");
    Append(bf1,restartLine+"\n");await resumedBoundary.SampleAsync();Assert(resumedBoundary.Snapshot().Total==909,"restart duplicate counted");

    string chBoundaryLocal=Path.Combine(root,"claude-boundary","local-agent-mode-sessions","s");Directory.CreateDirectory(Path.Combine(chBoundaryLocal,".claude","projects","session"));
    string chBoundaryProjects=Path.Combine(root,"claude-boundary","projects");Directory.CreateDirectory(chBoundaryProjects);
    var chBoundaryStart=DateTimeOffset.Now.AddSeconds(-1);
    var chBoundarySource=new ClaudeUsageSource(Path.Combine(root,"claude-boundary"),chBoundaryProjects,chBoundaryStart);await chBoundarySource.InitializeAsync();
    string cf1=Path.Combine(chBoundaryLocal,".claude","projects","session","cf1.jsonl"),cf2=Path.Combine(chBoundaryLocal,".claude","projects","session","cf2.jsonl"),cf3=Path.Combine(chBoundaryLocal,".claude","projects","session","cf3.jsonl");
    string ca1=Assistant("ca1",100,10,0,0),ca2=Assistant("ca2",200,20,0,0);byte[] cb1=Encoding.UTF8.GetBytes(ca1),cb2=Encoding.UTF8.GetBytes(ca2);
    File.WriteAllText(cf1,"");File.WriteAllText(cf2,"");AppendBytes(cf1,cb1[..(cb1.Length/2)]);AppendBytes(cf2,cb2[..(cb2.Length/2)]);await chBoundarySource.SampleAsync();Assert(chBoundarySource.Snapshot().Total==0,"claude interleaved partial counted early");
    AppendBytes(cf2,cb2[(cb2.Length/2)..]);Append(cf2,"\n");AppendBytes(cf1,cb1[(cb1.Length/2)..]);Append(cf1,"\n");await chBoundarySource.SampleAsync();Assert(chBoundarySource.Snapshot().Total==330,"claude interleaved files lost usage");
    string cu8=Assistant("ca3",300,30,0,0,"中文🙂");byte[] c3=Encoding.UTF8.GetBytes(cu8);int cmid=FindBytes(c3,Encoding.UTF8.GetBytes("中"))+1;File.WriteAllText(cf3,"");AppendBytes(cf3,c3[..cmid]);await chBoundarySource.SampleAsync();Assert(chBoundarySource.Snapshot().Total==330,"claude utf8 prefix counted early");AppendBytes(cf3,c3[cmid..]);Append(cf3,"\n");await chBoundarySource.SampleAsync();Assert(chBoundarySource.Snapshot().Total==660,"claude utf8 split line lost");
    string cr=Assistant("ca4",400,40,0,0);byte[] crb=Encoding.UTF8.GetBytes(cr);AppendBytes(cf1,crb[..(crb.Length/2)]);await chBoundarySource.SampleAsync();var chState=chBoundarySource.Export();var chResume=new ClaudeUsageSource(Path.Combine(root,"claude-boundary"),chBoundaryProjects,chBoundaryStart,chState);AppendBytes(cf1,crb[(crb.Length/2)..]);Append(cf1,"\n");await chResume.SampleAsync();Assert(chResume.Snapshot().Total==1100,"claude restart half-line lost or double counted");Append(cf1,cr+"\n");await chResume.SampleAsync();Assert(chResume.Snapshot().Total==1100,"claude restart duplicate counted");

    // 快取：有 CliPaths 時不在空 projects 樹裏重新遞歸尋找。
    string cacheRoot=Path.Combine(root,"claude-cache"),cacheLocal=Path.Combine(cacheRoot,"local-agent-mode-sessions"),cacheProjects=Path.Combine(cacheRoot,"projects");
    string cacheMeta=Path.Combine(cacheRoot,"local-agent-mode-sessions","claude-code-sessions","s","local_test.json");Directory.CreateDirectory(Path.GetDirectoryName(cacheMeta)!);Directory.CreateDirectory(cacheProjects);
    string cacheId="11111111-1111-1111-1111-111111111111";File.WriteAllText(cacheMeta,"{\"cliSessionId\":\""+cacheId+"\"}");
    string cachePath=Path.Combine(cacheRoot,"cached","transcript.jsonl");Directory.CreateDirectory(Path.GetDirectoryName(cachePath)!);File.WriteAllText(cachePath,Assistant("cache0",1,1,0,0)+"\n");
    var cacheStart=DateTimeOffset.Now;var cacheState=new ClaudeUsageState{SourceAvailable=true,CliSessionIds=[cacheId],CliPaths=new(){[cacheId]=cachePath}};
    var cacheSource=new ClaudeUsageSource(cacheRoot,cacheProjects,cacheStart,cacheState);await cacheSource.InitializeAsync();File.AppendAllText(cachePath,Assistant("cache1",10,1,0,0)+"\n");await cacheSource.SampleAsync();Assert(cacheSource.Snapshot().Total==11,"cached CliPath was not used");

    // 無來源時必須顯式不可用，不能顯示假 0。
    var emptyClaude=new ClaudeUsageSource(Path.Combine(root,"missing"),Path.Combine(root,"missing-projects"),DateTimeOffset.UtcNow);
    await emptyClaude.InitializeAsync();await emptyClaude.SampleAsync();
    Assert(!emptyClaude.Snapshot().Available&&emptyClaude.Snapshot().Total==0,"missing claude source faked zero");

    // 系統匣：僅活動時顯示；兩個詳情獨立；時長停止後凍結；不可用有文字。
    var now=start.AddHours(1);
    var hidden=IntegrationTrayPresenter.Codex(false,false,null,null,null,"",now);
    Assert(!hidden.Visible,"inactive codex section visible");
    var visible=IntegrationTrayPresenter.Codex(true,true,start,start.AddSeconds(70),IntegrationUsage.FromCodex(1000,800,200), "fixture", now);
    Assert(visible.Visible&&visible.Duration.Contains("00:01:10")&&visible.Detail.Contains("快取 800")&&!visible.Duration.Contains("2,000"),"codex tray formatting wrong");
    var desktop=IntegrationTrayPresenter.Desktop(true,start,null,IntegrationUsage.FromAnthropic(100,10,900,50),"fixture",start.AddSeconds(5));
    Assert(desktop.Visible&&desktop.Duration.Contains("00:00:05")&&desktop.Detail.Contains("快取讀 900")&&desktop.Detail.Contains("寫 50"),"desktop tray formatting wrong");
    var unavailable=IntegrationTrayPresenter.Desktop(true,start,null,IntegrationUsage.Unavailable("無官方會話用量"),"",start.AddSeconds(5));
    Assert(unavailable.Detail=="無官方會話用量"&&unavailable.Duration.Contains("暫不可用"),"unavailable tray state wrong");
    Assert(!IntegrationTrayPresenter.Desktop(false,start,null,null,"",now).Visible,"inactive desktop section visible");

    Console.WriteLine("INTEGRATION_USAGE_DEDUP_CACHE_TIMING_RESTORE_UNAVAILABLE_TRAY_OK");
}
finally
{
    try{Directory.Delete(root,true);}catch{}
}
