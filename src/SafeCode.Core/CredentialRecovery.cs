namespace SafeCode.Core;

public sealed partial class SshSession
{
    // 仅更新同一服务器、同一账号的凭据。验证失败不替换旧会话、不关闭文件桥。
    public async Task ReauthenticateAsync(string password,CancellationToken ct=default)
    {
        if(string.IsNullOrEmpty(password)||password.IndexOfAny(['\r','\n','\0'])>=0)throw new ArgumentException("请输入当前服务器登录密码。");
        await mountLifecycle.WaitAsync(ct);
        try
        {
            await hostGate.WaitAsync(ct);
            try
            {
                await operations.WaitAsync(ct);
                try
                {
                    var settings=(connection??throw new InvalidOperationException("尚未建立过服务器连接，请返回登录。")) with{Password=password};
                    if(trustedKey==null||mountTrustFailed)throw new InvalidOperationException("服务器身份需要人工核对，不能仅通过更新密码恢复。");
                    var replacement=NewMountConnection(settings);
                    try
                    {
                        await replacement.ConnectAsync(ct);
                        using var check=replacement.CreateCommand("cat /etc/os-release");check.CommandTimeout=TimeSpan.FromSeconds(10);await check.ExecuteAsync(ct);
                        if(check.ExitStatus!=0||!ParseOsRelease(check.Result).TryGetValue("ID",out var id)||id!="ubuntu")throw new UnsupportedOsException("恢复目标未通过 Ubuntu 检查，原连接未替换。");
                        using var privilege=replacement.CreateCommand(prefix.Length>0?"sudo -n true":"test $(id -u) = 0");privilege.CommandTimeout=TimeSpan.FromSeconds(10);await privilege.ExecuteAsync(ct);
                        if(privilege.ExitStatus!=0)throw new InvalidOperationException("该账户的安装权限已变化，未替换原连接。");
                        using var create=replacement.CreateCommand("mktemp -d /tmp/safe-code.XXXXXXXX");create.CommandTimeout=TimeSpan.FromSeconds(10);await create.ExecuteAsync(ct);
                        string directory=create.Result.Trim();
                        if(create.ExitStatus!=0||!directory.StartsWith("/tmp/safe-code.")||directory.Any(char.IsWhiteSpace))throw new IOException("无法准备恢复通道，原连接未替换。");
                        using var sftp=MakeSftp(settings);await sftp.ConnectAsync(ct);
                        await UploadManagementModulesAsync(sftp,directory,ct);
                        var previous=client;connection=settings;client=replacement;remoteDirectory=directory;
                        mountAuthenticationFailed=false;gatewayAuthenticationFailed=false;
                        if(HasMounts){mountState="reconnecting";mountMessage="新密码已验证，正在恢复原挂载通道";}
                        foreach(var tunnel in tunnels)DisposeMountResource(tunnel);tunnels.Clear();DisposeMountResource(previous);
                    }
                    catch{DisposeMountResource(replacement);throw;}
                }
                finally{operations.Release();}
            }
            finally{hostGate.Release();}
        }
        finally{mountLifecycle.Release();}
        // 凭据已经验证并提交；后续恢复超时不应把成功的密码更新报告为失败。
        try{await RecoverMountsAsync(ct);}catch(OperationCanceledException)when(ct.IsCancellationRequested){}
    }
}
