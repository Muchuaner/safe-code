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
        var key=CreateWorkspaceKey();var clientId=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(key.PublicKey)))[..20].ToLowerInvariant();
        var proposed=new WebDavServer(mappings);
        ForwardedPortRemote? proposedForward=null;MountResult result;
        try
        {
            proposedForward=new ForwardedPortRemote("127.0.0.1",0,"127.0.0.1",(uint)proposed.Port);
            client!.AddForwardedPort(proposedForward);proposedForward.Start();
            // Keep the old file channel alive while the server validates and synchronizes it.
            result=await RequestAsync<MountResult>("mount_apply",new {clientId,port=proposedForward.BoundPort,username=proposed.Username,password=proposed.Password,mappings},ct);
        }
        catch{proposedForward?.Dispose();proposed.Dispose();throw;}
        fileForward?.Dispose();fileServer?.Dispose();fileForward=proposedForward;fileServer=proposed;mountClientId=clientId;
        SaveMountMappings(mappings);return result;
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
