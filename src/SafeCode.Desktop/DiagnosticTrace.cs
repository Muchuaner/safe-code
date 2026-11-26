using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;
using SafeCode.Core;
using Renci.SshNet.Common;
using System.Net.Sockets;

namespace SafeCode.Desktop;

// 診斷只輸出固定詞表，原始日誌、異常文本、堆棧和請求參數從不進入報告。
internal sealed record DiagnosticSignal(int Seconds,string Code);
internal sealed record DiagnosticReport(string Operation,string Outcome,string ErrorKind,string FailureSite,int DurationSeconds,DiagnosticSignal[] Signals,int OmittedLines,string Stage,
    [property:JsonIgnore(Condition=JsonIgnoreCondition.WhenWritingNull)] IReadOnlyDictionary<string,object>? Gateway=null);
internal sealed class DiagnosticTrace(string operation,int generation,bool background=false)
{
    private static readonly string[] DeploymentOperations=["account","localization","desktop","chrome","input","claude","ssh","cloud","mount_install"];
    internal static readonly string[] Operations=[
        ..DeploymentOperations,"mount_apply","connection","ip_policy","directory_list",
        "account_customization","locale_customization","deployment_settings","desktop_launch","desktop_display",
        "directory_create","mount_reconnect","cloud_launch","cloud_public","resource_stats","environment_assessment",
        "codex_bridge_start","codex_bridge_stop","codex_bridge_restore","codex_launch",
        "claude_desktop_start","claude_desktop_stop","claude_desktop_restore","claude_desktop_launch",
        "desktop_key_enable","desktop_key_disable","desktop_key_restore","desktop_key_request",
        "gateway_enable","gateway_status","gateway_reconnect","gateway_open","gateway_key_list","gateway_usage",
        "gateway_key_issue","gateway_key_update","gateway_key_revoke","gateway_settings","gateway_port","gateway_origin",
        "login_restore"];
    // 階段只取流程里程碑；dns_failure/package_lock_wait 等是錯誤信號，不能冒充階段。
    // 同一操作內後續子請求會再次輸出 request_uploaded，不能讓它把階段倒回早期。
    private static readonly Dictionary<string,int> PhaseRanks=new(){["ssh_authenticated"]=1,["agent_ready"]=1,["request_uploaded"]=2,["job_started"]=3,["remote_started"]=4,["packages_present"]=4,["package_unpacking"]=5,["package_configuring"]=6,["claude_download_started"]=7,["claude_download_verified"]=8};
    private readonly Stopwatch clock=Stopwatch.StartNew();
    private readonly List<DiagnosticSignal> signals=[];
    private readonly object gate=new();
    private int omitted;
    private string stage="started";
    private int phaseRank;
    public string Operation=>operation;
    public string Id {get;}=Guid.NewGuid().ToString();
    public int Generation=>generation;
    public bool Background=>background;
    public IReadOnlyDictionary<string,object>? Gateway {get;set;}
    public static bool IsBackgroundOperation(string operation,JsonElement args=default)=>
        operation is "deployment_settings" or "resource_stats" or "environment_assessment" or "gateway_reconnect" ||
        operation is ("gateway_key_list" or "gateway_usage") && ReadFlag(args,"background");
    private static bool ReadFlag(JsonElement args,string property)=>
        args.ValueKind==JsonValueKind.Object&&args.TryGetProperty(property,out var value)&&value.ValueKind==JsonValueKind.True;
    private static string? ReadString(JsonElement args,string property)=>
        args.ValueKind==JsonValueKind.Object&&args.TryGetProperty(property,out var value)&&value.ValueKind==JsonValueKind.String?value.GetString():null;
    public static string? GatewayOperation(JsonElement args)=>ReadString(args,"action") switch
    {
        "list"=>"gateway_key_list", "usage"=>"gateway_usage", "issue"=>"gateway_key_issue", "update"=>"gateway_key_update",
        "revoke"=>"gateway_key_revoke", "settings"=>"gateway_settings", "set_port"=>"gateway_port", "set_origin"=>"gateway_origin",
        _=>null
    };
    public static string? OperationFor(string rpc,JsonElement args)
    {
        if(rpc is "deploy" or "job.resume")
        {
            var operation=ReadString(args,rpc=="deploy"?"step":"operation");
            return DeploymentOperations.Contains(operation)?operation:null;
        }
        return rpc switch
        {
            "connect" when ReadFlag(args,"automatic")=>"gateway_reconnect",
            "connect" or "connection.reauthenticate"=>"connection", "ip.apply"=>"ip_policy",
            "folder.list"=>"directory_list", "folder.create"=>"directory_create",
            "cloud.install"=>"cloud", "cloud.open"=>"cloud_launch", "cloud.expose"=>"cloud_public",
            "mount.install"=>"mount_install", "mount.apply"=>"mount_apply", "mount.reconnect"=>"mount_reconnect",
            "account.draft.save"=>"account_customization", "deployment.locale.save"=>"locale_customization", "deployment.settings.get"=>"deployment_settings",
            "desktop"=>"desktop_launch", "desktop.resolution.save"=>"desktop_display",
            "resources"=>"resource_stats", "environment.assess"=>"environment_assessment",
            "bridge.start"=>"codex_bridge_start", "bridge.stop"=>"codex_bridge_stop", "codex.open"=>"codex_launch",
            "claudeDesktop.open"=>"claude_desktop_launch",
            "desktopKey.enable"=>"desktop_key_enable", "desktopKey.disable"=>"desktop_key_disable",
            "safeCodeApi.enablePublic"=>"gateway_enable", "safeCodeApi.context" or "window.gateway.open"=>"gateway_open",
            "safeCodeApi.manage"=>GatewayOperation(args), "login.restoreSaved"=>"login_restore",
            _=>null
        };
    }
    private int Seconds=>(int)Math.Clamp(clock.Elapsed.TotalSeconds,0,86400);
    public void Observe(string line)
    {
        string? code=ClassifyLine(line);
        lock(gate)
        {
            if(code==null){omitted=Math.Min(omitted+1,1000000);return;}
            if(PhaseRanks.TryGetValue(code,out int rank)&&!(code=="request_uploaded"&&phaseRank>=3)&&rank>=phaseRank){stage=code;phaseRank=rank;}
            if(signals.Count>0&&signals[^1].Code==code)return;
            if(signals.Count==64)signals.RemoveAt(0);
            signals.Add(new(Seconds,code));
        }
    }
    internal static string? ClassifyLine(string line)
    {
        // 即使文本含有憑據，也只返回下面的常量，不拼接任何輸入片段。
        if(line.Contains("系統正在更新，正在等待",StringComparison.Ordinal)||line.Contains("lock-frontend",StringComparison.OrdinalIgnoreCase)||line.Contains("Waiting for cache lock",StringComparison.OrdinalIgnoreCase))return "package_lock_wait";
        if(line.Contains("DNS 域名解析失敗",StringComparison.Ordinal)||line.Contains("Could not resolve",StringComparison.OrdinalIgnoreCase)||line.Contains("Temporary failure resolving",StringComparison.OrdinalIgnoreCase))return "dns_failure";
        if(line.Contains("No space left on device",StringComparison.OrdinalIgnoreCase))return "disk_full";
        if(line.Contains("Failed to fetch",StringComparison.OrdinalIgnoreCase))return "package_fetch_failed";
        if(line.Contains("dpkg was interrupted",StringComparison.OrdinalIgnoreCase))return "package_interrupted";
        if(line.Contains("開始部署：",StringComparison.Ordinal)||line.Contains("開始應用目錄掛載配置",StringComparison.Ordinal)||line.Contains("伺服器正在獨立應用設定",StringComparison.Ordinal))return "remote_started";
        if(line.Contains("所需組件已安裝，跳過重複下載。",StringComparison.Ordinal))return "packages_present";
        if(line.Contains("任務請求已上載，正在啟動遠端操作…",StringComparison.Ordinal))return "request_uploaded";
        if(line.Contains("遠端任務已啟動，正在讀取實際安裝日誌…",StringComparison.Ordinal))return "job_started";
        if(line.Contains("SSH 認證成功，檢查作業系統…",StringComparison.Ordinal))return "ssh_authenticated";
        if(line.Contains("部署模組已就緒，檢查伺服器環境…",StringComparison.Ordinal))return "agent_ready";
        if(line.Contains("正在透過 VPS 網絡獲取 Claude Code 官方版本和校驗資訊",StringComparison.Ordinal))return "claude_download_started";
        if(line.Contains("VPS 下載完成，官方 Claude Code 檔案校驗通過",StringComparison.Ordinal))return "claude_download_verified";
        if(line.Contains("Unpacking ",StringComparison.Ordinal))return "package_unpacking";
        if(line.Contains("Setting up ",StringComparison.Ordinal))return "package_configuring";
        return null;
    }
    internal static string FailureSite(string operation,Exception? error)
    {
        if(error==null)return operation=="connection"?"connection":operation is "mount_apply" or "directory_list"?"directory_mount":"unknown";
        foreach(var frame in new StackTrace(error,false).GetFrames()??[])
        {
            var method=frame.GetMethod();
            if(method?.DeclaringType?.FullName=="SafeCode.Desktop.ClientPreferences")
            {
                string? site=method.Name switch{"ClearDraft"=>"account_draft_clear","SaveDraft"=>"account_draft_save","ReadDraft"=>"account_draft_read",_=>null};
                if(site!=null)return site;
            }
        }
        // 遠端協定錯誤與 SSH 通道錯誤才是 remote_task；不能僅因調用棧經過 SshSession
        // 就把本地目錄校驗、參數校驗或安裝前置檢查誤標為遠端失敗。
        Exception cause=error;
        while(cause is InvalidOperationException&&cause is not RemoteOperationException&&cause.InnerException!=null)cause=cause.InnerException;
        if(cause is RemoteOperationException)return "remote_task";
        if(cause is SshAuthenticationException or SshConnectionException or SshOperationTimeoutException or SocketException)
            return operation=="connection"?"connection":"remote_task";
        if(operation=="connection")return "connection";
        if(operation=="ip_policy"&&cause is TimeoutException)return "remote_task";
        if(operation is "mount_apply" or "directory_list")return "directory_mount";
        return "unknown";
    }
    private static string HttpErrorKind(System.Net.Http.HttpRequestException error)
    {
        if(error.HttpRequestError==System.Net.Http.HttpRequestError.SecureConnectionError)return "crypto_error";
        if(error.HttpRequestError is System.Net.Http.HttpRequestError.ConnectionError or System.Net.Http.HttpRequestError.NameResolutionError)return "network_error";
        return (int?)error.StatusCode switch
        {
            400=>"gateway_invalid_request", 401 or 403=>"gateway_auth_failed", 409=>"gateway_conflict",
            429=>"gateway_rate_limited", 501=>"gateway_not_supported", >=500 and <=599=>"gateway_upstream_failed",
            _=>"http_error"
        };
    }
    internal static string ErrorKind(Exception? error)=>error?.GetType()==typeof(InvalidOperationException)&&error.InnerException!=null?ErrorKind(error.InnerException):error switch{
        null=>"none",RemoteOperationException remote=>remote.Code,JsonException=>"json_invalid",SshAuthenticationException=>"ssh_auth_failed",SshOperationTimeoutException=>"ssh_timeout",SshConnectionException=>"ssh_connection_failed",SocketException=>"network_error",OperationCanceledException=>"cancelled",DirectoryNotFoundException=>"directory_not_found",
        FileNotFoundException=>"file_not_found",UnauthorizedAccessException=>"access_denied",TimeoutException=>"timeout",
        System.Security.Cryptography.CryptographicException=>"crypto_error",System.Net.Http.HttpRequestException http=>HttpErrorKind(http),
        IOException=>"io_error",ArgumentException=>"invalid_argument",_=>"operation_failed"};
    public DiagnosticReport Finish(Exception? error)
    {
        // 對遠端包裝後的異常僅提取已知錯誤碼，不上載原始 message。
        string kind=ErrorKind(error);
        string? signal=error==null?null:ClassifyLine(error.Message);
        if(signal!=null&&PhaseRanks.ContainsKey(signal))signal=null;
        if(signal!=null){Observe(error!.Message);kind=signal;}
        string site=FailureSite(operation,error);
        lock(gate)return new(operation,error==null?"success":kind=="cancelled"?"cancelled":"failure",kind,site,Seconds,signals.ToArray(),omitted,stage,Gateway);
    }
}
