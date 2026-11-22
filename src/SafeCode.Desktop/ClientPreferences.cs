using System.IO;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;
using SafeCode.Core;
namespace SafeCode.Desktop;

internal sealed record AccountDraft(string Username,string Hostname,string Password);
internal sealed record DeploymentChoices(bool AccountSaved,string? LocaleRegion);
public sealed record DesktopResolution(int Width=1280,int Height=800);
internal sealed class ClientPreferences(string directory)
{
    internal static readonly DesktopResolution[] Resolutions=[new(1024,768),new(1280,800),new(1366,768),new(1600,900),new(1920,1080),new(2560,1440),new(2560,1600)];
    private string DraftPath(SshSession session)=>Path.Combine(directory,"account-drafts",Convert.ToHexString(SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(session.Host+":"+session.Port+":"+session.Fingerprint)))+(OperatingSystem.IsMacOS()?".aes":".dpapi"));
    private string DeploymentLocalePath(SshSession session)=>DraftPath(session)+".locale";
    public DeploymentChoices ReadDeploymentChoices(SshSession session)
    {
        string draft=DraftPath(session),localePath=DeploymentLocalePath(session);
        bool accountSaved=File.Exists(draft)||
            (session.Server?.Statuses.GetValueOrDefault("account")==true&&File.Exists(draft+".configured"));
        string? region=File.Exists(localePath)?File.ReadAllText(localePath):null;
        return new(accountSaved,region!=null&&DeploymentCatalog.FindLocale(region)!=null?region:null);
    }
    public DeploymentChoices SaveDeploymentLocale(SshSession session,string region)
    {
        if(DeploymentCatalog.FindLocale(region)==null)throw new ArgumentException("请选择有效的时区和系统语言。");
        string path=DeploymentLocalePath(session);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        string temporary=path+"."+Guid.NewGuid().ToString("N")+".new";
        try{File.WriteAllText(temporary,region);File.Move(temporary,path,true);}
        finally{File.Delete(temporary);}
        return ReadDeploymentChoices(session);
    }
    public LocaleSettings RequireDeploymentChoices(SshSession session)
    {
        var settings=ReadDeploymentChoices(session);
        if(!settings.AccountSaved||settings.LocaleRegion==null)
            throw new InvalidOperationException("请先打开两个「自定义」，分别保存工作账户和本土化设置，再开始部署。");
        return DeploymentCatalog.FindLocale(settings.LocaleRegion)!;
    }
    public bool RequiresAccountConfirmation(SshSession session)=>session.Server?.Statuses.GetValueOrDefault("account")==true||File.Exists(DraftPath(session)+".configured");
    public AccountDraft? ReadDraft(SshSession session)
    {
        string path=DraftPath(session);if(!File.Exists(path))return null;
        byte[] data=LocalSecrets.Unprotect(File.ReadAllBytes(path));
        try{return JsonSerializer.Deserialize<AccountDraft>(data,JsonDefaults.Options);}finally{CryptographicOperations.ZeroMemory(data);}
    }
    public void SaveDraft(SshSession session,AccountDraft draft)
    {
        if(!Regex.IsMatch(draft.Username,"^[a-z][a-z0-9_-]{1,30}$"))throw new ArgumentException("用户名须为 2–31 位小写字母、数字、下划线或短横线。");
        if(!Regex.IsMatch(draft.Hostname,"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"))throw new ArgumentException("机器名称须为 1–63 位字母、数字或短横线。");
        if(draft.Password.Length>0&&(draft.Password.Length<12||draft.Password.Length>256||draft.Password.IndexOfAny(['\r','\n','\0'])>=0))throw new ArgumentException("工作密码须为 12–256 位，不能包含换行或空字符。");
        string path=DraftPath(session);Directory.CreateDirectory(Path.GetDirectoryName(path)!);byte[] data=JsonSerializer.SerializeToUtf8Bytes(draft,JsonDefaults.Options);
        try{File.WriteAllBytes(path+".new",LocalSecrets.Protect(data));File.Move(path+".new",path,true);File.WriteAllText(path+".configured","1");}finally{CryptographicOperations.ZeroMemory(data);}
    }
    public void ClearDraft(SshSession session)
    {
        // 使用默认账户参数时不会创建草稿目录，部署成功后的清理应直接跳过。
        try{File.Delete(DraftPath(session));}catch(DirectoryNotFoundException){}
    }
    public DesktopResolution ReadResolution()
    {
        try{var value=JsonSerializer.Deserialize<DesktopResolution>(File.ReadAllText(Path.Combine(directory,"desktop-resolution.json")),JsonDefaults.Options);return value!=null&&Resolutions.Contains(value)?value:new();}catch(IOException){return new();}catch(JsonException){return new();}
    }
    public void SaveResolution(DesktopResolution resolution)
    {
        if(!Resolutions.Contains(resolution))throw new ArgumentException("请选择支持的分辨率。");
        Directory.CreateDirectory(directory);string path=Path.Combine(directory,"desktop-resolution.json");File.WriteAllText(path+".new",JsonSerializer.Serialize(resolution,JsonDefaults.Options));File.Move(path+".new",path,true);
    }
}
