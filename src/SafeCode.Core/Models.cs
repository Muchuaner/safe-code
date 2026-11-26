using System.Text.Json;

namespace SafeCode.Core;

public sealed record ConnectionSettings(string Host, string Username, string Password, int Port = 22);
public sealed record HostKeyInfo(string Host, int Port, string Fingerprint,string? PreviousFingerprint=null);
public sealed record AccountInfo(string Username, string Password);
public sealed record LocaleSettings(string Region, string Timezone, string Locale, string Language);
public sealed record NetworkStats(double? LatencyMs, double LossPercent, int Probes);
public sealed record ServerResourceStats(double CpuPercent, long MemoryUsedBytes, long MemoryTotalBytes, long DiskUsedBytes, long DiskTotalBytes);
public sealed record IpIntelligence(string Ip, string Location, int? Score, string RegionCode, string? City, bool IsSubnetScore,string GeoSource = "簡表",bool HasGeoConflict = false);
public sealed class ServerInfo
{
    public string OsId { get; set; } = "";
    public string OsName { get; set; } = "";
    public string OsVersion { get; set; } = "";
    public string Architecture { get; set; } = "";
    public long MemoryBytes { get; set; }
    public long DiskFreeBytes { get; set; }
    public int CpuCount { get; set; }
    public bool IsRoot { get; set; }
    public string Username { get; set; } = "";
    public string Hostname { get; set; } = "";
    public string HomeDirectory { get; set; } = "";
    public string ObservedEgressIp { get; set; } = "";
    public Dictionary<string, bool> Statuses { get; set; } = new();
    public List<string> AssignedIps { get; set; } = new();
    public string Timezone { get; set; } = "";
    public string SystemLocale { get; set; } = "";
    public bool IpPolicyHealthy { get; set; }
    public string ClaudeVersion { get; set; } = "";
    public bool DesktopReady { get; set; }
    public bool CloudReady { get; set; }
    public bool MountReady { get; set; }
    public IpPolicySettings? IpPolicy { get; set; }
    public SocksEgressStatus? SocksEgress { get; set; }
    public List<RemoteJob> ActiveJobs { get; set; } = [];
}
public sealed record DeploymentStep(string Id, string Title, string Description);
public static class DeploymentCatalog
{
    public static readonly DeploymentStep[] Steps = [
        new("account", "更新系統並設定工作用戶", "先完成系統更新，再建立開發帳戶和配置機器名稱。"),
        new("localization", "本土化設定", "手動選擇時區與系統語言。"),
        new("desktop", "安裝虛擬桌面", "安裝 XFCE 與 xrdp，透過加密隧道訪問。"),
        new("chrome", "安裝 Chrome", "使用 Google 官方軟件源安裝瀏覽器。"),
        new("input", "安裝中文輸入法", "安裝拼音輸入法與中文字體。"),
        new("claude", "安裝 Claude Code", "安裝官方原生程式、Git 和 tmux。"),
        new("ssh", "配置 SSH 安全通道", "配置工作用戶密鑰與持久終端機會話。")
    ];
    public static readonly LocaleSettings[] Locales = [
        new("日本", "Asia/Tokyo", "ja_JP.UTF-8", "日語"),
        new("美國 · 洛杉磯", "America/Los_Angeles", "en_US.UTF-8", "英語"),
        new("美國 · 紐約", "America/New_York", "en_US.UTF-8", "英語"),
        new("新加坡", "Asia/Singapore", "en_SG.UTF-8", "英語"),
        new("英國", "Europe/London", "en_GB.UTF-8", "英語"),
        new("德國", "Europe/Berlin", "de_DE.UTF-8", "德語"),
        new("韓國", "Asia/Seoul", "ko_KR.UTF-8", "韓語"),
        new("德國 · 法蘭克福", "Europe/Berlin", "de_DE.UTF-8", "德語")
    ];
    public static LocaleSettings? FindLocale(string region) => region switch
    {
        "jp" => Locales[0], "us_la" => Locales[1], "us_ny" => Locales[2],
        "sg" => Locales[3], "gb" => Locales[4], "de" => Locales[5], "kr" => Locales[6],
        "de_fra" => Locales[7],
        _ => null
    };
}
public sealed class UnsupportedOsException(string message) : Exception(message);
public static class JsonDefaults
{
    public static readonly JsonSerializerOptions Options = new() { PropertyNameCaseInsensitive = true, PropertyNamingPolicy = JsonNamingPolicy.CamelCase, WriteIndented = true };
}

public sealed record IpPolicySettings(string IngressIp,string EgressIp,bool LockIngress,bool LockEgress,int SshPort=22);
public sealed record IpPrepareResult(string Token,IpPolicySettings Policy);
public sealed record IpCommitResult(IpPolicySettings Policy,string ObservedEgress,bool Committed);

public sealed record SocksEgressStatus(bool Enabled, bool GuardActive, bool TunnelRunning, string? Endpoint, string? ObservedIp, string? Issue);
public sealed record SocksEgressProbe(bool Tcp, bool Udp, string? ObservedIp, string? TcpMessage, string? UdpMessage);

public sealed record RemoteJob(string JobId,string Operation);
public sealed class JobPoll { public List<string> Lines{get;set;}=[]; public long NextOffset{get;set;} public bool Done{get;set;} public JsonElement? Value{get;set;} public string? Error{get;set;} public string? ErrorCode{get;set;} }

public sealed record EgressProbe(string Ip);

public sealed class RemoteOperationException(string message,string? code) : InvalidOperationException(code=="json_invalid"?"伺服器配置或任務結果無法解析，請提交回饋。":message)
{
    public string Code {get;}=Allowed.Contains(code??"")?code!:"operation_failed";
    private static readonly HashSet<string> Allowed=["json_invalid","package_lock_wait","package_fetch_failed","dns_failure","disk_full","package_interrupted","mount_failed","mount_timeout","mount_target_invalid","deployment_busy","access_denied","timeout","invalid_argument","operation_failed"];
}
