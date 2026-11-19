using System.Text.Json;

namespace SafeCode.Core;

/// <summary>本次接入会话的用量。来源必须是真实 usage；不可用时不要用 0 冒充。</summary>
public sealed record IntegrationUsage(bool Available,long Input,long Output,long CacheRead,long CacheWrite,long Total,string? Note=null)
{
    public static IntegrationUsage Unavailable(string note)=>new(false,0,0,0,0,0,note);
    // OpenAI/Codex 的 input 已包含 cached，总 token 只加 input+output。
    public static IntegrationUsage FromCodex(long input,long cachedInput,long output,string? note=null)
        =>new(true,input,output,Math.Max(0,cachedInput),0,Math.Max(0,input)+Math.Max(0,output),note);
    // Anthropic 的 input、cache read/write、output 分别计费，总 token 四者相加。
    public static IntegrationUsage FromAnthropic(long input,long output,long cacheRead,long cacheWrite,string? note=null)
        =>new(true,input,output,cacheRead,cacheWrite,Math.Max(0,input)+Math.Max(0,output)+Math.Max(0,cacheRead)+Math.Max(0,cacheWrite),note);
}

public sealed record IntegrationSession(DateTimeOffset StartedAt,DateTimeOffset? StoppedAt=null)
{
    public bool Active=>StoppedAt==null;
    public long ElapsedSeconds(DateTimeOffset now)
    {
        var end=StoppedAt??now;var seconds=(long)Math.Floor((end-StartedAt).TotalSeconds);
        return Math.Max(0,seconds);
    }
    public IntegrationSession Stop(DateTimeOffset now)=>this with{StoppedAt=now<StartedAt?StartedAt:now};
}

public sealed class UsagePart
{
    public long Input{get;set;}
    public long Output{get;set;}
    public long CacheRead{get;set;}
    public long CacheWrite{get;set;}
    public UsagePart Copy()=>new(){Input=Input,Output=Output,CacheRead=CacheRead,CacheWrite=CacheWrite};
    public bool SameAs(UsagePart other)=>Input==other.Input&&Output==other.Output&&CacheRead==other.CacheRead&&CacheWrite==other.CacheWrite;
}

/// <summary>Anthropic JSONL：同一 message.id 的流式快照只按各字段最大值计一次。</summary>
public sealed class AnthropicUsageLedger
{
    private readonly Dictionary<string,UsagePart> messages;
    private readonly UsagePart counted;
    public AnthropicUsageLedger() : this(new Dictionary<string,UsagePart>(),new UsagePart()){}
    public AnthropicUsageLedger(Dictionary<string,UsagePart> messages,UsagePart counted)
    {
        this.messages=messages;this.counted=counted;
    }
    public int CountedMessages=>messages.Count;
    public bool Observe(string? messageId,long input,long output,long cacheRead,long cacheWrite)
    {
        if(string.IsNullOrWhiteSpace(messageId))return false;
        long before=counted.Input+counted.Output+counted.CacheRead+counted.CacheWrite;
        if(!messages.TryGetValue(messageId,out var previous)){previous=new UsagePart();messages[messageId]=previous;}
        CountDelta(previous,counted,input,output,cacheRead,cacheWrite);
        return counted.Input+counted.Output+counted.CacheRead+counted.CacheWrite>before;
    }
    internal static void CountDelta(UsagePart previous,UsagePart counted,long input,long output,long cacheRead,long cacheWrite)
    {
        input=Math.Max(input,0);output=Math.Max(output,0);cacheRead=Math.Max(cacheRead,0);cacheWrite=Math.Max(cacheWrite,0);
        if(input>previous.Input){counted.Input+=input-previous.Input;previous.Input=input;}
        if(output>previous.Output){counted.Output+=output-previous.Output;previous.Output=output;}
        if(cacheRead>previous.CacheRead){counted.CacheRead+=cacheRead-previous.CacheRead;previous.CacheRead=cacheRead;}
        if(cacheWrite>previous.CacheWrite){counted.CacheWrite+=cacheWrite-previous.CacheWrite;previous.CacheWrite=cacheWrite;}
    }
    public IntegrationUsage Snapshot(string? note=null)=>IntegrationUsage.FromAnthropic(counted.Input,counted.Output,counted.CacheRead,counted.CacheWrite,note);
    public AnthropicUsageLedger Copy()=>new(messages.ToDictionary(pair=>pair.Key,pair=>pair.Value.Copy()),counted.Copy());
    public Dictionary<string,UsagePart> ExportMessages()=>messages.ToDictionary(pair=>pair.Key,pair=>pair.Value.Copy());
    public UsagePart ExportCounted()=>counted.Copy();
}

