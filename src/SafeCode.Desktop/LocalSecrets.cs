using System.IO;
using System.Security.Cryptography;
using SafeCode.Core;

namespace SafeCode.Desktop;

// DPAPI CurrentUser: 密文只可由当前 Windows 用户在此系统解密。
internal static class LocalSecrets
{
    internal static string? DirectoryOverride {get;set;}
    public static byte[] Unprotect(byte[] bytes)=>ProtectedData.Unprotect(bytes,null,DataProtectionScope.CurrentUser);
    public static byte[] Protect(byte[] bytes)=>ProtectedData.Protect(bytes,null,DataProtectionScope.CurrentUser);
    private static SavedLoginStore Login => new(Path.Combine(DirectoryOverride??Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode"),"login.dpapi"),Protect,Unprotect);
    public static bool HasSavedLogin => Login.Exists;
    public static void Save(ConnectionSettings settings) => Login.Save(settings);
    public static ConnectionSettings? Load() => Login.Load();
    public static void CheckpointLogin() => Login.Checkpoint();
}
