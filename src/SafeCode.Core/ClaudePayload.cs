using System.Net.Http;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace SafeCode.Core;

public sealed partial class SshSession
{
    // 仅从官方发布服务获取未修改的稳定版；不缓存或代管用户授权。
    private const string ClaudeReleaseBase="https://downloads.claude.ai/claude-code-releases";
    private async Task<(object Options,string Path)> PrepareClaudePayloadAsync(CancellationToken ct)
    {
        string platform=Server?.Architecture switch {"x86_64"=>"linux-x64","aarch64"=>"linux-arm64",_=>throw new InvalidOperationException("Claude Code 当前仅支持 x86_64 或 ARM64 Ubuntu。")};
        string bundledRoot=Path.Combine(AppContext.BaseDirectory,"Payloads","claude");
        string bundledManifest=Path.Combine(bundledRoot,platform+".json");
        if(File.Exists(bundledManifest))
        {
            using var bundle=JsonDocument.Parse(await File.ReadAllTextAsync(bundledManifest,ct));
            string bundledVersion=bundle.RootElement.GetProperty("version").GetString()??"";
            string bundledHash=bundle.RootElement.GetProperty("sha256").GetString()??"";
            if(!Regex.IsMatch(bundledVersion,@"\A[0-9]+\.[0-9]+\.[0-9]+\z")||!Regex.IsMatch(bundledHash,@"\A[a-f0-9]{64}\z"))throw new InvalidDataException("随包 Claude Code 校验信息无效，请重新获取完整软件包。");
            string bundledFile=Path.Combine(bundledRoot,platform);
            Log?.Invoke("正在校验随包提供的官方 Claude Code，无需联网下载安装文件…");
            await using(var file=File.OpenRead(bundledFile))
                if(!Convert.ToHexString(await SHA256.HashDataAsync(file,ct)).Equals(bundledHash,StringComparison.OrdinalIgnoreCase))throw new InvalidDataException("随包 Claude Code 文件校验失败，未上传或执行，请重新获取完整软件包。");
            return await UploadClaudePayloadAsync(bundledFile,bundledHash,bundledVersion,ct);
        }
        try{return await DownloadClaudeOnServerAsync(ct);}
        catch(RemoteOperationException error) when(error.Code is "dns_failure" or "package_fetch_failed" or "timeout")
        {Log?.Invoke("VPS 无法下载 Claude Code 官方文件，正在尝试本机网络…");}
        using var http=new HttpClient {Timeout=TimeSpan.FromMinutes(10)};
        try
        {
            Log?.Invoke("正在由客户端备用网络获取 Claude Code 官方稳定版…");
            string release=(await http.GetStringAsync(ClaudeReleaseBase+"/stable",ct)).Trim();
            if(!Regex.IsMatch(release,@"\A[0-9]+\.[0-9]+\.[0-9]+\z"))throw new InvalidDataException("Claude Code 官方版本信息无效，请稍后重试。");
            using var manifest=JsonDocument.Parse(await http.GetStringAsync($"{ClaudeReleaseBase}/{release}/manifest.json",ct));
            string digest=manifest.RootElement.GetProperty("platforms").GetProperty(platform).GetProperty("checksum").GetString()??"";
            if(!Regex.IsMatch(digest,@"\A[a-f0-9]{64}\z"))throw new InvalidDataException("Claude Code 官方校验信息无效，已停止安装。");
            string cache=Path.Combine(dataDirectory,"downloads","claude");Directory.CreateDirectory(cache);
            string payload=Path.Combine(cache,release+"-"+platform+"-"+digest);
            async Task<bool> ValidAsync(string file)
            {
                if(!File.Exists(file))return false;
                await using var stream=File.OpenRead(file);
                return Convert.ToHexString(await SHA256.HashDataAsync(stream,ct)).Equals(digest,StringComparison.OrdinalIgnoreCase);
            }
            if(!await ValidAsync(payload))
            {
                string temporary=payload+"."+Guid.NewGuid().ToString("N")+".tmp";
                try
                {
                    Log?.Invoke("正在下载官方 Claude Code 安装文件，首次下载可能需要几分钟…");
                    using var response=await http.GetAsync($"{ClaudeReleaseBase}/{release}/{platform}/claude",HttpCompletionOption.ResponseHeadersRead,ct);response.EnsureSuccessStatusCode();
                    await using(var file=File.Create(temporary))
                    await using(var download=await response.Content.ReadAsStreamAsync(ct))
                    {
                        byte[] buffer=new byte[131072];long received=0;var progress=System.Diagnostics.Stopwatch.StartNew();
                        while(true)
                        {
                            int count=await download.ReadAsync(buffer,ct);if(count==0)break;
                            received+=count;if(received>1024L*1024*1024)throw new InvalidDataException("Claude Code 下载文件异常过大，已停止安装。");
                            await file.WriteAsync(buffer.AsMemory(0,count),ct);
                            if(progress.Elapsed.TotalSeconds>=10){Log?.Invoke("正在下载 Claude Code：已接收 "+(received/1024/1024)+" MB，请稍候…");progress.Restart();}
                        }
                    }
                    if(!await ValidAsync(temporary))throw new InvalidDataException("Claude Code 下载文件校验失败，未上传或执行，请重试。");
                    File.Move(temporary,payload,true);
                }
                finally{File.Delete(temporary);}
            }
            else Log?.Invoke("已找到校验通过的 Claude Code 官方缓存，跳过重复下载。");
            return await UploadClaudePayloadAsync(payload,digest,release,ct);
        }
        catch(HttpRequestException error)
        {
            bool dns=error.HttpRequestError==HttpRequestError.NameResolutionError;
            throw new RemoteOperationException(dns?"无法连接 Claude Code 官方下载服务（downloads.claude.ai）：客户端 DNS 域名解析失败，请检查本机 DNS 或网络后重试。":"无法连接 Claude Code 官方下载服务（downloads.claude.ai），请检查本机网络后重试。",dns?"dns_failure":"package_fetch_failed");
        }
        catch(TaskCanceledException) when(!ct.IsCancellationRequested)
        {throw new TimeoutException("下载 Claude Code 官方文件超时，请检查本机网络后重试。");}
    }
    private async Task<(object Options,string Path)> DownloadClaudeOnServerAsync(CancellationToken ct)
    {
        Log?.Invoke("正在通过 VPS 网络下载并校验 Claude Code 官方稳定版…");
        using var command=(client??throw new InvalidOperationException("服务器未连接。")).CreateCommand(prefix+"python3 -u "+Quote(remoteDirectory+"/agent.py")+" --claude-download");
        command.CommandTimeout=TimeSpan.FromMinutes(12);
        var execution=command.ExecuteAsync(ct);string? json=null,error=null,code=null;
        using var reader=new StreamReader(command.OutputStream);
        while(await reader.ReadLineAsync(ct) is {} line)
        {
            if(line.StartsWith("SC_RESULT:"))json=line[10..];
            else if(line.StartsWith("SC_ERROR_CODE:"))code=line[14..];
            else if(line.StartsWith("SC_ERROR_JSON:"))error=JsonSerializer.Deserialize<string>(line[14..]);
            else Log?.Invoke(line);
        }
        await execution;
        if(command.ExitStatus!=0||json==null)throw new RemoteOperationException(error??"VPS 下载任务未返回结果，请检查服务器连接。",code);
        var options=JsonSerializer.Deserialize<JsonElement>(json);
        return (options,options.GetProperty("claudePayload").GetString()!);
    }
    private async Task<(object Options,string Path)> UploadClaudePayloadAsync(string payload,string digest,string release,CancellationToken ct)
    {
        string remotePayload=remoteDirectory+"/claude-payload-"+Guid.NewGuid().ToString("N");
        Log?.Invoke("Claude Code 官方文件校验通过，正在通过 SSH 上传…");
        using var sftp=MakeSftp();await sftp.ConnectAsync(ct);
        await using var input=File.OpenRead(payload);var progress=System.Diagnostics.Stopwatch.StartNew();
        // 大文件上传时持续显示进度，避免用户将等待误认为卡住。
        await Task.Run(()=>sftp.UploadFile(input,remotePayload,true,received=>{ct.ThrowIfCancellationRequested();if(progress.Elapsed.TotalSeconds>=10){Log?.Invoke("正在上传 Claude Code："+(received*100/(ulong)input.Length)+"%，请稍候…");progress.Restart();}}),ct);
        sftp.ChangePermissions(remotePayload,600);
        return (new {claudePayload=remotePayload,claudeSha256=digest,claudeVersion=release},remotePayload);
    }

}
