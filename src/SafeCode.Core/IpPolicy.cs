using Renci.SshNet;
using System.Security.Cryptography;

namespace SafeCode.Core;
public sealed partial class SshSession
{
    private async Task<SshClient> ProbeIngressAsync(string ingress,CancellationToken ct)
    {
        if(connection==null)throw new InvalidOperationException("尚未连接。");
        var info=new PasswordConnectionInfo(ingress,connection.Port,connection.Username,connection.Password){Timeout=TimeSpan.FromSeconds(15)};
        var candidate=new SshClient(info){KeepAliveInterval=TimeSpan.FromSeconds(20)};
        candidate.HostKeyReceived+=(_,e)=>e.CanTrust=trustedKey!=null&&CryptographicOperations.FixedTimeEquals(trustedKey,e.HostKey);
        try{await candidate.ConnectAsync(ct);using var command=candidate.CreateCommand("true");await command.ExecuteAsync(ct);if(command.ExitStatus!=0)throw new InvalidOperationException("新入口验证失败。");return candidate;}
        catch{candidate.Dispose();throw;}
    }
    public async Task<ServerInfo> ApplyIpPolicyAsync(IpPolicySettings policy,CancellationToken ct=default)
    {
        if(HasMounts)throw new InvalidOperationException("请先在目录设置中移除挂载，完成同步后再切换网络入口。");
        if(client?.IsConnected!=true||connection==null)throw new InvalidOperationException("尚未连接。");
        policy=policy with{SshPort=Port};
        var prepared=await RequestAsync<IpPrepareResult>("ip_prepare",policy,ct);
        bool applied=false;SshClient? candidate=null;
        try
        {
            Log?.Invoke("核对新入口与当前服务器的 SSH 主机密钥…");
            using(var probe=await ProbeIngressAsync(policy.IngressIp,ct)){}
            await RequestAsync<System.Text.Json.JsonElement>("ip_apply",new{token=prepared.Token},ct);applied=true;
            Log?.Invoke("自动回退已启用，验证新连接与实际出口…");
            candidate=await ProbeIngressAsync(policy.IngressIp,ct);
            // 必须使用新建 TCP 连接验证，不能复用变更前已建立的 SSH 会话。
            var old=client;var oldConnection=connection;
            client=candidate;connection=connection with{Host=policy.IngressIp};
            try
            {
                var committed=await RequestAsync<IpCommitResult>("ip_commit",new{token=prepared.Token},ct);
                if(!committed.Committed)throw new InvalidOperationException("网络设置尚未提交。");
                foreach(var tunnel in tunnels)tunnel.Dispose();tunnels.Clear();
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
            try{await RequestAsync<System.Text.Json.JsonElement>("ip_rollback",new{token=prepared.Token},CancellationToken.None);}catch{}
            throw new InvalidOperationException(error.Message+"\n"+(applied?"出入口验证未通过，已请求回退。服务器另有 120 秒独立回退保护，请使用原入口重新检查。":"新入口或已分配 IP 验证失败，未提交网络变更。"));
        }
        finally{candidate?.Dispose();}
    }
}
