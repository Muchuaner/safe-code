namespace SafeCode.Core;

public sealed partial class SshSession
{
    // 僅更新同一伺服器、同一帳號的憑據。驗證失敗不替換舊會話、不關閉檔案橋。
    public async Task ReauthenticateAsync(string password,CancellationToken ct=default)
    {
        if(string.IsNullOrEmpty(password)||password.IndexOfAny(['\r','\n','\0'])>=0)throw new ArgumentException("請輸入當前伺服器登入密碼。");
        await mountLifecycle.WaitAsync(ct);
        try
        {
            await hostGate.WaitAsync(ct);
            try
            {
                await operations.WaitAsync(ct);
                try
                {
                    var settings=(connection??throw new InvalidOperationException("尚未建立過伺服器連線，請返回登入。")) with{Password=password};
                    if(trustedKey==null||mountTrustFailed)throw new InvalidOperationException("伺服器身份需要人工核對，不能僅透過更新密碼恢復。");
                    var replacement=NewMountConnection(settings);
                    try
                    {
                        await replacement.ConnectAsync(ct);
                        using var check=replacement.CreateCommand("cat /etc/os-release");check.CommandTimeout=TimeSpan.FromSeconds(10);await check.ExecuteAsync(ct);
                        if(check.ExitStatus!=0||!ParseOsRelease(check.Result).TryGetValue("ID",out var id)||id!="ubuntu")throw new UnsupportedOsException("恢復目標未通過 Ubuntu 檢查，原連線未替換。");
                        using var privilege=replacement.CreateCommand(prefix.Length>0?"sudo -n true":"test $(id -u) = 0");privilege.CommandTimeout=TimeSpan.FromSeconds(10);await privilege.ExecuteAsync(ct);
                        if(privilege.ExitStatus!=0)throw new InvalidOperationException("該帳戶的安裝權限已變化，未替換原連線。");
                        using var create=replacement.CreateCommand("mktemp -d /tmp/safe-code.XXXXXXXX");create.CommandTimeout=TimeSpan.FromSeconds(10);await create.ExecuteAsync(ct);
                        string directory=create.Result.Trim();
                        if(create.ExitStatus!=0||!directory.StartsWith("/tmp/safe-code.")||directory.Any(char.IsWhiteSpace))throw new IOException("無法準備恢復通道，原連線未替換。");
                        using var sftp=MakeSftp(settings);await sftp.ConnectAsync(ct);
                        await UploadManagementModulesAsync(sftp,directory,ct);
                        var previous=client;connection=settings;client=replacement;remoteDirectory=directory;
                        mountAuthenticationFailed=false;gatewayAuthenticationFailed=false;
                        if(HasMounts){mountState="reconnecting";mountMessage="新密碼已驗證，正在恢復原掛載通道";}
                        foreach(var tunnel in tunnels)DisposeMountResource(tunnel);tunnels.Clear();DisposeMountResource(previous);
                    }
                    catch{DisposeMountResource(replacement);throw;}
                }
                finally{operations.Release();}
            }
            finally{hostGate.Release();}
        }
        finally{mountLifecycle.Release();}
        // 憑據已經驗證並提交；後續恢復超時不應把成功的密碼更新報告為失敗。
        try{await RecoverMountsAsync(ct);}catch(OperationCanceledException)when(ct.IsCancellationRequested){}
    }
}
