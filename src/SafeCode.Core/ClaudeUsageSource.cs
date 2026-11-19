using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace SafeCode.Core;

public sealed class ClaudeUsageState
{
    public bool Initialized{get;set;}
    public bool SourceAvailable{get;set;}
    public bool HasUsage{get;set;}
    public Dictionary<string,UsagePart> Messages{get;set;}=[];
    public UsagePart Counted{get;set;}=new();
    public Dictionary<string,ClaudeTrackedFile> Files{get;set;}=[];
    public List<string> CliSessionIds{get;set;}=[];
    public Dictionary<string,string> CliPaths{get;set;}=[];
}

public sealed class ClaudeTrackedFile
{
    public long Offset{get;set;}
    public long Length{get;set;}
}

/// <summary>
/// 只读 Claude Desktop 官方本地会话 JSONL 的 usage 元数据。范围：
/// 3P 本地会话（local-agent-mode-sessions）和 Claude Desktop 代码页关联的 CLI transcript。
/// 不读取正文入库，不估算，不从服务端汇总其他客户端流量。
/// </summary>
public sealed partial class ClaudeUsageSource
{
    private const int MaximumMessages=4000;
    private static readonly Regex CliSessionPattern=new(@"""cliSessionId""\s*:\s*""([0-9a-fA-F-]{36})""",RegexOptions.Compiled|RegexOptions.CultureInvariant);
    private readonly string localRoot;
    private readonly string projectsRoot;
    private readonly DateTimeOffset startedAt;
    private readonly AnthropicUsageLedger ledger;
    private readonly Dictionary<string,ClaudeTrackedFile> files;
    private readonly HashSet<string> cliSessionIds;
    private readonly Dictionary<string,string> cliPaths;
    private readonly Dictionary<string,DateTime> metadataSeen=new(StringComparer.Ordinal);
    private readonly Dictionary<string,DateTime> pathMissAt=new(StringComparer.Ordinal);
    private bool sourceAvailable,hasUsage;
    private string? error;
    public ClaudeUsageSource(string localRoot,string projectsRoot,DateTimeOffset startedAt,ClaudeUsageState? state=null)
    {
        this.localRoot=localRoot;this.projectsRoot=projectsRoot;this.startedAt=startedAt;
        state??=new ClaudeUsageState();
        ledger=new AnthropicUsageLedger(state.Messages,state.Counted);
        files=state.Files;cliSessionIds=new HashSet<string>(state.CliSessionIds,StringComparer.OrdinalIgnoreCase);
        cliPaths=new Dictionary<string,string>(state.CliPaths,StringComparer.OrdinalIgnoreCase);
        sourceAvailable=state.SourceAvailable;hasUsage=state.HasUsage;
        foreach(var (path,file) in files.ToArray())if(!File.Exists(path)||new FileInfo(path).Length<file.Offset)files.Remove(path);
    }
    public bool Available=>sourceAvailable&&hasUsage;
    public string? Error=>error;
    public IntegrationUsage Snapshot()=>Available
        ?ledger.Snapshot("Claude Desktop 官方本地会话 JSONL（只读 token 元数据）")
        :IntegrationUsage.Unavailable(error??(sourceAvailable?"暂未读到新的官方会话用量；主聊天不写本地 token 元数据时无法统计":"未发现可读的 Claude Desktop 官方会话用量"));
    public ClaudeUsageState Export()
    {
        var recent=ledger.ExportMessages();
        if(recent.Count>MaximumMessages)
        {
            var keep=recent.OrderByDescending(pair=>pair.Value.Input+pair.Value.Output+pair.Value.CacheRead+pair.Value.CacheWrite).Take(MaximumMessages).ToDictionary(pair=>pair.Key,pair=>pair.Value);
            recent=keep;
        }
        return new(){Initialized=true,SourceAvailable=sourceAvailable,HasUsage=hasUsage,Messages=recent,Counted=ledger.ExportCounted(),Files=files.ToDictionary(pair=>pair.Key,pair=>new ClaudeTrackedFile{Offset=pair.Value.Offset,Length=pair.Value.Length}),CliSessionIds=[..cliSessionIds],CliPaths=new Dictionary<string,string>(cliPaths,StringComparer.OrdinalIgnoreCase)};
    }
    public async Task InitializeAsync(CancellationToken ct=default)
    {
        DiscoverCliMetadata();
        foreach(string path in EnumerateLocalJsonl())
        {
            ct.ThrowIfCancellationRequested();sourceAvailable=true;TrackExisting(path);
        }
        foreach(string id in cliSessionIds)
        {
            string? path=ResolveCliTranscript(id);if(path is null)continue;
            sourceAvailable=true;TrackExisting(path);
        }
    }
    public async Task SampleAsync(CancellationToken ct=default)
    {
        try
        {
            DiscoverCliMetadata();
            foreach(string id in cliSessionIds)
            {
                string? path=ResolveCliTranscript(id);if(path is null)continue;
                sourceAvailable=true;TrackExisting(path);
            }
            foreach(string path in EnumerateLocalJsonl())
            {
                ct.ThrowIfCancellationRequested();sourceAvailable=true;TrackExisting(path);
            }
            foreach(var (path,tracked) in files)
            {
                ct.ThrowIfCancellationRequested();
                if(!File.Exists(path))continue;
                long currentLength=new FileInfo(path).Length;
                if(currentLength<tracked.Offset){tracked.Offset=0;tracked.Length=currentLength;}
                if(currentLength==tracked.Offset)continue;
                await ReadNewLinesAsync(path,tracked,ct);
            }
        }
        catch(Exception exception)when(exception is IOException or UnauthorizedAccessException){error="读取 Claude Desktop 本地会话用量失败";}
    }
    private void TrackExisting(string path)
    {
        if(files.ContainsKey(path))return;
        long length=new FileInfo(path).Length;
        bool createdDuringSession=File.GetCreationTimeUtc(path)>=startedAt.UtcDateTime;
        files[path]=new ClaudeTrackedFile{Offset=createdDuringSession?0:length,Length=length};
    }
    private void DiscoverCliMetadata()
    {
        foreach(string metadata in EnumerateCliMetadata())
        {
            DateTime mtime;
            try{mtime=File.GetLastWriteTimeUtc(metadata);}catch(IOException){continue;}catch(UnauthorizedAccessException){continue;}
            if(metadataSeen.TryGetValue(metadata,out var seen)&&seen==mtime)continue;
            metadataSeen[metadata]=mtime;ReadCliSessionId(metadata);
        }
    }
    private string? ResolveCliTranscript(string id)
    {
        if(cliPaths.TryGetValue(id,out var cached)&&File.Exists(cached))return cached;
        cliPaths.Remove(id);
        if(pathMissAt.TryGetValue(id,out var missed)&&DateTime.UtcNow-missed<TimeSpan.FromMinutes(1))return null;
        string? found=FindCliTranscript(id);
        if(found is null){pathMissAt[id]=DateTime.UtcNow;return null;}
        cliPaths[id]=found;pathMissAt.Remove(id);return found;
    }
    private IEnumerable<string> EnumerateLocalJsonl()
    {
        string root=Path.Combine(localRoot,"local-agent-mode-sessions");
        if(!Directory.Exists(root))yield break;
        foreach(string path in Directory.EnumerateFiles(root,"*.jsonl",SearchOption.AllDirectories))yield return path;
    }
    private IEnumerable<string> EnumerateCliMetadata()
    {
        string root=Path.Combine(localRoot,"claude-code-sessions");
        if(!Directory.Exists(root))yield break;
        foreach(string path in Directory.EnumerateFiles(root,"local_*.json",SearchOption.AllDirectories))yield return path;
    }
    private void ReadCliSessionId(string metadata)
    {
        try
        {
            string text=File.ReadAllText(metadata);var match=CliSessionPattern.Match(text);
            if(match.Success)cliSessionIds.Add(match.Groups[1].Value);
        }
        catch(IOException){}catch(UnauthorizedAccessException){}
    }
    private string? FindCliTranscript(string id)
    {
        if(!Directory.Exists(projectsRoot))return null;
        foreach(string path in Directory.EnumerateFiles(projectsRoot,id+".jsonl",SearchOption.AllDirectories))return path;
        return null;
    }
    private async Task ReadNewLinesAsync(string path,ClaudeTrackedFile tracked,CancellationToken ct)
    {
        long offset=tracked.Offset;byte[] bytes;
        await using(var stream=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete))
        {
            long length=stream.Length;long available=length-offset;if(available<=0)return;
            if(available>16*1024*1024){offset=length-16*1024*1024;available=length-offset;tracked.Offset=offset;}
            if(available>int.MaxValue)throw new IOException("会话文件增量过大。");
            bytes=new byte[(int)available];stream.Seek(offset,SeekOrigin.Begin);
            int read=0;while(read<bytes.Length){int n=await stream.ReadAsync(bytes.AsMemory(read,bytes.Length-read),ct);if(n==0)break;read+=n;}
            tracked.Length=length;if(read<bytes.Length)Array.Resize(ref bytes,read);
        }
        int end=-1;for(int i=bytes.Length-1;i>=0;i--)if(bytes[i]==(byte)'\n'){end=i;break;}
        if(end<0)return; // 半行留在原文件，持久 offset 仍停在完整行边界，重启后重读。
        tracked.Offset=offset+end+1;
        string complete=Encoding.UTF8.GetString(bytes,0,end+1);
        foreach(string line in complete.Split('\n'))ObserveLine(line);
    }
    private void ObserveLine(string line)
    {
        if(!line.Contains("\"usage\"",StringComparison.Ordinal))return;
        try
        {
            using var document=JsonDocument.Parse(line);var root=document.RootElement;
            if(!root.TryGetProperty("type",out var type)||type.GetString()!="assistant")return;
            if(!root.TryGetProperty("message",out var message))return;
            if(!message.TryGetProperty("usage",out var usage)||usage.ValueKind!=JsonValueKind.Object)return;
            string? id=message.TryGetProperty("id",out var messageId)?messageId.GetString():null;
            if(string.IsNullOrWhiteSpace(id))return;
            if(!TryReadLong(usage,"input_tokens",out long input)||!TryReadLong(usage,"output_tokens",out long output)||!TryReadLong(usage,"cache_read_input_tokens",out long cacheRead)||!TryReadLong(usage,"cache_creation_input_tokens",out long cacheWrite))return;
            if(input<0||output<0||cacheRead<0||cacheWrite<0)return;
            if(ledger.Observe(id,input,output,cacheRead,cacheWrite))hasUsage=true;
        }
        catch(JsonException){}
    }
    private static bool TryReadLong(JsonElement element,string name,out long result)
    {
        result=0;
        if(!element.TryGetProperty(name,out var value))return true;
        return value.ValueKind==JsonValueKind.Number&&value.TryGetInt64(out result);
    }
}
