using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace SafeCode.Desktop;
internal sealed record HostBridgeRequest(string Nonce,string Sid,int Port,string PublicKey);
internal static class HostBridgeInstaller
{
    internal const string PayloadSha256="b67ff8becd203502190ec42ac7787c24c2cd3325ed7700485fe7a1c83d2a7957";
    internal static string Root=>Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),"SafeCodeHostBridge");
    private static readonly SecurityIdentifier Admins=new("S-1-5-32-544"),SystemSid=new("S-1-5-18"),Users=new("S-1-5-32-545");
    internal static string Script(string name)
    {using var stream=Assembly.GetExecutingAssembly().GetManifestResourceStream("SafeCode.Desktop.PowerShell."+name+".ps1")??throw new IOException("缺少本机控制安装模块。");using var reader=new StreamReader(stream);return reader.ReadToEnd();}
    internal static async Task<int> RunAsync(string requestPath)
    {
        try
        {
            using var identity=WindowsIdentity.GetCurrent();
            if(!new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator))throw new InvalidOperationException("部署本机 SSH 需要 Windows 管理员授权。");
            string allowed=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode","HostBridge");
            requestPath=Path.GetFullPath(requestPath);
            if(Path.GetDirectoryName(requestPath)!=allowed||!Regex.IsMatch(Path.GetFileName(requestPath),@"\Arequest-[a-f0-9]{32}\.json\z")||new FileInfo(requestPath).Length>8192)throw new InvalidDataException("本机控制安装请求无效。");
            var request=JsonSerializer.Deserialize<HostBridgeRequest>(await File.ReadAllTextAsync(requestPath))??throw new InvalidDataException("本机控制请求为空。");
            if(request.Sid!=identity.User?.Value||request.Port<49152||request.Port>65535||!Regex.IsMatch(request.Nonce,@"\A[a-f0-9]{32}\z")||!Regex.IsMatch(request.PublicKey,@"\Assh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?\z"))throw new InvalidDataException("本机控制安装身份或参数无效。");
            if(!Regex.IsMatch(Environment.UserName,@"\A[a-zA-Z0-9_.@-]{1,128}\z"))throw new InvalidDataException("当前 Windows 账户类型暂不支持本机 SSH 控制。");
            if(Directory.Exists(Root))
            {
                if((File.GetAttributes(Root)&FileAttributes.ReparsePoint)!=0)throw new IOException("本机控制管理目录不能是链接。");
                var owner=new DirectoryInfo(Root).GetAccessControl().GetOwner(typeof(SecurityIdentifier));
                if(owner==null||(!owner.Equals(Admins)&&!owner.Equals(SystemSid)))throw new IOException("本机控制管理目录所有者异常，未执行管理员写入。");
                string active=Path.Combine(Root,"active.json"),lease=Path.Combine(Root,"lease");
                if(File.Exists(active)&&File.Exists(lease)&&DateTime.UtcNow-File.GetLastWriteTimeUtc(lease)<TimeSpan.FromSeconds(35))throw new IOException("已有本机控制会话，请先停用。");
            }
            Directory.CreateDirectory(Root);SecureDirectory(Root);
            string zip=Path.Combine(AppContext.BaseDirectory,"Payloads","openssh","OpenSSH-Win64.zip");
            using(var input=new FileStream(zip,FileMode.Open,FileAccess.Read,FileShare.Read))
            {
                if(!Convert.ToHexString(await SHA256.HashDataAsync(input)).Equals(PayloadSha256,StringComparison.OrdinalIgnoreCase))throw new IOException("随包 OpenSSH 校验失败，未执行安装。");
                input.Position=0;string binary=Path.Combine(Root,"bin");
                if(Directory.Exists(binary)&&(File.GetAttributes(binary)&FileAttributes.ReparsePoint)!=0)throw new IOException("本机 SSH 程序目录不能是链接。");
                // No upstream install-sshd script: preserve existing SSH services.
                if(!Directory.Exists(binary)){Directory.CreateDirectory(binary);using var archive=new ZipArchive(input,ZipArchiveMode.Read,true);archive.ExtractToDirectory(binary);}
            }
            await File.WriteAllTextAsync(Path.Combine(Root,"broker.ps1"),Script("HostBridgeBroker"),new UTF8Encoding(true));
            await File.WriteAllTextAsync(Path.Combine(Root,"request.json"),JsonSerializer.Serialize(new{request.Nonce,request.Sid,request.Port,request.PublicKey,Username=Environment.UserName}));
            string script=Script("HostBridgeInstall");
            var start=new ProcessStartInfo(Path.Combine(Environment.SystemDirectory,"WindowsPowerShell","v1.0","powershell.exe")){UseShellExecute=false,CreateNoWindow=true,RedirectStandardError=true,RedirectStandardOutput=true};
            foreach(string arg in new[]{"-NoProfile","-NonInteractive","-EncodedCommand",Convert.ToBase64String(Encoding.Unicode.GetBytes(script))})start.ArgumentList.Add(arg);
            using var process=Process.Start(start)??throw new IOException("无法启动本机 SSH 安装任务。");
            var output=process.StandardOutput.ReadToEndAsync();var error=process.StandardError.ReadToEndAsync();await process.WaitForExitAsync();
            if(process.ExitCode!=0){try{File.Delete(Path.Combine(Root,"lease"));}catch{}throw new IOException("本机 SSH 安装失败："+(await error).Trim());}
            return 0;
        }
        catch(Exception error){System.Windows.MessageBox.Show(error.Message,"本机远程控制部署失败");return 1;}
    }
    private static void SecureDirectory(string path)
    {
        var acl=new DirectorySecurity();acl.SetAccessRuleProtection(true,false);acl.SetOwner(Admins);
        foreach(var sid in new[]{Admins,SystemSid})acl.AddAccessRule(new FileSystemAccessRule(sid,FileSystemRights.FullControl,InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit,PropagationFlags.None,AccessControlType.Allow));
        acl.AddAccessRule(new FileSystemAccessRule(Users,FileSystemRights.ReadAndExecute,InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit,PropagationFlags.None,AccessControlType.Allow));new DirectoryInfo(path).SetAccessControl(acl);
    }
}
