using System.Text.Json;
namespace SafeCode.Core;

public sealed class HostTrustRequiredException(HostKeyInfo key):Exception("需要确认服务器身份。")
{ public HostKeyInfo Key {get;}=key; }
public sealed class HostTrustStore(string directory)
{
    private string PathName=>Path.Combine(directory,"host-fingerprints.json");
    private Dictionary<string,string> Read()=>File.Exists(PathName)?JsonSerializer.Deserialize<Dictionary<string,string>>(File.ReadAllText(PathName))??new():new();
    public HostKeyInfo Describe(string host,int port,string fingerprint){var pins=Read();return new(host,port,fingerprint,pins.GetValueOrDefault(host+":"+port));}
    public bool Matches(HostKeyInfo key)=>key.PreviousFingerprint==key.Fingerprint;
    public void Accept(HostKeyInfo key)
    {
        var pins=Read();string name=key.Host+":"+key.Port;
        if(pins.GetValueOrDefault(name)!=key.PreviousFingerprint)throw new InvalidOperationException("服务器信任记录已变化，请重新连接并核对。");
        pins[name]=key.Fingerprint;Directory.CreateDirectory(directory);string temp=PathName+".new";
        File.WriteAllText(temp,JsonSerializer.Serialize(pins,JsonDefaults.Options));File.Move(temp,PathName,true);
        // 仅移除该端点的旧公钥；重连核对新指纹后再写入实际公钥。
        string known=Path.Combine(directory,"known_hosts");string endpoint=key.Port==22?key.Host:$"[{key.Host}]:{key.Port}";
        if(File.Exists(known))File.WriteAllLines(known,File.ReadAllLines(known).Where(line=>!line.StartsWith(endpoint+" ",StringComparison.Ordinal)));
    }
}
