using System.Text;
using System.Text.Json;

namespace SafeCode.Core;

public sealed class CodexUsageState
{
    public bool Initialized{get;set;}
    public Dictionary<string,UsagePart> Sessions{get;set;}=[];
    public UsagePart Counted{get;set;}=new();
    public Dictionary<string,CodexTrackedFile> Files{get;set;}=[];
}

public sealed class CodexTrackedFile
{
    public string SessionId{get;set;}="";
    public long Offset{get;set;}
    public long Length{get;set;}
}

/// <summary>
/// 只读本机 Codex 会话 JSONL 的 token_count 元数据，且只认 Safe Code 的 model_provider。
/// 不读取正文，不通过 SSH 扫描远端；由后台低频调用。
/// </summary>
public sealed class CodexUsageSource
{
    public const string Provider="safe_code_claude";
    private const int MaxTailBytes=256*1024;
    private readonly string sessionsRoot;
    private readonly DateTimeOffset startedAt;
    private readonly CodexUsageLedger ledger;
    private readonly Dictionary<string,CodexTrackedFile> files;
    private bool providerFilesSeen;
    private string? error;
    public CodexUsageSource(string sessionsRoot,DateTimeOffset startedAt,CodexUsageState? state=null)
    {
        this.sessionsRoot=sessionsRoot;this.startedAt=startedAt;
        state??=new CodexUsageState();
        ledger=new CodexUsageLedger(state.Sessions,state.Counted);
        files=state.Files;
        foreach(var (path,file) in files.ToArray())
            if(!File.Exists(path)||new FileInfo(path).Length<file.Offset)files.Remove(path);
        providerFilesSeen=files.Count>0;
    }
    public bool Available=>providerFilesSeen;
    public string? Error=>error;
    public IntegrationUsage Snapshot()=>providerFilesSeen
        ?ledger.Snapshot("本机 Codex 会话 token_count（仅 Safe Code 供应商）")
        :IntegrationUsage.Unavailable(error??"未发现 Safe Code 供应商的 Codex 会话用量");
    public CodexUsageState Export()=>new(){Initialized=true,Sessions=ledger.ExportSessions(),Counted=ledger.ExportCounted(),Files=files.ToDictionary(pair=>pair.Key,pair=>new CodexTrackedFile{SessionId=pair.Value.SessionId,Offset=pair.Value.Offset,Length=pair.Value.Length})};
    public async Task InitializeAsync(CancellationToken ct=default)
    {
        if(!Directory.Exists(sessionsRoot))return;
        foreach(string path in EnumerateCandidates(DateTimeOffset.MinValue))
        {
            ct.ThrowIfCancellationRequested();
            if(files.ContainsKey(path))continue;
            string? provider=null,sessionId=null;
            try{(provider,sessionId)=await ReadMetaAsync(path,ct);}catch(IOException){}catch(UnauthorizedAccessException){}
            if(provider!=Provider||sessionId is null)continue;
            providerFilesSeen=true;
            long length=new FileInfo(path).Length;
            bool createdDuringSession=File.GetCreationTimeUtc(path)>=startedAt.UtcDateTime;
            if(!createdDuringSession)
            {
                var (input,cached,output)=await ReadTailTotalsAsync(path,length,ct);
                if(input>0||output>0||cached>0)ledger.Baseline(sessionId,input,cached,output);
            }
            files[path]=new CodexTrackedFile{SessionId=sessionId,Offset=createdDuringSession?0:length,Length=length};
        }
    }
    public async Task SampleAsync(CancellationToken ct=default)
    {
        if(!Directory.Exists(sessionsRoot)){error="未找到本机 Codex 会话目录";return;}
        try
        {
            var since=startedAt.AddMinutes(-2);
            foreach(string path in EnumerateRecent(since))
            {
                ct.ThrowIfCancellationRequested();
                if(!files.TryGetValue(path,out var tracked))
                {
                    string? provider=null,sessionId=null;
                    try{(provider,sessionId)=await ReadMetaAsync(path,ct);}catch(IOException){continue;}catch(UnauthorizedAccessException){continue;}
                    if(provider!=Provider||sessionId is null)continue;
                    providerFilesSeen=true;
                    long length=new FileInfo(path).Length;
                    if(File.GetCreationTimeUtc(path)<startedAt.UtcDateTime)
                    {
                        var (input,cached,output)=await ReadTailTotalsAsync(path,length,ct);
                        if(input>0||output>0||cached>0)ledger.Baseline(sessionId,input,cached,output);
                        files[path]=new CodexTrackedFile{SessionId=sessionId,Offset=length,Length=length};
                        continue;
                    }
                    tracked=new CodexTrackedFile{SessionId=sessionId,Offset=0,Length=0};files[path]=tracked;
                }
                providerFilesSeen=true;
                long currentLength=new FileInfo(path).Length;
                if(currentLength<tracked.Offset){tracked.Offset=0;tracked.Length=currentLength;}
                if(currentLength==tracked.Offset)continue;
                await ReadNewLinesAsync(path,tracked,ct);
            }
        }
        catch(Exception exception)when(exception is IOException or UnauthorizedAccessException){error="读取本机 Codex 会话用量失败";}
    }
    private IEnumerable<string> EnumerateCandidates(DateTimeOffset since)
    {
        if(!Directory.Exists(sessionsRoot))yield break;
        foreach(string path in Directory.EnumerateFiles(sessionsRoot,"*.jsonl",SearchOption.AllDirectories))
        {
            if(File.GetLastWriteTimeUtc(path)<since.UtcDateTime)continue;
            yield return path;
        }
    }
    private IEnumerable<string> EnumerateRecent(DateTimeOffset since)
    {
        if(!Directory.Exists(sessionsRoot))yield break;
        // 只扫接入会话覆盖的日期目录，避免每次轮询遍历全部历史。
        for(var day=since.LocalDateTime.Date;day<=DateTime.Today;day=day.AddDays(1))
        {
            string directory=SessionDateFolder(sessionsRoot,day);
            if(!Directory.Exists(directory))continue;
            foreach(string path in Directory.EnumerateFiles(directory,"*.jsonl"))yield return path;
        }
    }
    // Codex stores session files in directories named for the local date.
    internal static string SessionDateFolder(string root,DateTime localDate)
        =>Path.Combine(root,localDate.ToString("yyyy"),localDate.ToString("MM"),localDate.ToString("dd"));
    private static async Task<(string? Provider,string? SessionId)> ReadMetaAsync(string path,CancellationToken ct)
    {
        await using var stream=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete);
        using var reader=new StreamReader(stream,Encoding.UTF8,false,8192);
        for(int i=0;i<4;i++)
        {
            string? line=await reader.ReadLineAsync(ct);if(line is null)break;
            try
            {
                using var document=JsonDocument.Parse(line);var root=document.RootElement;
                if(root.TryGetProperty("type",out var type)&&type.GetString()=="session_meta"&&root.TryGetProperty("payload",out var payload))
                {
                    string? provider=payload.TryGetProperty("model_provider",out var p)?p.GetString():null;
                    string? id=payload.TryGetProperty("id",out var value)?value.GetString():null;
                    return(provider,id);
                }
            }
            catch(JsonException){}
        }
        return(null,null);
    }
    private static async Task<(long Input,long Cached,long Output)> ReadTailTotalsAsync(string path,long length,CancellationToken ct)
    {
        if(length==0)return(0,0,0);
        int count=(int)Math.Min(MaxTailBytes,length);
        byte[] buffer=new byte[count];
        await using(var stream=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete))
        {
            stream.Seek(length-count,SeekOrigin.Begin);int read=0;while(read<count){int n=await stream.ReadAsync(buffer.AsMemory(read,count-read),ct);if(n==0)break;read+=n;}
        }
        string text=Encoding.UTF8.GetString(buffer,0,count);long input=0,cached=0,output=0;
        foreach(string line in text.Split('\n'))
        {
            if(TryParseTokenCount(line,out long i,out long c,out long o)){input=i;cached=c;output=o;}
        }
        return(input,cached,output);
    }
    private async Task ReadNewLinesAsync(string path,CodexTrackedFile tracked,CancellationToken ct)
    {
        long offset=tracked.Offset;byte[] bytes;
        await using(var stream=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete))
        {
            long length=stream.Length;long available=length-offset;if(available<=0)return;
            if(available>8*1024*1024){offset=length-8*1024*1024;available=length-offset;tracked.Offset=offset;}
            if(available>int.MaxValue)throw new IOException("会话文件增量过大。");
            bytes=new byte[(int)available];stream.Seek(offset,SeekOrigin.Begin);
            int read=0;while(read<bytes.Length){int n=await stream.ReadAsync(bytes.AsMemory(read,bytes.Length-read),ct);if(n==0)break;read+=n;}
            tracked.Length=length;if(read<bytes.Length)Array.Resize(ref bytes,read);
        }
        int end=-1;for(int i=bytes.Length-1;i>=0;i--)if(bytes[i]==(byte)'\n'){end=i;break;}
        if(end<0)return; // 半行留在原文件，持久 offset 仍停在完整行边界，重启后重读。
        tracked.Offset=offset+end+1;
        string complete=Encoding.UTF8.GetString(bytes,0,end+1);
        foreach(string line in complete.Split('\n'))
            if(TryParseTokenCount(line,out long input,out long cached,out long output))ledger.Observe(tracked.SessionId,input,cached,output);
    }
    private static bool TryReadLong(JsonElement element,string name,out long value)
    {
        value=0;
        if(!element.TryGetProperty(name,out var property))return true;
        if(property.ValueKind!=JsonValueKind.Number||!property.TryGetInt64(out value)){value=0;return false;}
        return true;
    }
    internal static bool TryParseTokenCount(string line,out long input,out long cached,out long output)
    {
        input=cached=output=0;
        if(!line.Contains("\"token_count\"",StringComparison.Ordinal))return false;
        try
        {
            using var document=JsonDocument.Parse(line);var root=document.RootElement;
            if(!root.TryGetProperty("type",out var type)||type.GetString()!="event_msg")return false;
            if(!root.TryGetProperty("payload",out var payload)||!payload.TryGetProperty("type",out var kind)||kind.GetString()!="token_count")return false;
            if(!payload.TryGetProperty("info",out var info)||!info.TryGetProperty("total_token_usage",out var total))return false;
            if(!TryReadLong(total,"input_tokens",out input)||!TryReadLong(total,"cached_input_tokens",out cached)||!TryReadLong(total,"output_tokens",out output))return false;
            return true;
        }
        catch(JsonException){return false;}
    }
}
