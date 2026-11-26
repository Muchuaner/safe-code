using System.IO;
using System.Diagnostics;
using System.Net;
using System.Net.Http;
using System.Net.Http.Json;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using System.Xml;
using System.Xml.Linq;
using SafeCode.Core;

namespace SafeCode.Desktop;

internal sealed class UsagePreferences
{
    public bool Enabled {get;set;}
    public bool DiagnosticsEnabled {get;set;}
    public int OptInVersion {get;set;}
    public int DiagnosticScopeVersion {get;set;}
    public bool Confirmed {get;set;}
    public int DiagnosticNoticeVersion {get;set;}
    public string InstallationId {get;set;}="";
    public string DiagnosticInstallationId {get;set;}="";
}
internal sealed record DiagnosticContext(string InstallationId,string SessionId,string OperationId);
internal sealed record FeedbackContext(DiagnosticContext Correlation,DiagnosticReport Report);
internal sealed record DownloadedInstaller(string Path,string Digest);
internal sealed record UpdateInfo(string CurrentVersion,string? Version,string Status,string? Page,bool CanInstall=false);
internal sealed class ClientServices : IDisposable
{
    private const string Repository="Muchuaner/safe-code";
    private const string ReleasesPage="https://github.com/"+Repository+"/releases";
    private const string ReleasesAtom=ReleasesPage+".atom";
    private const string ReleaseTagPrefix=ReleasesPage+"/tag/";
    private const string ReleasesFeedId="tag:github.com,2008:"+ReleasesPage;
    private const string AtomNamespace="http://www.w3.org/2005/Atom";
    private const string UnknownUpdateStatus="GitHub 暫時限制了更新查詢，且無法從官方發佈頁確認版本，請稍後重試。";
    private const int ExplicitOptInVersion=1;
    private const int CurrentDiagnosticScope=2;
    private readonly string preferencesPath;
    private readonly string directory;
    private readonly string updateAssetSuffix;
    private readonly string currentVersion;
    private readonly Uri? endpoint;
    private readonly HttpClient http;
    private readonly HttpMessageHandler? downloadTransport;
    private UsagePreferences preferences;
    private CancellationTokenSource reporting=new();
    private bool counted;
    private CancellationTokenSource diagnosticReporting=new();
    private int diagnosticGeneration;
    private readonly object diagnosticGate=new();
    private readonly HashSet<string> reportedSuccesses=new(StringComparer.Ordinal);
    private readonly HashSet<(string Operation,string Error)> sampledBackgroundErrors=[];
    private long diagnosticWindow=Stopwatch.GetTimestamp();
    private int diagnosticSuccessCount,diagnosticFailureCount,backgroundSuccessCount,backgroundFailureCount;
    private bool disposed;
    private string diagnosticSessionId=Guid.NewGuid().ToString();
    private FeedbackContext? latestFailure;
    private DateTimeOffset latestFailureAt;
    private FeedbackContext? RecentFailure=>DiagnosticsAllowed&&DateTimeOffset.UtcNow-latestFailureAt<TimeSpan.FromHours(24)?latestFailure:null;
    private readonly SemaphoreSlim diagnosticSlots=new(2,2);
    private readonly string eventId=Guid.NewGuid().ToString();
    private sealed record ReleaseAsset(string Version,string Url,string Digest,long Size);
    private ReleaseAsset? release;
    private DateTimeOffset lastCheck;
    private DateTimeOffset apiBlockedUntil;
    private UpdateInfo? lastUpdate;
    private bool lastUpdateFromFallback;
    private readonly SemaphoreSlim updateCheckGate=new(1,1);
    public static string Version=>Assembly.GetExecutingAssembly().GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion.Split('+')[0]??Assembly.GetExecutingAssembly().GetName().Version?.ToString(3)??"0.4.3";
    public ClientServices(string directory,HttpMessageHandler? transport=null,HttpMessageHandler? downloadTransport=null,string? updateAssetSuffix=null,bool endpointEnabled=true,string? currentVersion=null)
    {
        http=transport==null?new HttpClient(new HttpClientHandler{AllowAutoRedirect=false}):new HttpClient(transport);http.MaxResponseContentBufferSize=1048576;http.Timeout=TimeSpan.FromSeconds(15);this.downloadTransport=downloadTransport;this.updateAssetSuffix=updateAssetSuffix??AssetSuffix;this.currentVersion=string.IsNullOrWhiteSpace(currentVersion)?Version:currentVersion;
        this.directory=directory;Directory.CreateDirectory(directory);preferencesPath=Path.Combine(directory,"usage-preferences.json");
        try{preferences=JsonSerializer.Deserialize<UsagePreferences>(File.ReadAllText(preferencesPath),JsonDefaults.Options)??new();}catch(IOException){preferences=new();}catch(JsonException){preferences=new();}
        if(preferences.OptInVersion<ExplicitOptInVersion)
        {
            // Older records cannot distinguish default selections from explicit opt-ins.
            preferences.Enabled=false;
            preferences.DiagnosticsEnabled=false;
            preferences.InstallationId="";
            preferences.DiagnosticInstallationId="";
            preferences.OptInVersion=ExplicitOptInVersion;
        }
        if(preferences.DiagnosticScopeVersion<CurrentDiagnosticScope)
        {
            // Consent to installation diagnostics is not consent to feature diagnostics.
            preferences.DiagnosticsEnabled=false;
            preferences.DiagnosticInstallationId="";
            preferences.DiagnosticScopeVersion=CurrentDiagnosticScope;
        }
        http.DefaultRequestHeaders.UserAgent.ParseAdd("SafeCode/"+this.currentVersion);
        string config=Path.Combine(AppContext.BaseDirectory,"service-endpoints.json");
        try{if(endpointEnabled&&File.Exists(config)){using var doc=JsonDocument.Parse(File.ReadAllText(config));if(doc.RootElement.TryGetProperty("feedbackBaseUrl",out var value)&&Uri.TryCreate(value.GetString(),UriKind.Absolute,out var uri)&&uri.Scheme=="https"&&!uri.IsLoopback&&string.IsNullOrEmpty(uri.UserInfo)&&uri.AbsolutePath=="/"&&string.IsNullOrEmpty(uri.Query)&&string.IsNullOrEmpty(uri.Fragment))endpoint=uri;}}catch(IOException){}catch(JsonException){}catch(InvalidOperationException){}
    }
    public bool NoticeWasConfirmed=>preferences.Confirmed&&preferences.DiagnosticNoticeVersion>=2;
    public object Preferences()
    {
        lock(diagnosticGate)return new{enabled=preferences.Enabled,confirmed=preferences.Confirmed,diagnosticsEnabled=preferences.DiagnosticsEnabled,serviceAvailable=endpoint!=null,recipient=endpoint?.Host,issueId=RecentFailure?.Correlation.OperationId};
    }
    public Task<object> ConfirmConsentAsync(){lock(diagnosticGate)return SetConsentAsync(preferences.Enabled,true);}
    private void Save(){string temp=preferencesPath+".new";File.WriteAllText(temp,JsonSerializer.Serialize(preferences,JsonDefaults.Options));File.Move(temp,preferencesPath,true);}
    public async Task<object> SetConsentAsync(bool enabled,bool confirmed)
    {
        lock(diagnosticGate)
        {
            bool previousConfirmation=preferences.Confirmed;
            int previousNoticeVersion=preferences.DiagnosticNoticeVersion;
            preferences.Enabled=enabled;preferences.Confirmed|=confirmed;
            if(confirmed)preferences.DiagnosticNoticeVersion=2;
            if(!enabled){reporting.Cancel();reporting.Dispose();reporting=new();preferences.InstallationId="";}
            try{Save();}catch{preferences.Enabled=false;preferences.Confirmed=previousConfirmation;preferences.DiagnosticNoticeVersion=previousNoticeVersion;throw;}
        }
        if(enabled&&NoticeWasConfirmed)await CountLaunchAsync();
        return Preferences();
    }
    public bool DiagnosticsAllowed
    {
        get{lock(diagnosticGate)return !disposed&&endpoint!=null&&NoticeWasConfirmed&&preferences.DiagnosticsEnabled;}
    }
    public int DiagnosticGeneration=>Volatile.Read(ref diagnosticGeneration);
    public DiagnosticTrace? BeginDiagnostic(string operation,bool background=false)
    {
        lock(diagnosticGate)
            return DiagnosticsAllowed&&DiagnosticTrace.Operations.Contains(operation)?new(operation,diagnosticGeneration,background):null;
    }
    public object SetDiagnosticConsent(bool enabled)
    {
        lock(diagnosticGate)
        {
            preferences.DiagnosticsEnabled=enabled;
            if(!enabled){preferences.DiagnosticInstallationId="";latestFailure=null;diagnosticSessionId=Guid.NewGuid().ToString();}
            diagnosticGeneration++;
            diagnosticReporting.Cancel();diagnosticReporting.Dispose();diagnosticReporting=new();
            try{Save();}catch{preferences.DiagnosticsEnabled=false;throw;}
            return Preferences();
        }
    }
    private DiagnosticContext Correlation(DiagnosticTrace trace)
    {
        if(!Guid.TryParse(preferences.DiagnosticInstallationId,out _)){preferences.DiagnosticInstallationId=Guid.NewGuid().ToString();Save();}
        return new(preferences.DiagnosticInstallationId,diagnosticSessionId,trace.Id);
    }
    public string? RecordFailure(DiagnosticTrace trace,Exception error)
    {
        lock(diagnosticGate)
        {
            if(trace.Background||!CanReport(trace)||DiagnosticTrace.ErrorKind(error)=="cancelled")return null;
            if(latestFailure?.Correlation.OperationId==trace.Id)return trace.Id;
            try{latestFailure=new(Correlation(trace),trace.Finish(error));latestFailureAt=DateTimeOffset.UtcNow;return trace.Id;}catch{return null;}
        }
    }
    private bool CanReport(DiagnosticTrace trace)=>DiagnosticsAllowed&&trace.Generation==diagnosticGeneration&&DiagnosticTrace.Operations.Contains(trace.Operation);
    private bool ReserveDiagnostic(DiagnosticTrace trace,DiagnosticReport report)
    {
        if(Stopwatch.GetElapsedTime(diagnosticWindow)>=TimeSpan.FromHours(1))
        {
            diagnosticWindow=Stopwatch.GetTimestamp();
            diagnosticSuccessCount=diagnosticFailureCount=backgroundSuccessCount=backgroundFailureCount=0;
            sampledBackgroundErrors.Clear();
        }
        if(report.Outcome=="success")
        {
            if(diagnosticSuccessCount>=20||(trace.Background&&backgroundSuccessCount>=8)||!reportedSuccesses.Add(trace.Operation))return false;
            diagnosticSuccessCount++;
            if(trace.Background)backgroundSuccessCount++;
        }
        else
        {
            if(diagnosticFailureCount>=18)return false;
            if(trace.Background&&(report.Outcome=="cancelled"||backgroundFailureCount>=6||
                !sampledBackgroundErrors.Add((trace.Operation,report.ErrorKind))))return false;
            diagnosticFailureCount++;
            if(trace.Background)backgroundFailureCount++;
        }
        return true;
    }
    public async Task<T> WithDiagnosticAsync<T>(string operation,Func<Task<T>> run,CancellationToken ct,bool background=false)
    {
        var trace=BeginDiagnostic(operation,background);
        Exception? failure=null;
        try{return await run();}
        catch(Exception error){failure=error;throw;}
        finally{if(trace!=null)_=SendDiagnosticAsync(trace,failure,ct);}
    }
    public async Task SendDiagnosticAsync(DiagnosticTrace trace,Exception? error,CancellationToken ct)
    {
        CancellationTokenSource? deadline=null;
        bool acquired=false;
        try
        {
            DiagnosticReport report;
            DiagnosticContext context;
            lock(diagnosticGate)
            {
                if(!CanReport(trace)||ct.IsCancellationRequested)return;
                report=trace.Finish(error);
                if(error!=null)RecordFailure(trace,error);
                if(!ReserveDiagnostic(trace,report))return;
                context=Correlation(trace);
                deadline=CancellationTokenSource.CreateLinkedTokenSource(ct,diagnosticReporting.Token);
                deadline.CancelAfter(TimeSpan.FromSeconds(12));
            }
            var body=new{schema=report.Gateway==null?2:3,eventId=trace.Id,version=Version,context,report};
            await diagnosticSlots.WaitAsync(deadline.Token);acquired=true;
            for(int attempt=0;attempt<2;attempt++)
            {
                if(!DiagnosticsAllowed||trace.Generation!=diagnosticGeneration||deadline.IsCancellationRequested)return;
                try{using var response=await http.PostAsJsonAsync(new Uri(endpoint!,"v1/diagnostics"),body,JsonDefaults.Options,deadline.Token);if(response.IsSuccessStatusCode||(int)response.StatusCode<500)return;}
                catch(HttpRequestException){}
                if(attempt==0)await Task.Delay(1000,deadline.Token);
            }
        }
        catch(Exception){} // 診斷髮送永遠不能改變部署結果或彈出第二個錯誤。
        finally{deadline?.Dispose();if(acquired)diagnosticSlots.Release();}
    }
    private async Task CountLaunchAsync()
    {
        CancellationToken token;string installationId;
        lock(diagnosticGate)
        {
            if(disposed||counted||endpoint==null||!preferences.Enabled||!NoticeWasConfirmed)return;
            if(!Guid.TryParse(preferences.InstallationId,out _)){preferences.InstallationId=Guid.NewGuid().ToString();Save();}
            counted=true;token=reporting.Token;installationId=preferences.InstallationId;
        }
        // 固定欄位白名單：無 VPS 地址、登入名、機器名、密碼或項目路徑。
        var body=new{schema=1,eventId,installationId,version=Version,os=OperatingSystem.IsMacOS()?"macOS":"Windows",eventType="launch"};
        for(int attempt=0;attempt<3;attempt++){
            if(token.IsCancellationRequested||!preferences.Enabled)return;
            try{using var response=await http.PostAsJsonAsync(new Uri(endpoint,"v1/usage"),body,JsonDefaults.Options,token);if(response.IsSuccessStatusCode||((int)response.StatusCode<500&&response.StatusCode!=HttpStatusCode.TooManyRequests))return;}
            catch(HttpRequestException){}catch(OperationCanceledException){if(token.IsCancellationRequested)return;}
            if(attempt<2)try{await Task.Delay(TimeSpan.FromSeconds(2*(attempt+1)),token);}catch(OperationCanceledException){return;}
        }
    }
    public async Task SendFeedbackAsync(string message,CancellationToken ct)
    {
        if(endpoint==null)throw new InvalidOperationException("回饋服務尚未啟用，請暫時透過 QQ 群回饋。");
        message=message.Trim();if(message.Length<5||message.Length>4000)throw new ArgumentException("回饋內容須為 5–4000 字。");
        FeedbackContext? context;
        lock(diagnosticGate)context=RecentFailure;
        using var response=await http.PostAsJsonAsync(new Uri(endpoint,"v1/feedback"),new{schema=context?.Report.Gateway==null?2:3,eventId=Guid.NewGuid().ToString(),version=Version,message,context},JsonDefaults.Options,ct);
        if(!response.IsSuccessStatusCode)throw new IOException(response.StatusCode==HttpStatusCode.TooManyRequests?"提交較頻繁，請稍後重試。":"回饋暫未送達，請稍後重試。");
    }
    public async Task<UpdateInfo> CheckUpdateAsync(bool force,CancellationToken ct)
    {
        await updateCheckGate.WaitAsync(ct);
        try
        {
            var now=DateTimeOffset.UtcNow;
            if(lastUpdate!=null&&now-lastCheck<TimeSpan.FromSeconds(force?30:43200)
                &&(!lastUpdateFromFallback||now<apiBlockedUntil))return lastUpdate;
            // A refresh must not let a previous success survive a failed request.
            lastUpdate=null;release=null;
            bool betaChannel=ReleaseNumber.Parse(currentVersion)?.Beta==true;
            if(now>=apiBlockedUntil)
            {
                using var request=new HttpRequestMessage(HttpMethod.Get,"https://api.github.com/repos/"+Repository+"/releases?per_page=100");request.Headers.Accept.ParseAdd("application/vnd.github+json");
                using var response=await http.SendAsync(request,ct);
                if(response.StatusCode is HttpStatusCode.Forbidden or HttpStatusCode.TooManyRequests)
                {
                    apiBlockedUntil=RateLimitUntil(response);
                }
                else
                {
                    apiBlockedUntil=DateTimeOffset.MinValue;
                    if(response.StatusCode==HttpStatusCode.NotFound)return CacheUpdate(new(currentVersion,null,betaChannel?"尚未發佈測試版本":"尚未發佈正式版本",null));
                    response.EnsureSuccessStatusCode();using var doc=JsonDocument.Parse(await response.Content.ReadAsStringAsync(ct));var root=doc.RootElement;
                    return CacheUpdate(SelectPlatformUpdate(root,betaChannel));
                }
            }
            var fallback=await CheckReleaseFallbackAsync(betaChannel,ct);
            if(fallback.Status==UnknownUpdateStatus)return fallback;
            return CacheUpdate(fallback,fromFallback:true);
        }
        finally{updateCheckGate.Release();}
    }
    private UpdateInfo SelectPlatformUpdate(JsonElement releases,bool betaChannel)
    {
        ReleaseNumber? bestNumber=null;
        UpdateInfo? bestUpdate=null;
        foreach(var candidate in releases.EnumerateArray())
        {
            if(candidate.GetProperty("draft").GetBoolean())continue;
            var number=ReleaseNumber.Parse(candidate.GetProperty("tag_name").GetString()??"");
            if(number==null||number.Beta&&!betaChannel||candidate.GetProperty("prerelease").GetBoolean()&&!number.Beta
                ||number.CompareTo(bestNumber)<=0)continue;
            var update=ParseRelease(candidate,currentVersion,updateAssetSuffix);
            if(!update.CanInstall)continue;
            string name="SafeCode-"+update.Version+"-"+updateAssetSuffix;
            var asset=candidate.GetProperty("assets").EnumerateArray().First(x=>x.GetProperty("name").GetString()==name);
            release=new(update.Version!,asset.GetProperty("browser_download_url").GetString()!,asset.GetProperty("digest").GetString()![7..],asset.GetProperty("size").GetInt64());
            bestNumber=number;
            bestUpdate=update;
        }
        return bestUpdate??new(currentVersion,null,"未發現適用於此平台的新版本",null);
    }
    private UpdateInfo CacheUpdate(UpdateInfo update,bool fromFallback=false)
    {
        lastCheck=DateTimeOffset.UtcNow;
        lastUpdateFromFallback=fromFallback;
        return lastUpdate=update;
    }
    private static DateTimeOffset RateLimitUntil(HttpResponseMessage response)
    {
        var now=DateTimeOffset.UtcNow;var until=now+TimeSpan.FromMinutes(5);bool parsed=false;
        if(response.Headers.RetryAfter is {} retry)
        {
            if(retry.Delta is {} delta){until=delta>DateTimeOffset.MaxValue-now?DateTimeOffset.MaxValue:now+delta;parsed=true;}
            else if(retry.Date is {} date){until=date;parsed=true;}
        }
        if(!parsed&&response.Headers.TryGetValues("X-RateLimit-Reset",out var values)&&long.TryParse(values.FirstOrDefault(),out var seconds)&&seconds>0)
        {
            try{until=DateTimeOffset.FromUnixTimeSeconds(seconds);parsed=true;}catch(ArgumentOutOfRangeException){}
        }
        if(!parsed||until<=now)until=now+TimeSpan.FromMinutes(5);
        return until;
    }
    private async Task<UpdateInfo> CheckReleaseFallbackAsync(bool betaChannel,CancellationToken ct)
    {
        try
        {
            var installed=ReleaseNumber.Parse(currentVersion);
            if(installed==null)return UnknownFallback();
            using var request=new HttpRequestMessage(HttpMethod.Get,betaChannel?ReleasesAtom:ReleasesPage+"/latest");
            if(betaChannel)request.Headers.Accept.ParseAdd("application/atom+xml, application/xml;q=0.9");
            using var response=await http.SendAsync(request,ct);
            if(!betaChannel)
            {
                // GitHub's latest release URL identifies the formal release independently of its tag name.
                if(response.StatusCode!=HttpStatusCode.Found||response.Headers.Location is not {} location)return UnknownFallback();
                var tag=TagFromReleaseLink(new Uri(request.RequestUri!,location).AbsoluteUri);
                var number=ReleaseNumber.Parse(tag??"");
                return number is {Beta:false} ? await ConfirmFallbackAssetAsync(FallbackResult(installed,number,tag!,"官方發佈頁未發現更新"),ct) : UnknownFallback();
            }
            response.EnsureSuccessStatusCode();
            return await ConfirmFallbackAssetAsync(ParseAtomRelease(await response.Content.ReadAsStringAsync(ct),installed),ct);
        }
        catch(OperationCanceledException) when(ct.IsCancellationRequested){throw;}
        catch(Exception e) when(e is HttpRequestException or IOException or XmlException or UriFormatException or OperationCanceledException){return UnknownFallback();}
    }
    private async Task<UpdateInfo> ConfirmFallbackAssetAsync(UpdateInfo update,CancellationToken ct)
    {
        if(update.Version==null)return update;
        string tag=TagFromReleaseLink(update.Page!)!;
        string name="SafeCode-"+update.Version+"-"+updateAssetSuffix;
        using var request=new HttpRequestMessage(HttpMethod.Head,ReleasesPage+"/download/"+Uri.EscapeDataString(tag)+"/"+name);
        using var response=await http.SendAsync(request,HttpCompletionOption.ResponseHeadersRead,ct);
        if(response.StatusCode==HttpStatusCode.OK)return update;
        if((int)response.StatusCode is >=300 and <=399&&response.Headers.Location is {IsAbsoluteUri:true} location
            &&location.Scheme=="https"&&location.UserInfo==""&&
            location.Host is "release-assets.githubusercontent.com" or "objects.githubusercontent.com")return update;
        return UnknownFallback();
    }
    private UpdateInfo UnknownFallback()=>new(currentVersion,null,UnknownUpdateStatus,ReleasesPage);
    private UpdateInfo ParseAtomRelease(string xml,ReleaseNumber installed)
    {
        XNamespace atom=AtomNamespace;
        using var reader=XmlReader.Create(new StringReader(xml),new XmlReaderSettings{DtdProcessing=DtdProcessing.Prohibit,XmlResolver=null,MaxCharactersInDocument=2097152});
        var feed=XDocument.Load(reader).Root;
        if(feed?.Name!=atom+"feed"||(string?)feed.Element(atom+"id")!=ReleasesFeedId)return UnknownFallback();

        ReleaseNumber? best=null;
        string? bestTag=null;
        foreach(var link in feed.Elements(atom+"entry").Elements(atom+"link"))
        {
            if(((string?)link.Attribute("rel")??"alternate")!="alternate")continue;
            var tag=TagFromReleaseLink((string?)link.Attribute("href"));
            var number=ReleaseNumber.Parse(tag??"");
            if(number!=null&&number.CompareTo(best)>0){best=number;bestTag=tag;}
        }
        // Atom has no prerelease flag. A newer tag is an advisory link to the official release page.
        return best==null ? UnknownFallback() : FallbackResult(installed,best,bestTag!,"官方發佈訂閱未發現更新");
    }
    private static string? TagFromReleaseLink(string? href) =>
        href?.StartsWith(ReleaseTagPrefix,StringComparison.Ordinal)==true
            ? Uri.UnescapeDataString(href[ReleaseTagPrefix.Length..]) : null;
    private UpdateInfo FallbackResult(ReleaseNumber installed,ReleaseNumber latest,string tag,string sameVersionStatus)
    {
        int comparison=latest.CompareTo(installed);
        if(comparison<0)return UnknownFallback();
        if(comparison==0)return new(currentVersion,null,sameVersionStatus,ReleasesPage);
        string version=tag.StartsWith('v')?tag[1..]:tag;
        return new(currentVersion,version,"發現新版本 "+version+"（請前往官方發佈頁確認並下載）",ReleaseTagPrefix+Uri.EscapeDataString(tag),false);
    }
    private static string AssetSuffix=>OperatingSystem.IsMacOS()?(System.Runtime.InteropServices.RuntimeInformation.ProcessArchitecture==System.Runtime.InteropServices.Architecture.Arm64?"macOS-arm64.dmg":"macOS-x64.dmg"):"Setup-x64.exe";
    internal static UpdateInfo ParseRelease(JsonElement root,string current,string assetSuffix="Setup-x64.exe")
    {
        string tag=root.GetProperty("tag_name").GetString()??"";string version=tag.StartsWith('v')?tag[1..]:tag;
        var latest=ReleaseNumber.Parse(version);var installed=ReleaseNumber.Parse(current);
        if(latest==null||installed==null||root.GetProperty("draft").GetBoolean()||(root.GetProperty("prerelease").GetBoolean()&&(!installed.Beta||!latest.Beta)))throw new IOException("GitHub 返回的版本資訊無效。");
        string page="https://github.com/"+Repository+"/releases/tag/"+Uri.EscapeDataString(tag);
        if(latest.CompareTo(installed)<=0)return new(current,null,"已是最新版本",page);
        string name="SafeCode-"+version+"-"+assetSuffix;bool valid=false;
        foreach(var asset in root.GetProperty("assets").EnumerateArray()){
            if(asset.GetProperty("name").GetString()!=name)continue;
            string digest=asset.TryGetProperty("digest",out var d)?d.GetString()??"":"";string url=asset.GetProperty("browser_download_url").GetString()??"";
            valid=System.Text.RegularExpressions.Regex.IsMatch(digest,"^sha256:[a-fA-F0-9]{64}$")&&url=="https://github.com/"+Repository+"/releases/download/"+tag+"/"+name&&asset.GetProperty("size").GetInt64() is >0 and <=1073741824;
        }
        return new(current,version,"發現新版本 "+version,page,valid);
    }
    public async Task<DownloadedInstaller> DownloadUpdateAsync(Action<long,long> progress,CancellationToken ct)
    {
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromMinutes(20));var downloadToken=deadline.Token;
        var item=release??throw new InvalidOperationException("請先檢查新版本；沒有可驗證的安裝包時可開啟 GitHub 下載頁。");
        string folder=Path.Combine(directory,"Updates",Guid.NewGuid().ToString("N"));Directory.CreateDirectory(folder);
        string file=Path.Combine(folder,"SafeCode-"+item.Version+"-"+updateAssetSuffix);
        using var download=downloadTransport==null?new HttpClient(new HttpClientHandler{AllowAutoRedirect=false}):new HttpClient(downloadTransport,false);download.Timeout=TimeSpan.FromMinutes(20);download.DefaultRequestHeaders.UserAgent.ParseAdd("SafeCode/"+currentVersion);
        Uri uri=new(item.Url);
        try{
            HttpResponseMessage? response=null;
            for(int i=0;i<6;i++){
                if(uri.Scheme!="https"||uri.UserInfo!=""||!(uri.Host=="github.com"||uri.Host=="release-assets.githubusercontent.com"||uri.Host=="objects.githubusercontent.com"))throw new IOException("安裝包跳轉到了未允許的下載地址。");
                response=await download.GetAsync(uri,HttpCompletionOption.ResponseHeadersRead,downloadToken);
                if((int)response.StatusCode is >=300 and <400){var location=response.Headers.Location;response.Dispose();response=null;if(location==null)throw new IOException("下載跳轉無效。");uri=new Uri(uri,location);continue;}break;
            }
            using(response){if(response==null)throw new IOException("下載跳轉次數過多。");response.EnsureSuccessStatusCode();await using var source=await response.Content.ReadAsStreamAsync(downloadToken);await using var target=new FileStream(file,FileMode.CreateNew,FileAccess.Write,FileShare.None,81920,true);using var hash=IncrementalHash.CreateHash(HashAlgorithmName.SHA256);byte[] buffer=new byte[81920];long total=0;long lastProgress=Environment.TickCount64;int count;while((count=await source.ReadAsync(buffer,downloadToken))>0){total+=count;if(total>item.Size)throw new IOException("下載檔案大於發佈記錄。");hash.AppendData(buffer,0,count);await target.WriteAsync(buffer.AsMemory(0,count),downloadToken);if(Environment.TickCount64-lastProgress>=200||total==item.Size){progress(total,item.Size);lastProgress=Environment.TickCount64;}}if(total!=item.Size||!Convert.ToHexString(hash.GetHashAndReset()).Equals(item.Digest,StringComparison.OrdinalIgnoreCase))throw new IOException("安裝包校驗失敗，已拒絕執行。");}
            return new(file,item.Digest);
        }catch{File.Delete(file);throw;}
    }
    public static async Task VerifyInstallerAsync(DownloadedInstaller installer,CancellationToken ct)
    {
        await using var file=File.OpenRead(installer.Path);var digest=await SHA256.HashDataAsync(file,ct);
        if(!Convert.ToHexString(digest).Equals(installer.Digest,StringComparison.OrdinalIgnoreCase))throw new IOException("安裝檔案已變化，請重新下載。");
    }
    public void Dispose()
    {
        lock(diagnosticGate)
        {
            if(disposed)return;
            disposed=true;diagnosticGeneration++;diagnosticReporting.Cancel();diagnosticReporting.Dispose();reporting.Cancel();reporting.Dispose();
        }
        http.Dispose();
    }
}
