using System.IO;
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
    public bool Enabled {get;set;}=true;
    public bool Confirmed {get;set;}
    public string InstallationId {get;set;}="";
}
internal sealed record DownloadedInstaller(string Path,string Digest);
internal sealed record UpdateInfo(string CurrentVersion,string? Version,string Status,string? Page,bool CanInstall=false);
internal sealed class ClientServices : IDisposable
{
    private const string Repository="Muchuaner/safe-code";
    private readonly string preferencesPath;
    private readonly string directory;
    private readonly Uri? endpoint;
    private readonly HttpClient http;
    private readonly HttpMessageHandler? downloadTransport;
    private UsagePreferences preferences;
    private CancellationTokenSource reporting=new();
    private bool counted;
    private readonly string eventId=Guid.NewGuid().ToString();
    private (string Version,string Url,string Digest,long Size)? release;
    private DateTimeOffset lastCheck;
    private UpdateInfo? lastUpdate;
    public static string Version=>Assembly.GetExecutingAssembly().GetName().Version?.ToString(3)??"0.4.0";
    public ClientServices(string directory,HttpMessageHandler? transport=null,HttpMessageHandler? downloadTransport=null)
    {
        http=transport==null?new HttpClient(new HttpClientHandler{AllowAutoRedirect=false}):new HttpClient(transport);http.MaxResponseContentBufferSize=1048576;http.Timeout=TimeSpan.FromSeconds(15);this.downloadTransport=downloadTransport;
        this.directory=directory;Directory.CreateDirectory(directory);preferencesPath=Path.Combine(directory,"usage-preferences.json");
        try{preferences=JsonSerializer.Deserialize<UsagePreferences>(File.ReadAllText(preferencesPath),JsonDefaults.Options)??new();}catch(IOException){preferences=new();}catch(JsonException){preferences=new();}
        http.DefaultRequestHeaders.UserAgent.ParseAdd("SafeCode/"+Version);
        string config=Path.Combine(AppContext.BaseDirectory,"service-endpoints.json");
        try{if(File.Exists(config)){using var doc=JsonDocument.Parse(File.ReadAllText(config));if(doc.RootElement.TryGetProperty("feedbackBaseUrl",out var value)&&Uri.TryCreate(value.GetString(),UriKind.Absolute,out var uri)&&uri.Scheme=="https"&&!uri.IsLoopback&&string.IsNullOrEmpty(uri.UserInfo)&&uri.AbsolutePath=="/"&&string.IsNullOrEmpty(uri.Query)&&string.IsNullOrEmpty(uri.Fragment))endpoint=uri;}}catch(IOException){}catch(JsonException){}catch(InvalidOperationException){}
    }
    public bool NoticeWasConfirmed=>preferences.Confirmed;
    public object Preferences()=>new{enabled=preferences.Enabled,confirmed=preferences.Confirmed,serviceAvailable=endpoint!=null,recipient=endpoint?.Host};
    private void Save(){string temp=preferencesPath+".new";File.WriteAllText(temp,JsonSerializer.Serialize(preferences,JsonDefaults.Options));File.Move(temp,preferencesPath,true);}
    public async Task<object> SetConsentAsync(bool enabled,bool confirmed)
    {
        preferences.Enabled=enabled;preferences.Confirmed|=confirmed;
        if(!enabled){reporting.Cancel();reporting.Dispose();reporting=new();preferences.InstallationId="";}
        Save();
        if(enabled&&preferences.Confirmed)await CountLaunchAsync();
        return Preferences();
    }
    private async Task CountLaunchAsync()
    {
        if(counted||endpoint==null||!preferences.Enabled||!preferences.Confirmed)return;
        if(!Guid.TryParse(preferences.InstallationId,out _)){preferences.InstallationId=Guid.NewGuid().ToString();Save();}
        counted=true;var token=reporting.Token;
        // 固定字段白名单：无 VPS 地址、登录名、机器名、密码或项目路径。
        var body=new{schema=1,eventId,installationId=preferences.InstallationId,version=Version,os="Windows",eventType="launch"};
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
        using var response=await http.PostAsJsonAsync(new Uri(endpoint,"v1/feedback"),new{schema=1,eventId=Guid.NewGuid().ToString(),version=Version,message},JsonDefaults.Options,ct);
        if(!response.IsSuccessStatusCode)throw new IOException(response.StatusCode==HttpStatusCode.TooManyRequests?"提交较频繁，请稍后重试。":"反馈暂未送达，请稍后重试。");
    }
    public async Task<UpdateInfo> CheckUpdateAsync(bool force,CancellationToken ct)
    {
        if(lastUpdate!=null&&DateTimeOffset.UtcNow-lastCheck<TimeSpan.FromSeconds(force?30:43200))return lastUpdate;
        lastCheck=DateTimeOffset.UtcNow;
        using var request=new HttpRequestMessage(HttpMethod.Get,"https://api.github.com/repos/"+Repository+"/releases/latest");request.Headers.Accept.ParseAdd("application/vnd.github+json");
        using var response=await http.SendAsync(request,ct);
        if(response.StatusCode==HttpStatusCode.NotFound)return lastUpdate=new(Version,null,"尚未发布正式版本",null);
        if(response.StatusCode is HttpStatusCode.Forbidden or HttpStatusCode.TooManyRequests)throw new IOException("GitHub 暂时限制了查询频率，请稍后重试。");
        response.EnsureSuccessStatusCode();using var doc=JsonDocument.Parse(await response.Content.ReadAsStringAsync(ct));var root=doc.RootElement;
        var update=ParseRelease(root,Version);release=null;
        if(update.CanInstall){string name="SafeCode-"+update.Version+"-Setup-x64.exe";var asset=root.GetProperty("assets").EnumerateArray().First(x=>x.GetProperty("name").GetString()==name);release=(update.Version!,asset.GetProperty("browser_download_url").GetString()!,asset.GetProperty("digest").GetString()![7..],asset.GetProperty("size").GetInt64());}
        return lastUpdate=update;
    }
    internal static UpdateInfo ParseRelease(JsonElement root,string current)
    {
        string tag=root.GetProperty("tag_name").GetString()??"";string version=tag.TrimStart('v');
        if(root.GetProperty("draft").GetBoolean()||root.GetProperty("prerelease").GetBoolean()||!System.Version.TryParse(version,out var latest)||!System.Version.TryParse(current,out var installed))throw new IOException("GitHub 返回的版本信息无效。");
        string page="https://github.com/"+Repository+"/releases/tag/"+Uri.EscapeDataString(tag);
        if(latest<=installed)return new(current,null,"已是最新版本",page);
        string name="SafeCode-"+version+"-Setup-x64.exe";bool valid=false;
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
        string file=Path.Combine(folder,"SafeCode-"+item.Version+"-Setup-x64.exe");
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
    public void Dispose(){reporting.Cancel();reporting.Dispose();http.Dispose();}
}
