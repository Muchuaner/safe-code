using System.IO;
using System.Diagnostics;
using System.Net;
using System.Net.Http;
using System.Net.Http.Json;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
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
    private const int ExplicitOptInVersion=1;
    private const int CurrentDiagnosticScope=1;
    private readonly string preferencesPath;
    private readonly string directory;
    private readonly string updateAssetSuffix;
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
    private (string Version,string Url,string Digest,long Size)? release;
    private DateTimeOffset lastCheck;
    private UpdateInfo? lastUpdate;
    public static string Version=>Assembly.GetExecutingAssembly().GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion.Split('+')[0]??Assembly.GetExecutingAssembly().GetName().Version?.ToString(3)??"0.4.3";
    public ClientServices(string directory,HttpMessageHandler? transport=null,HttpMessageHandler? downloadTransport=null,string? updateAssetSuffix=null,bool endpointEnabled=true)
    {
        http=transport==null?new HttpClient(new HttpClientHandler{AllowAutoRedirect=false}):new HttpClient(transport);http.MaxResponseContentBufferSize=1048576;http.Timeout=TimeSpan.FromSeconds(15);this.downloadTransport=downloadTransport;this.updateAssetSuffix=updateAssetSuffix??AssetSuffix;
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
        http.DefaultRequestHeaders.UserAgent.ParseAdd("SafeCode/"+Version);
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
            var body=new{schema=2,eventId=trace.Id,version=Version,context,report};
            await diagnosticSlots.WaitAsync(deadline.Token);acquired=true;
            for(int attempt=0;attempt<2;attempt++)
            {
                if(!DiagnosticsAllowed||trace.Generation!=diagnosticGeneration||deadline.IsCancellationRequested)return;
                try{using var response=await http.PostAsJsonAsync(new Uri(endpoint!,"v1/diagnostics"),body,JsonDefaults.Options,deadline.Token);if(response.IsSuccessStatusCode||(int)response.StatusCode<500)return;}
                catch(HttpRequestException){}
                if(attempt==0)await Task.Delay(1000,deadline.Token);
            }
        }
        catch(Exception){} // 诊断发送永远不能改变部署结果或弹出第二个错误。
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
        // 固定字段白名单：无 VPS 地址、登录名、机器名、密码或项目路径。
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
        if(endpoint==null)throw new InvalidOperationException("反馈服务尚未启用，请暂时通过 QQ 群反馈。");
        message=message.Trim();if(message.Length<5||message.Length>4000)throw new ArgumentException("反馈内容须为 5–4000 字。");
        FeedbackContext? context;
        lock(diagnosticGate)context=RecentFailure;
        using var response=await http.PostAsJsonAsync(new Uri(endpoint,"v1/feedback"),new{schema=2,eventId=Guid.NewGuid().ToString(),version=Version,message,context},JsonDefaults.Options,ct);
        if(!response.IsSuccessStatusCode)throw new IOException(response.StatusCode==HttpStatusCode.TooManyRequests?"提交较频繁，请稍后重试。":"反馈暂未送达，请稍后重试。");
    }
    public async Task<UpdateInfo> CheckUpdateAsync(bool force,CancellationToken ct)
    {
        if(lastUpdate!=null&&DateTimeOffset.UtcNow-lastCheck<TimeSpan.FromSeconds(force?30:43200))return lastUpdate;
        lastCheck=DateTimeOffset.UtcNow;
        bool betaChannel=ReleaseNumber.Parse(Version)?.Beta==true;
        using var request=new HttpRequestMessage(HttpMethod.Get,"https://api.github.com/repos/"+Repository+(betaChannel?"/releases?per_page=100":"/releases/latest"));request.Headers.Accept.ParseAdd("application/vnd.github+json");
        using var response=await http.SendAsync(request,ct);
        if(response.StatusCode==HttpStatusCode.NotFound)return lastUpdate=new(Version,null,"尚未发布正式版本",null);
        if(response.StatusCode is HttpStatusCode.Forbidden or HttpStatusCode.TooManyRequests)throw new IOException("GitHub 暂时限制了查询频率，请稍后重试。");
        response.EnsureSuccessStatusCode();using var doc=JsonDocument.Parse(await response.Content.ReadAsStringAsync(ct));var root=doc.RootElement;
        if(betaChannel){JsonElement? best=null;ReleaseNumber? bestNumber=null;foreach(var candidate in root.EnumerateArray()){if(candidate.GetProperty("draft").GetBoolean())continue;var number=ReleaseNumber.Parse(candidate.GetProperty("tag_name").GetString()??"");if(number==null||(candidate.GetProperty("prerelease").GetBoolean()&&!number.Beta))continue;if(bestNumber==null||number.CompareTo(bestNumber)>0){best=candidate;bestNumber=number;}}if(best==null)return lastUpdate=new(Version,null,"尚未发布测试版本",null);root=best.Value;}
        var update=ParseRelease(root,Version,updateAssetSuffix);release=null;
        if(update.CanInstall){string name="SafeCode-"+update.Version+"-"+updateAssetSuffix;var asset=root.GetProperty("assets").EnumerateArray().First(x=>x.GetProperty("name").GetString()==name);release=(update.Version!,asset.GetProperty("browser_download_url").GetString()!,asset.GetProperty("digest").GetString()![7..],asset.GetProperty("size").GetInt64());}
        return lastUpdate=update;
    }
    private static string AssetSuffix=>OperatingSystem.IsMacOS()?(System.Runtime.InteropServices.RuntimeInformation.ProcessArchitecture==System.Runtime.InteropServices.Architecture.Arm64?"macOS-arm64.dmg":"macOS-x64.dmg"):"Setup-x64.exe";
    internal static UpdateInfo ParseRelease(JsonElement root,string current,string assetSuffix="Setup-x64.exe")
    {
        string tag=root.GetProperty("tag_name").GetString()??"";string version=tag.StartsWith('v')?tag[1..]:tag;
        var latest=ReleaseNumber.Parse(version);var installed=ReleaseNumber.Parse(current);
        if(latest==null||installed==null||root.GetProperty("draft").GetBoolean()||(root.GetProperty("prerelease").GetBoolean()&&(!installed.Beta||!latest.Beta)))throw new IOException("GitHub 返回的版本信息无效。");
        string page="https://github.com/"+Repository+"/releases/tag/"+Uri.EscapeDataString(tag);
        if(latest.CompareTo(installed)<=0)return new(current,null,"已是最新版本",page);
        string name="SafeCode-"+version+"-"+assetSuffix;bool valid=false;
        foreach(var asset in root.GetProperty("assets").EnumerateArray()){
            if(asset.GetProperty("name").GetString()!=name)continue;
            string digest=asset.TryGetProperty("digest",out var d)?d.GetString()??"":"";string url=asset.GetProperty("browser_download_url").GetString()??"";
            valid=System.Text.RegularExpressions.Regex.IsMatch(digest,"^sha256:[a-fA-F0-9]{64}$")&&url=="https://github.com/"+Repository+"/releases/download/"+tag+"/"+name&&asset.GetProperty("size").GetInt64() is >0 and <=1073741824;
        }
        return new(current,version,"发现新版本 "+version,page,valid);
    }
    public async Task<DownloadedInstaller> DownloadUpdateAsync(Action<long,long> progress,CancellationToken ct)
    {
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromMinutes(20));var downloadToken=deadline.Token;
        var item=release??throw new InvalidOperationException("请先检查新版本；没有可验证的安装包时可打开 GitHub 下载页。");
        string folder=Path.Combine(directory,"Updates",Guid.NewGuid().ToString("N"));Directory.CreateDirectory(folder);
        string file=Path.Combine(folder,"SafeCode-"+item.Version+"-"+updateAssetSuffix);
        using var download=downloadTransport==null?new HttpClient(new HttpClientHandler{AllowAutoRedirect=false}):new HttpClient(downloadTransport,false);download.Timeout=TimeSpan.FromMinutes(20);download.DefaultRequestHeaders.UserAgent.ParseAdd("SafeCode/"+Version);
        Uri uri=new(item.Url);
        try{
            HttpResponseMessage? response=null;
            for(int i=0;i<6;i++){
                if(uri.Scheme!="https"||uri.UserInfo!=""||!(uri.Host=="github.com"||uri.Host=="release-assets.githubusercontent.com"||uri.Host=="objects.githubusercontent.com"))throw new IOException("安装包跳转到了未允许的下载地址。");
                response=await download.GetAsync(uri,HttpCompletionOption.ResponseHeadersRead,downloadToken);
                if((int)response.StatusCode is >=300 and <400){var location=response.Headers.Location;response.Dispose();response=null;if(location==null)throw new IOException("下载跳转无效。");uri=new Uri(uri,location);continue;}break;
            }
            using(response){if(response==null)throw new IOException("下载跳转次数过多。");response.EnsureSuccessStatusCode();await using var source=await response.Content.ReadAsStreamAsync(downloadToken);await using var target=new FileStream(file,FileMode.CreateNew,FileAccess.Write,FileShare.None,81920,true);using var hash=IncrementalHash.CreateHash(HashAlgorithmName.SHA256);byte[] buffer=new byte[81920];long total=0;long lastProgress=Environment.TickCount64;int count;while((count=await source.ReadAsync(buffer,downloadToken))>0){total+=count;if(total>item.Size)throw new IOException("下载文件大于发布记录。");hash.AppendData(buffer,0,count);await target.WriteAsync(buffer.AsMemory(0,count),downloadToken);if(Environment.TickCount64-lastProgress>=200||total==item.Size){progress(total,item.Size);lastProgress=Environment.TickCount64;}}if(total!=item.Size||!Convert.ToHexString(hash.GetHashAndReset()).Equals(item.Digest,StringComparison.OrdinalIgnoreCase))throw new IOException("安装包校验失败，已拒绝运行。");}
            return new(file,item.Digest);
        }catch{File.Delete(file);throw;}
    }
    public static async Task VerifyInstallerAsync(DownloadedInstaller installer,CancellationToken ct)
    {
        await using var file=File.OpenRead(installer.Path);var digest=await SHA256.HashDataAsync(file,ct);
        if(!Convert.ToHexString(digest).Equals(installer.Digest,StringComparison.OrdinalIgnoreCase))throw new IOException("安装文件已变化，请重新下载。");
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
