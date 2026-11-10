using Renci.SshNet;
using System.Security.Cryptography;
using System.Text.Json;

namespace SafeCode.Core;
public sealed partial class SshSession
{
    private async Task<SshClient> ProbeIngressAsync(string ingress,CancellationToken ct)
    {
        if(connection==null)throw new InvalidOperationException("尚未连接。");
        var info=new PasswordConnectionInfo(ingress,connection.Port,connection.Username,connection.Password){Timeout=TimeSpan.FromSeconds(8)};
        var candidate=new SshClient(info){KeepAliveInterval=TimeSpan.FromSeconds(20)};
        candidate.HostKeyReceived+=(_,e)=>e.CanTrust=trustedKey!=null&&CryptographicOperations.FixedTimeEquals(trustedKey,e.HostKey);
        try{await candidate.ConnectAsync(ct);using var command=candidate.CreateCommand("true");command.CommandTimeout=TimeSpan.FromSeconds(5);await command.ExecuteAsync(ct);if(command.ExitStatus!=0)throw new InvalidOperationException("新入口验证失败。");return candidate;}
        catch{candidate.Dispose();throw;}
    }
    private async Task WaitIpPolicyAsync(string token,string ingress,CancellationToken ct)
    {
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(55));
        while(true)
        {
            deadline.Token.ThrowIfCancellationRequested();
            try
            {
                using var probe=await ProbeIngressAsync(ingress,deadline.Token);
                using var command=probe.CreateCommand(prefix+"python3 -u "+Quote("/opt/safe-code/network-apply-"+token+".py")+" status "+Quote(token));command.CommandTimeout=TimeSpan.FromSeconds(5);
                await command.ExecuteAsync(deadline.Token);
                if(command.ExitStatus==0){using var doc=JsonDocument.Parse(command.Result);if(doc.RootElement.GetProperty("ready").GetBoolean())return;}
            }
            catch(OperationCanceledException){throw;}
            catch(Exception){Log?.Invoke("等待网络规则生效，正在重新验证入口…");}
            await Task.Delay(750,deadline.Token);
        }
    }
    private async Task<bool> TryRollbackIpAsync(string token,string originalIngress,string proposedIngress)
    {
        using var deadline=new CancellationTokenSource(TimeSpan.FromSeconds(25));
        foreach(string ingress in new[]{originalIngress,proposedIngress}.Distinct())
        {
            try
            {
                using var recovery=await ProbeIngressAsync(ingress,deadline.Token);
                using var command=recovery.CreateCommand(prefix+"python3 -u "+Quote("/opt/safe-code/network-apply-"+token+".py")+" rollback "+Quote(token));command.CommandTimeout=TimeSpan.FromSeconds(10);
                await command.ExecuteAsync(deadline.Token);
                if(command.ExitStatus==0)return true;
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
            bool rollbackRequested=await TryRollbackIpAsync(prepared.Token,originalIngress,policy.IngressIp);
            if(!applied&&!rollbackRequested)try{await RequestAsync<JsonElement>("ip_rollback",new{token=prepared.Token},ct);}catch{}
            throw new InvalidOperationException(error.Message+"\n"+(applied?(rollbackRequested?"验证未通过，已执行回退。请重新连接原入口确认状态。":"验证未通过；服务器设有 120 秒独立回退保护，请稍后重新连接原入口确认状态。") :"未提交网络变更，原网络设置保留。"));
        }
        finally{candidate?.Dispose();}
    }
}
