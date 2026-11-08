using Renci.SshNet;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace SafeCode.Core;
public sealed record MountResult(string ClientId,List<string> Paths,List<string>? ManagedPaths=null);
public sealed partial class SshSession
{
    private WebDavServer? fileServer;
    private ForwardedPortRemote? fileForward;
    private string mountClientId="";
    public bool HasMounts => fileServer!=null;
    public async Task<MountResult> ApplyMountsAsync(List<MountMapping> mappings,CancellationToken ct=default)
    {
        if(Server?.MountReady!=true)throw new InvalidOperationException("请先安装目录挂载组件。");
        if(mappings.Count==0){if(HasMounts)await UnmountAsync(ct);SaveMountMappings(mappings);return new("",[]);}
        if(mappings.Count>12)throw new ArgumentException("一次最多挂载 12 组目录。");
        var key=CreateWorkspaceKey();mountClientId=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(key.PublicKey)))[..20].ToLowerInvariant();
        var proposed=new WebDavServer(mappings);
        try{if(HasMounts)await UnmountAsync(ct);}catch{proposed.Dispose();throw;}
        fileServer=proposed;
        try
        {
            fileForward=new ForwardedPortRemote("127.0.0.1",0,"127.0.0.1",(uint)fileServer.Port);
            client!.AddForwardedPort(fileForward);fileForward.Start();
            var result=await RequestAsync<MountResult>("mount_apply",new {clientId=mountClientId,port=fileForward.BoundPort,username=fileServer.Username,password=fileServer.Password,mappings},ct);
            SaveMountMappings(mappings);return result;
        }
        catch{fileForward?.Dispose();fileForward=null;fileServer.Dispose();fileServer=null;throw;}
    }
    private string MountSettingsPath=>Path.Combine(dataDirectory,"mounts-"+Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(Fingerprint)))[..20].ToLowerInvariant()+".json");
    private void SaveMountMappings(List<MountMapping> mappings)=>File.WriteAllText(MountSettingsPath,JsonSerializer.Serialize(mappings,JsonDefaults.Options));
    public List<MountMapping> ReadMountMappings()=>File.Exists(MountSettingsPath)?JsonSerializer.Deserialize<List<MountMapping>>(File.ReadAllText(MountSettingsPath),JsonDefaults.Options)??[]:[];
    public async Task UnmountAsync(CancellationToken ct=default)
    {
        if(mountClientId.Length>0&&IsConnected)await RequestAsync<MountResult>("mount_remove",new {clientId=mountClientId},ct);
        fileForward?.Dispose();fileForward=null;fileServer?.Dispose();fileServer=null;mountClientId="";
    }
}