/// <summary>
/// Codex 会话 JSONL 的 token_count 是会话累计快照。按会话记录上次累计值，只累加增量；
/// cached 已含在 input 中，仅作展示，不计入 total。
/// </summary>
public sealed class CodexUsageLedger
{
    private readonly Dictionary<string,UsagePart> sessions;
    private readonly UsagePart counted;
    public CodexUsageLedger() : this(new Dictionary<string,UsagePart>(),new UsagePart()){}
    public CodexUsageLedger(Dictionary<string,UsagePart> sessions,UsagePart counted)
    {
        this.sessions=sessions;this.counted=counted;
    }
    public int ObservedSessions=>sessions.Count;
    public void Baseline(string sessionId,long input,long cachedInput,long output)
    {
        if(string.IsNullOrWhiteSpace(sessionId))return;
        sessions[sessionId]=new UsagePart{Input=Math.Max(0,input),Output=Math.Max(0,output),CacheRead=Math.Max(0,cachedInput)};
    }
    public void Observe(string sessionId,long input,long cachedInput,long output)
    {
        if(string.IsNullOrWhiteSpace(sessionId))return;
        if(!sessions.TryGetValue(sessionId,out var previous)){previous=new UsagePart();sessions[sessionId]=previous;}
        // 累计快照偶尔会乱序；只接受增长，避免把重复/回退快照再加一遍。
        input=Math.Max(input,0);output=Math.Max(output,0);cachedInput=Math.Max(cachedInput,0);
        if(input>previous.Input){counted.Input+=input-previous.Input;previous.Input=input;}
        if(output>previous.Output){counted.Output+=output-previous.Output;previous.Output=output;}
        if(cachedInput>previous.CacheRead){counted.CacheRead+=cachedInput-previous.CacheRead;previous.CacheRead=cachedInput;}
    }
    public IntegrationUsage Snapshot(string? note=null)=>IntegrationUsage.FromCodex(counted.Input,counted.CacheRead,counted.Output,note);
    public Dictionary<string,UsagePart> ExportSessions()=>sessions.ToDictionary(pair=>pair.Key,pair=>pair.Value.Copy());
    public UsagePart ExportCounted()=>counted.Copy();
}

public enum PolicyValueKind{String,DWord}
public sealed record PolicyValue(PolicyValueKind Kind,string? Text=null,int? Number=null)
{
    public static PolicyValue FromString(string value)=>new(PolicyValueKind.String,value);
    public static PolicyValue FromDWord(int value)=>new(PolicyValueKind.DWord,Number:value);
}

