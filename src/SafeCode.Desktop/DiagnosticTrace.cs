using System.Diagnostics;
using System.IO;
using System.Text.Json;

namespace SafeCode.Desktop;

// 诊断只输出固定词表，原始日志、异常文本、堆栈和请求参数从不进入报告。
internal sealed record DiagnosticSignal(int Seconds,string Code);
internal sealed record DiagnosticReport(string Operation,string Outcome,string ErrorKind,string FailureSite,int DurationSeconds,DiagnosticSignal[] Signals,int OmittedLines);
internal sealed class DiagnosticTrace(string operation,int generation)
{
    internal static readonly string[] Operations=["account","localization","desktop","chrome","input","claude","ssh","cloud","mount_install","mount_apply","connection","ip_policy"];
    private readonly Stopwatch clock=Stopwatch.StartNew();
    private readonly List<DiagnosticSignal> signals=[];
    private readonly object gate=new();
    private int omitted;
    public string Operation=>operation;
    public int Generation=>generation;
    public static string? OperationFor(string rpc,JsonElement args)
    {
        string? value=rpc switch{
            "deploy"=>args.ValueKind==JsonValueKind.Object&&args.TryGetProperty("step",out var step)&&step.ValueKind==JsonValueKind.String?step.GetString():null,
            "job.resume"=>args.ValueKind==JsonValueKind.Object&&args.TryGetProperty("operation",out var op)&&op.ValueKind==JsonValueKind.String?op.GetString():null,
            "cloud.install"=>"cloud","mount.install"=>"mount_install","mount.apply"=>"mount_apply","connect"=>"connection","ip.apply"=>"ip_policy",_=>null};
        return Operations.Contains(value)?value:null;
    }
    private int Seconds=>(int)Math.Clamp(clock.Elapsed.TotalSeconds,0,86400);
    public void Observe(string line)
    {
        string? code=ClassifyLine(line);
        lock(gate){if(code==null){omitted=Math.Min(omitted+1,1000000);return;}if(signals.Count>0&&signals[^1].Code==code)return;if(signals.Count==64)signals.RemoveAt(0);signals.Add(new(Seconds,code));}
    }
    internal static string? ClassifyLine(string line)
    {
        // 即使文本含有凭据，也只返回下面的常量，不拼接任何输入片段。
        if(line.Contains("lock-frontend",StringComparison.OrdinalIgnoreCase)||line.Contains("Waiting for cache lock",StringComparison.OrdinalIgnoreCase))return "package_lock_wait";
        if(line.Contains("Could not resolve",StringComparison.OrdinalIgnoreCase)||line.Contains("Temporary failure resolving",StringComparison.OrdinalIgnoreCase))return "dns_failure";
        if(line.Contains("No space left on device",StringComparison.OrdinalIgnoreCase))return "disk_full";
        if(line.Contains("Failed to fetch",StringComparison.OrdinalIgnoreCase))return "package_fetch_failed";
        if(line.Contains("dpkg was interrupted",StringComparison.OrdinalIgnoreCase))return "package_interrupted";
        if(line.Contains("开始部署：",StringComparison.Ordinal))return "remote_started";
        if(line.Contains("所需组件已安装，跳过重复下载。",StringComparison.Ordinal))return "packages_present";
        if(line.Contains("任务请求已上传，正在启动远端操作…",StringComparison.Ordinal))return "request_uploaded";
        if(line.Contains("远端任务已启动，正在读取实际安装日志…",StringComparison.Ordinal))return "job_started";
        if(line.Contains("SSH 认证成功，检查操作系统…",StringComparison.Ordinal))return "ssh_authenticated";
        if(line.Contains("部署模块已就绪，检查服务器环境…",StringComparison.Ordinal))return "agent_ready";
        if(line.Contains("Unpacking ",StringComparison.Ordinal))return "package_unpacking";
        if(line.Contains("Setting up ",StringComparison.Ordinal))return "package_configuring";
        return null;
    }
    private static string FailureSite(Exception? error)
    {
        if(error==null)return "unknown";
        foreach(var frame in new StackTrace(error,false).GetFrames()??[])
        {
            var method=frame.GetMethod();string? type=method?.DeclaringType?.FullName;
            if(type=="SafeCode.Desktop.ClientPreferences")
            {
                string? site=method!.Name switch{"ClearDraft"=>"account_draft_clear","SaveDraft"=>"account_draft_save","ReadDraft"=>"account_draft_read",_=>null};
                if(site!=null)return site;
            }
            if(type?.StartsWith("SafeCode.Core.SshSession",StringComparison.Ordinal)==true)return "remote_task";
        }
        return "unknown";
    }
    internal static string ErrorKind(Exception? error)=>error switch{
        null=>"none",OperationCanceledException=>"cancelled",DirectoryNotFoundException=>"directory_not_found",
        FileNotFoundException=>"file_not_found",UnauthorizedAccessException=>"access_denied",TimeoutException=>"timeout",
        System.Security.Cryptography.CryptographicException=>"crypto_error",System.Net.Http.HttpRequestException=>"http_error",
        IOException=>"io_error",ArgumentException=>"invalid_argument",_=>"operation_failed"};
    public DiagnosticReport Finish(Exception? error)
    {
        // 对远端包装后的异常仅提取已知错误码，不上传原始 message。
        string kind=ErrorKind(error);
        string? signal=error==null?null:ClassifyLine(error.Message);
        if(signal!=null){Observe(error!.Message);kind=signal;}
        lock(gate)return new(operation,error==null?"success":error is OperationCanceledException?"cancelled":"failure",kind,FailureSite(error),Seconds,signals.ToArray(),omitted);
    }
}
