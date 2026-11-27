using Renci.SshNet;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace SafeCode.Core;
public sealed partial class SshSession
{
    public TimeSpan IpVerificationTimeout {get;init;}=TimeSpan.FromSeconds(55);
    public TimeSpan IpRollbackTimeout {get;init;}=TimeSpan.FromSeconds(25);
    public TimeSpan IpSessionRollbackTimeout {get;init;}=TimeSpan.FromSeconds(12);
    private async Task<SshClient> ProbeIngressAsync(string ingress,CancellationToken ct)
    {
        if(connection==null)throw new InvalidOperationException("尚未连接。");
        var info=new PasswordConnectionInfo(ingress,connection.Port,connection.Username,connection.Password){Timeout=TimeSpan.FromSeconds(18)};
        PinHostKeyAlgorithms(info);
        var candidate=new SshClient(info){KeepAliveInterval=TimeSpan.FromSeconds(20)};
        candidate.HostKeyReceived+=(_,e)=>e.CanTrust=trustedKey!=null&&CryptographicOperations.FixedTimeEquals(trustedKey,e.HostKey);
        try{await candidate.ConnectAsync(ct);using var command=candidate.CreateCommand("true");command.CommandTimeout=TimeSpan.FromSeconds(5);await command.ExecuteAsync(ct);if(command.ExitStatus!=0)throw new InvalidOperationException("新入口验证失败。");return candidate;}
        catch{candidate.Dispose();throw;}
    }
    private async Task WaitIpPolicyAsync(string token,string ingress,CancellationToken ct)
    {
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(IpVerificationTimeout);
        string phase="未能通过新入口建立并验证 SSH 连接。请检查新地址的 SSH 端口和服务商入站规则。";
        while(true)
        {
            if(ct.IsCancellationRequested)ct.ThrowIfCancellationRequested();
            // 独立 deadline 到期是验证超时，不是用户取消；保留可区分的错误类别。
            if(deadline.IsCancellationRequested)throw new TimeoutException("等待新入口生效超时。"+phase);
            try
            {
                using var probe=await ProbeIngressAsync(ingress,deadline.Token);
                phase="新入口 SSH 已连通，但服务器尚未确认网络规则生效。";
                using var command=probe.CreateCommand(prefix+"python3 -u "+Quote("/opt/safe-code/network-apply-"+token+".py")+" status "+Quote(token));command.CommandTimeout=TimeSpan.FromSeconds(5);
                await command.ExecuteAsync(deadline.Token);
                if(command.ExitStatus==0){using var doc=JsonDocument.Parse(command.Result);if(doc.RootElement.GetProperty("ready").GetBoolean())return;}
            }
            catch(OperationCanceledException)when(ct.IsCancellationRequested){throw;}
            catch(OperationCanceledException){throw new TimeoutException("等待新入口生效超时。"+phase);}
            catch(Exception){Log?.Invoke("等待网络规则生效，正在重新验证入口…");}
            try{await Task.Delay(750,deadline.Token);}
            catch(OperationCanceledException)when(ct.IsCancellationRequested){throw;}
            catch(OperationCanceledException){throw new TimeoutException("等待新入口生效超时。"+phase);}
        }
    }
    private async Task<T> RequestOverControlAsync<T>(string operation, object? options, CancellationToken ct)
    {
        // 紧急回退不能在 SSH 已建立时再依赖新的 SFTP/TCP 连接：入口锁定后新连接可能被
        // 丢弃，而当前控制通道仍可执行命令。请求体只含操作和短期 token。
        if(client?.IsConnected!=true)throw new InvalidOperationException("SSH 已断开。");
        string path=remoteDirectory+"/control-"+Guid.NewGuid().ToString("N")+".json";
        string payload=Convert.ToBase64String(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new{op=operation,options,background=false},JsonDefaults.Options)));
        try
        {
            await ExecuteTextAsync(prefix+"sh -c "+Quote("umask 077; printf %s "+Quote(payload)+" | base64 -d > "+Quote(path)),ct);
            string output=await ExecuteTextAsync(prefix+"python3 -u "+Quote(remoteDirectory+"/agent.py")+" "+Quote(path),ct);
            string? json=output.Split('\n').LastOrDefault(x=>x.StartsWith("SC_RESULT:",StringComparison.Ordinal));
            if(json==null)throw new RemoteOperationException("远端回退任务未返回结果。","operation_failed");
            return JsonSerializer.Deserialize<T>(json[10..],JsonDefaults.Options)??throw new InvalidDataException("远端回退结果无效。");
        }
        finally
        {
            if(client?.IsConnected==true)
            {
                using var cleanupDeadline=new CancellationTokenSource(TimeSpan.FromSeconds(3));
                try{await ExecuteTextAsync(prefix+"rm -f -- "+Quote(path),cleanupDeadline.Token);}catch{}
            }
        }
    }
    private static bool ConfirmedIpRollback(JsonElement reply) =>
        reply.ValueKind==JsonValueKind.Object && reply.TryGetProperty("rolledBack",out var result) && result.ValueKind==JsonValueKind.True;

    private async Task<bool> TryRollbackIpAsync(string token,string originalIngress,string proposedIngress)
    {
        using var deadline=new CancellationTokenSource(IpRollbackTimeout);
        foreach(string ingress in new[]{originalIngress,proposedIngress}.Distinct())
        {
            try
            {
                using var recovery=await ProbeIngressAsync(ingress,deadline.Token);
                using var command=recovery.CreateCommand(prefix+"python3 -u "+Quote("/opt/safe-code/network-apply-"+token+".py")+" rollback "+Quote(token));command.CommandTimeout=TimeSpan.FromSeconds(10);
                await command.ExecuteAsync(deadline.Token);
                if(command.ExitStatus==0)
                {
                    using var reply=JsonDocument.Parse(command.Result);
                    if(ConfirmedIpRollback(reply.RootElement))return true;
                }
            }
            catch(Exception){}
        }
        return false;
    }
    public async Task<ServerInfo> ApplyIpPolicyAsync(IpPolicySettings policy,CancellationToken ct=default)
    {
        if(HasMounts)throw new InvalidOperationException("请先在目录设置中移除挂载，完成同步后再切换网络入口。");
        if(client?.IsConnected!=true||connection==null)throw new InvalidOperationException("尚未连接。");
        policy=policy with{SshPort=Port};
        Log?.Invoke("正在检查已分配 IP 和当前路由…");
        var prepared=await RequestAsync<IpPrepareResult>("ip_prepare",policy,ct);
        bool applied=false;SshClient? candidate=null;string originalIngress=connection.Host;
        try
        {
            Log?.Invoke("已确认 IP 绑定，启动独立网络任务与自动回退…");
            await RequestAsync<System.Text.Json.JsonElement>("ip_apply",new{token=prepared.Token},ct);applied=true;
            Log?.Invoke("服务器正在独立应用设置，断线不会中止自动回退…");
            await WaitIpPolicyAsync(prepared.Token,policy.IngressIp,ct);
            Log?.Invoke("规则已生效，验证全新 SSH 连接与实际出口…");
            candidate=await ProbeIngressAsync(policy.IngressIp,ct);
            // 必须使用新建 TCP 连接验证，不能复用变更前已建立的 SSH 会话。
            var old=client;var oldConnection=connection;
            client=candidate;connection=connection with{Host=policy.IngressIp};
            try
            {
                var committed=await RequestAsync<IpCommitResult>("ip_commit",new{token=prepared.Token},ct);
                if(!committed.Committed)throw new InvalidOperationException("网络设置尚未提交。");
                foreach(var tunnel in tunnels)try{tunnel.Dispose();}catch{}tunnels.Clear();
                observedEgressIp=committed.ObservedEgress;Server!.IpPolicy=policy;Server.ObservedEgressIp=observedEgressIp;
                try{Server=await RequestAsync<ServerInfo>("inspect",null,ct);}catch{Log?.Invoke("网络策略已提交，环境信息可稍后刷新。");}
                candidate=null;old.Dispose();
                try{WriteKnownHost();}catch{Log?.Invoke("新入口已生效，本地主机记录保存失败，下次连接需重新核对指纹。");}
                return Server;
            }
            catch{client=old;connection=oldConnection;throw;}
        }
        catch(Exception error)
        {
            // 新入口不可达时，已建立的 SSH 会话通常仍在 established 白名单内。
            // 先尝试新连接回退；仍失败则通过当前会话直接执行独立回退，避免把服务器
            // 留在新入口不可达、旧入口被阻断且待处理记录阻塞重试的状态。
            bool rollbackRequested=await TryRollbackIpAsync(prepared.Token,originalIngress,policy.IngressIp);
            if(!rollbackRequested&&!ct.IsCancellationRequested)
            {
                try
                {
                    using var rollbackDeadline=CancellationTokenSource.CreateLinkedTokenSource(ct);rollbackDeadline.CancelAfter(IpSessionRollbackTimeout);
                    var result=await RequestOverControlAsync<JsonElement>("ip_rollback",new{token=prepared.Token},rollbackDeadline.Token);
                    rollbackRequested=ConfirmedIpRollback(result);
                }
                catch(Exception){}
            }
            string suffix=rollbackRequested?"验证未通过，已执行回退。请重新连接原入口确认状态。":applied?"验证未通过；服务器设有 120 秒独立回退保护，请稍后重新连接原入口确认状态。":"未收到网络变更和回退的确认，请重新连接原入口检查状态。";
            Exception cause=error;
            if(error is OperationCanceledException&&!ct.IsCancellationRequested)cause=new TimeoutException("等待新入口生效超时。",error);
            throw new InvalidOperationException(error.Message+"\n"+suffix,cause);
        }
        finally{candidate?.Dispose();}
    }
}
