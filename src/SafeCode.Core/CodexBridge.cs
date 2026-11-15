using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace SafeCode.Core;
public sealed record CodexBridgeInstallResult(bool Ready,string Username,string ConnectCommand,string? Warning=null);
public sealed record CodexBridgeCleanup(bool Ready,string? Warning=null);
public sealed partial class SshSession
{
    public static (string Path,string PublicKey) CreateCodexBridgeKey(string directory)
    {
        Directory.CreateDirectory(directory);string path=System.IO.Path.Combine(directory,"identity.pem");
        if(!File.Exists(path))
        {
            using var rsa=RSA.Create(3072);File.WriteAllText(path,rsa.ExportRSAPrivateKeyPem());var p=rsa.ExportParameters(false);using var stream=new MemoryStream();
            void Put(byte[] bytes){stream.Write(BitConverter.GetBytes(System.Net.IPAddress.HostToNetworkOrder(bytes.Length)));stream.Write(bytes);}
            static byte[] Positive(byte[] bytes)=>(bytes[0]&128)!=0?[0,..bytes]:bytes;
            Put(Encoding.ASCII.GetBytes("ssh-rsa"));Put(Positive(p.Exponent!));Put(Positive(p.Modulus!));File.WriteAllText(path+".pub","ssh-rsa "+Convert.ToBase64String(stream.ToArray())+" safe-code-codex");
        }
        ProtectKeyFile(path);return(path,File.ReadAllText(path+".pub").Trim());
    }
    public async Task<CodexBridgeInstallResult> InstallCodexBridgeAsync(string publicKey,string keyId,string clientId,CancellationToken ct=default)
    {
        string payload;
        await operations.WaitAsync(ct);
        try
        {
            if(!IsConnected)throw new InvalidOperationException("请先连接服务器。");payload=remoteDirectory+"/codex-bridge-"+Guid.NewGuid().ToString("N")+".zip";
            using var resource=Assembly.GetExecutingAssembly().GetManifestResourceStream("SafeCode.Core.Remote.codex_bridge.zip")??throw new IOException("缺少适配器服务器组件。");
            using var sftp=MakeSftp();await sftp.ConnectAsync(ct);await sftp.UploadFileAsync(resource,payload,ct);sftp.ChangePermissions(payload,600);
        }
        finally{operations.Release();}
        return await RequestAsync<CodexBridgeInstallResult>("codex_bridge",new{action="install",payload,publicKey,keyId,clientId},ct);
    }
    public Task<CodexBridgeCleanup> RemoveCodexBridgeAsync(string publicKey,string keyId,string clientId,CancellationToken ct=default)=>RequestAsync<CodexBridgeCleanup>("codex_bridge",new{action="remove",publicKey,keyId,clientId},ct);
}