/// <summary>Claude Desktop 官方 3P 策略字段；停止时只回滚本工具写过且未被后续修改的字段。</summary>
public static class ClaudeDesktopPolicy
{
    public const string RegistryPath=@"SOFTWARE\Policies\Claude";
    public static readonly string[] ManagedFields=
    [
        "inferenceProvider","inferenceGatewayBaseUrl","inferenceGatewayAuthScheme","inferenceCredentialKind",
        "inferenceGatewayApiKey","inferenceModels","modelDiscoveryEnabled","defaultModelEffort","deploymentMode",
        "disableDeploymentModeChooser","alwaysStartWithDefaultModel","autoModeEnabled","autoModeClassifierModel","inferenceModelPricingEnabled",
        "chatTabEnabled","endUserAttribution","deploymentDisplayName"
    ];
    public static Dictionary<string,PolicyValue?> Restore(
        IReadOnlyDictionary<string,PolicyValue> current,
        IReadOnlyDictionary<string,PolicyValue> written,
        IReadOnlyDictionary<string,PolicyValue?> original)
    {
        var result=new Dictionary<string,PolicyValue?>();
        foreach(var field in ManagedFields)
        {
            if(!written.TryGetValue(field,out var ours))continue;
            if(!current.TryGetValue(field,out var now)||!Same(now,ours))continue; // 后续被用户/其他工具改过，保留。
            result[field]=original.TryGetValue(field,out var before)?before:null;
        }
        return result;
    }
    public static bool Same(PolicyValue? left,PolicyValue? right)
    {
        if(left is null||right is null)return left is null&&right is null;
        return left.Kind==right.Kind&&left.Text==right.Text&&left.Number==right.Number;
    }
    public static bool MatchesWritten(IReadOnlyDictionary<string,PolicyValue> current,IReadOnlyDictionary<string,PolicyValue> written)
    {
        foreach(var pair in written){if(!current.TryGetValue(pair.Key,out var now)||!Same(now,pair.Value))return false;}
        return true;
    }
    public static bool RouteWasReplaced(IReadOnlyDictionary<string,PolicyValue> current,IReadOnlyDictionary<string,PolicyValue> written)
    {
        if(!IsGateway(current)||!written.ContainsKey("inferenceGatewayBaseUrl")||!written.ContainsKey("inferenceGatewayApiKey"))return false;
        return new[]{"inferenceGatewayBaseUrl","inferenceGatewayApiKey"}.Any(field=>!current.TryGetValue(field,out var value)||!Same(value,written[field]));
    }
    public static string? Endpoint(IReadOnlyDictionary<string,PolicyValue> values)
        =>values.TryGetValue("inferenceGatewayBaseUrl",out var value)&&value.Kind==PolicyValueKind.String?value.Text:null;
    public static string? Display(IReadOnlyDictionary<string,PolicyValue> values)
    {
        if(!values.TryGetValue("inferenceModels",out var value)||value.Text is null)return null;
        try{using var document=JsonDocument.Parse(value.Text);foreach(var model in document.RootElement.EnumerateArray())if(model.TryGetProperty("labelOverride",out var label)&&label.ValueKind==JsonValueKind.String&&!string.IsNullOrWhiteSpace(label.GetString()))return label.GetString();}
        catch(JsonException){}
        return null;
    }
    public static string? Model(IReadOnlyDictionary<string,PolicyValue> values)
    {
        if(!values.TryGetValue("inferenceModels",out var value)||value.Text is null)return null;
        try{using var document=JsonDocument.Parse(value.Text);foreach(var model in document.RootElement.EnumerateArray())if(model.TryGetProperty("name",out var name)&&name.ValueKind==JsonValueKind.String&&!string.IsNullOrWhiteSpace(name.GetString()))return name.GetString();}
        catch(JsonException){}
        return null;
    }
    public static bool IsGateway(IReadOnlyDictionary<string,PolicyValue> values)
        =>values.TryGetValue("inferenceProvider",out var provider)&&provider.Text=="gateway"&&!string.IsNullOrWhiteSpace(Endpoint(values))&&!string.IsNullOrWhiteSpace(Model(values));
    public static string? NormalizeEndpoint(string? endpoint)
    {
        if(string.IsNullOrWhiteSpace(endpoint))return null;
        if(!Uri.TryCreate(endpoint.Trim(),UriKind.Absolute,out var uri))return null;
        if(uri.Scheme is not ("http" or "https")||string.IsNullOrWhiteSpace(uri.Host)||uri.UserInfo.Length>0)return null;
        string scheme=uri.Scheme.ToLowerInvariant();
        string host=uri.Host.ToLowerInvariant().TrimEnd('.');
        if(host.Contains(':'))host="["+host.Trim('[',']')+"]";
        int port=uri.IsDefaultPort?-1:uri.Port;
        string path=uri.AbsolutePath;
        if(path.Length>1)path=path.TrimEnd('/');
        if(path.Length==0)path="/";
        return scheme+"://"+host+(port>=0?":"+port.ToString():"")+path+uri.Query+uri.Fragment;
    }
    public static bool CanReuseCredential(string? existingEndpoint,string requestedEndpoint)
    {
        string? existing=NormalizeEndpoint(existingEndpoint),requested=NormalizeEndpoint(requestedEndpoint);
        return existing is not null&&requested is not null&&string.Equals(existing,requested,StringComparison.Ordinal);
    }
    public static bool IsTakeover(bool configured,bool sameEndpoint,bool sameModel,bool sameDisplay,string? apiKey)
        =>configured&&sameEndpoint&&sameModel&&sameDisplay&&string.IsNullOrEmpty(apiKey);
    public static string StopNote(bool takeover,bool originalGateway)
        =>takeover||originalGateway
            ?"已停止 Safe Code 的管理/统计并恢复原配置；原网关配置仍有效，Claude 仍可能继续使用它。"
            :"已停止 Safe Code 的管理/统计并移除本工具写入的网关字段；已运行的客户端可能直到重启才重新加载策略。";
}
