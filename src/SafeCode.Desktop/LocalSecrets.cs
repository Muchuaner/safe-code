using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using SafeCode.Core;

namespace SafeCode.Desktop;

// DPAPI CurrentUser: 密文只可由当前 Windows 用户在此系统解密。
internal static class LocalSecrets
{
    internal static string? DirectoryOverride {get;set;}
    private static string FilePath=>Path.Combine(DirectoryOverride??Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode"),"login.dpapi");
    public static byte[] Protect(byte[] bytes)=>ProtectedData.Protect(bytes,null,DataProtectionScope.CurrentUser);
    public static void Save(ConnectionSettings settings)
    {
        byte[] plain=JsonSerializer.SerializeToUtf8Bytes(settings,JsonDefaults.Options);
        try{Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);string temp=FilePath+".new";File.WriteAllBytes(temp,Protect(plain));File.Move(temp,FilePath,true);}finally{CryptographicOperations.ZeroMemory(plain);}
    }
    public static ConnectionSettings? Load()
    {
        if(!File.Exists(FilePath))return null;
        byte[]? plain=null;
        try{plain=ProtectedData.Unprotect(File.ReadAllBytes(FilePath),null,DataProtectionScope.CurrentUser);return JsonSerializer.Deserialize<ConnectionSettings>(plain,JsonDefaults.Options);}catch(CryptographicException){return null;}catch(JsonException){return null;}
        finally{if(plain!=null)CryptographicOperations.ZeroMemory(plain);}
    }
}
