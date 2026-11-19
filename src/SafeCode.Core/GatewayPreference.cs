using System.Text.Json;

namespace SafeCode.Core;

// Consent belongs to a verified SSH account, never just the most recently entered host.
public sealed record GatewayPreference(bool Enabled=false,string Host="",int Port=22,string Username="",string Fingerprint="",int LocalPort=18797,string Model="sonnet",bool Writable=false)
{
    public bool Matches(ConnectionSettings settings)=>Enabled&&string.Equals(Host,settings.Host,StringComparison.OrdinalIgnoreCase)&&Port==settings.Port&&Username==settings.Username;
    public bool Matches(ConnectionSettings settings,string fingerprint)=>Matches(settings)&&Fingerprint.Length>0&&Fingerprint==fingerprint;
    public bool Matches(SshSession session)=>Matches(new ConnectionSettings(session.Host,session.LoginUsername,"",session.Port),session.Fingerprint);
    public static GatewayPreference For(SshSession session,int port=18797,string model="sonnet",bool writable=false)=>new(true,session.Host,session.Port,session.LoginUsername,session.Fingerprint,port,model,writable);
    public static GatewayPreference Load(string path)
    {
        try{return File.Exists(path)?JsonSerializer.Deserialize<GatewayPreference>(File.ReadAllText(path))??new():new();}
        catch(Exception error)when(error is IOException or UnauthorizedAccessException or JsonException){return new();}
    }
    public void Save(string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        string temp=path+".new";File.WriteAllText(temp,JsonSerializer.Serialize(this));File.Move(temp,path,true);
    }
}
