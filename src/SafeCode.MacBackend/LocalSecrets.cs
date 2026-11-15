using System.Security.Cryptography;
using System.Text.Json;
using SafeCode.Core;
namespace SafeCode.Desktop;
// The AES key is obtained by the native app from macOS Keychain and passed only over stdin.
internal static class LocalSecrets
{
    internal static string DirectoryOverride="";
    private static byte[] key=[];
    public static void Initialize(byte[] secret){if(secret.Length!=32)throw new ArgumentException("钥匙串密钥无效。");key=secret;}
    public static byte[] Protect(byte[] plain)
    {
        byte[] result=new byte[28+plain.Length];RandomNumberGenerator.Fill(result.AsSpan(0,12));
        using var aes=new AesGcm(key,16);aes.Encrypt(result.AsSpan(0,12),plain,result.AsSpan(28),result.AsSpan(12,16));return result;
    }
    public static byte[] Unprotect(byte[] encrypted)
    {
        if(encrypted.Length<28)throw new CryptographicException("保存的数据不完整。");
        byte[] plain=new byte[encrypted.Length-28];using var aes=new AesGcm(key,16);
        aes.Decrypt(encrypted.AsSpan(0,12),encrypted.AsSpan(28),encrypted.AsSpan(12,16),plain);return plain;
    }
    public static void Save(ConnectionSettings settings)
    {
        byte[] plain=JsonSerializer.SerializeToUtf8Bytes(settings,JsonDefaults.Options);
        try{string path=Path.Combine(DirectoryOverride,"login.aes");File.WriteAllBytes(path+".new",Protect(plain));MacFiles.Protect(path+".new");File.Move(path+".new",path,true);}finally{CryptographicOperations.ZeroMemory(plain);}
    }
    public static ConnectionSettings? Load()
    {
        string path=Path.Combine(DirectoryOverride,"login.aes");if(!File.Exists(path))return null;
        byte[] plain=Unprotect(File.ReadAllBytes(path));try{return JsonSerializer.Deserialize<ConnectionSettings>(plain,JsonDefaults.Options);}finally{CryptographicOperations.ZeroMemory(plain);}
    }
}
