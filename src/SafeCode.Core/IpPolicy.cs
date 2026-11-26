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
        if(connection==null)throw new InvalidOperationException("尚未連線。");
        var info=new PasswordConnectionInfo(ingress,connection.Port,connection.Username,connection.Password){Timeout=SshConnectTimeout};
        PinHostKeyAlgorithms(info);
        var candidate=new SshClient(info){KeepAliveInterval=TimeSpan.FromSeconds(20)};
        candidate.HostKeyReceived+=(_,e)=>e.CanTrust=trustedKey!=null&&CryptographicOperations.FixedTimeEquals(trustedKey,e.HostKey);
        try{await candidate.ConnectAsync(ct);using var command=candidate.CreateCommand("true");command.CommandTimeout=TimeSpan.FromSeconds(5);await command.ExecuteAsync(ct);if(command.ExitStatus!=0)throw new InvalidOperationException("新入口驗證失敗。");return candidate;}
        catch{candidate.Dispose();throw;}
    }
    private async Task WaitIpPolicyAsync(string token,string ingress,CancellationToken ct)
    {
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(IpVerificationTimeout);
        string phase="未能透過新入口建立並驗證 SSH 連線。請檢查新地址的 SSH 連接埠和服務商入站規則。";
        while(true)
        {
            if(ct.IsCancellationRequested)ct.ThrowIfCancellationRequested();
            // 獨立 deadline 到期是驗證超時，不是用戶取消；保留可區分的錯誤類別。
            if(deadline.IsCancellationRequested)throw new TimeoutException("等待新入口生效超時。"+phase);
            try
            {
                using var probe=await ProbeIngressAsync(ingress,deadline.Token);
                phase="新入口 SSH 已連通，但伺服器尚未確認網絡規則生效。";
                using var command=probe.CreateCommand(prefix+"python3 -u "+Quote("/opt/safe-code/network-apply-"+token+".py")+" status "+Quote(token));command.CommandTimeout=TimeSpan.FromSeconds(5);
                await command.ExecuteAsync(deadline.Token);
                if(command.ExitStatus==0){using var doc=JsonDocument.Parse(command.Result);if(doc.RootElement.GetProperty("ready").GetBoolean())return;}
            }
            catch(OperationCanceledException)when(ct.IsCancellationRequested){throw;}
            catch(OperationCanceledException){throw new TimeoutException("等待新入口生效超時。"+phase);}
            catch(Exception){Log?.Invoke("等待網絡規則生效，正在重新驗證入口…");}
            try{await Task.Delay(750,deadline.Token);}
            catch(OperationCanceledException)when(ct.IsCancellationRequested){throw;}
            catch(OperationCanceledException){throw new TimeoutException("等待新入口生效超時。"+phase);}
        }
    }
    private async Task<T> RequestOverControlAsync<T>(string operation, object? options, CancellationToken ct)
    {
        // 緊急回退不能在 SSH 已建立時再依賴新的 SFTP/TCP 連線：入口鎖定後新連線可能被
        // 丟棄，而當前控制通道仍可執行命令。請求體只含操作和短期 token。
        if(client?.IsConnected!=true)throw new InvalidOperationException("SSH 已斷開。");
        string path=remoteDirectory+"/control-"+Guid.NewGuid().ToString("N")+".json";
        string payload=Convert.ToBase64String(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new{op=operation,options,background=false},JsonDefaults.Options)));
        try
        {
            await ExecuteTextAsync(prefix+"sh -c "+Quote("umask 077; printf %s "+Quote(payload)+" | base64 -d > "+Quote(path)),ct);
            string output=await ExecuteTextAsync(prefix+"python3 -u "+Quote(remoteDirectory+"/agent.py")+" "+Quote(path),ct);
            string? json=output.Split('\n').LastOrDefault(x=>x.StartsWith("SC_RESULT:",StringComparison.Ordinal));
            if(json==null)throw new RemoteOperationException("遠端回退任務未返回結果。","operation_failed");
            return JsonSerializer.Deserialize<T>(json[10..],JsonDefaults.Options)??throw new InvalidDataException("遠端回退結果無效。");
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
        if(HasMounts)throw new InvalidOperationException("請先在目錄設定中移除掛載，完成同步後再切換網絡入口。");
        if(client?.IsConnected!=true||connection==null)throw new InvalidOperationException("尚未連線。");
        policy=policy with{SshPort=Port};
        Log?.Invoke("正在檢查已分配 IP 和當前路由…");
        var prepared=await RequestAsync<IpPrepareResult>("ip_prepare",policy,ct);
        bool applied=false;SshClient? candidate=null;string originalIngress=connection.Host;
        try
        {
            Log?.Invoke("已確認 IP 綁定，啟動獨立網絡任務與自動回退…");
            await RequestAsync<System.Text.Json.JsonElement>("ip_apply",new{token=prepared.Token},ct);applied=true;
            Log?.Invoke("伺服器正在獨立應用設定，斷線不會中止自動回退…");
            await WaitIpPolicyAsync(prepared.Token,policy.IngressIp,ct);
            Log?.Invoke("規則已生效，驗證全新 SSH 連線與實際出口…");
            candidate=await ProbeIngressAsync(policy.IngressIp,ct);
            // 必須使用新建 TCP 連線驗證，不能複用變更前已建立的 SSH 會話。
            var old=client;var oldConnection=connection;
            client=candidate;connection=connection with{Host=policy.IngressIp};
            try
            {
                var committed=await RequestAsync<IpCommitResult>("ip_commit",new{token=prepared.Token},ct);
                if(!committed.Committed)throw new InvalidOperationException("網絡設定尚未提交。");
                foreach(var tunnel in tunnels)try{tunnel.Dispose();}catch{}tunnels.Clear();
                observedEgressIp=committed.ObservedEgress;Server!.IpPolicy=policy;Server.ObservedEgressIp=observedEgressIp;
                try{Server=await RequestAsync<ServerInfo>("inspect",null,ct);}catch{Log?.Invoke("網絡策略已提交，環境資訊可稍後重新整理。");}
                candidate=null;old.Dispose();
                try{WriteKnownHost();}catch{Log?.Invoke("新入口已生效，本地主機記錄儲存失敗，下次連線需重新核對指紋。");}
                return Server;
            }
            catch{client=old;connection=oldConnection;throw;}
        }
        catch(Exception error)
        {
            // 新入口不可達時，已建立的 SSH 會話通常仍在 established 白名單內。
            // 先嚐試新連線回退；仍失敗則透過當前會話直接執行獨立回退，避免把伺服器
            // 留在新入口不可達、舊入口被阻斷且待處理記錄阻塞重試的狀態。
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
            string suffix=rollbackRequested?"驗證未通過，已執行回退。請重新連線原入口確認狀態。":applied?"驗證未通過；伺服器設有 120 秒獨立回退保護，請稍後重新連線原入口確認狀態。":"未收到網絡變更和回退的確認，請重新連線原入口檢查狀態。";
            Exception cause=error;
            if(error is OperationCanceledException&&!ct.IsCancellationRequested)cause=new TimeoutException("等待新入口生效超時。",error);
            throw new InvalidOperationException(error.Message+"\n"+suffix,cause);
        }
        finally{candidate?.Dispose();}
    }
}
