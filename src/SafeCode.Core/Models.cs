using System.Text.Json;

namespace SafeCode.Core;

public sealed record ConnectionSettings(string Host, string Username, string Password, int Port = 22);
public sealed record HostKeyInfo(string Host, int Port, string Fingerprint,string? PreviousFingerprint=null);
public sealed record AccountInfo(string Username, string Password);
public sealed record LocaleSettings(string Region, string Timezone, string Locale, string Language);
public sealed record NetworkStats(double? LatencyMs, double LossPercent, int Probes);
public sealed record IpIntelligence(string Ip, string Location, int? Score, string RegionCode, string? City, bool IsSubnetScore,string GeoSource = "简表",bool HasGeoConflict = false);
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
    public List<RemoteJob> ActiveJobs { get; set; } = [];
}
public sealed record DeploymentStep(string Id, string Title, string Description);
public static class DeploymentCatalog
{
    public static readonly DeploymentStep[] Steps = [
        new("account", "设置工作用户和机器名称", "创建日常开发账户，配置独立密码和项目目录。"),
        new("localization", "本土化设置", "按出口所在地配置时区与系统语言。"),
        new("desktop", "安装虚拟桌面", "安装 XFCE 与 xrdp，通过加密隧道访问。"),
        new("chrome", "安装 Chrome", "使用 Google 官方软件源安装浏览器。"),
        new("input", "安装中文输入法", "安装拼音输入法与中文字体。"),
        new("claude", "安装 Claude Code", "安装官方原生程序、Git 和 tmux。"),
        new("ssh", "配置 SSH 安全通道", "配置工作用户密钥与持久终端会话。")
    ];
    public static LocaleSettings? MatchLocale(string country, string? city) => country.ToLowerInvariant() switch
    {
        "jp" => new("日本", "Asia/Tokyo", "ja_JP.UTF-8", "日语"),
        "sg" => new("新加坡", "Asia/Singapore", "en_SG.UTF-8", "英语"),
        "gb" => new("英国", "Europe/London", "en_GB.UTF-8", "英语"),
        "de" => new("德国", "Europe/Berlin", "de_DE.UTF-8", "德语"),
        "kr" => new("韩国", "Asia/Seoul", "ko_KR.UTF-8", "韩语"),
        "us" when city is "New York" => new("美国 · 纽约", "America/New_York", "en_US.UTF-8", "英语"),
        "us" when city is "Los Angeles" => new("美国 · 洛杉矶", "America/Los_Angeles", "en_US.UTF-8", "英语"),
        _ => null
    };
    public static readonly LocaleSettings[] Locales = [
        new("日本", "Asia/Tokyo", "ja_JP.UTF-8", "日语"),
        new("美国 · 洛杉矶", "America/Los_Angeles", "en_US.UTF-8", "英语"),
        new("美国 · 纽约", "America/New_York", "en_US.UTF-8", "英语"),
        new("新加坡", "Asia/Singapore", "en_SG.UTF-8", "英语"),
        new("英国", "Europe/London", "en_GB.UTF-8", "英语"),
        new("德国", "Europe/Berlin", "de_DE.UTF-8", "德语"),
        new("韩国", "Asia/Seoul", "ko_KR.UTF-8", "韩语")
    ];
}
public sealed class UnsupportedOsException(string message) : Exception(message);
public static class JsonDefaults
{
    public static readonly JsonSerializerOptions Options = new() { PropertyNameCaseInsensitive = true, PropertyNamingPolicy = JsonNamingPolicy.CamelCase, WriteIndented = true };
}

public sealed record IpPolicySettings(string IngressIp,string EgressIp,bool LockIngress,bool LockEgress,int SshPort=22);
public sealed record IpPrepareResult(string Token,IpPolicySettings Policy);
public sealed record IpCommitResult(IpPolicySettings Policy,string ObservedEgress,bool Committed);

public sealed record RemoteJob(string JobId,string Operation);
public sealed class JobPoll { public List<string> Lines{get;set;}=[]; public long NextOffset{get;set;} public bool Done{get;set;} public JsonElement? Value{get;set;} public string? Error{get;set;} }

public sealed record EgressProbe(string Ip);
