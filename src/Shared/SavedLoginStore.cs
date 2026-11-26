using System.IO;
using System.Security.Cryptography;
using System.Text.Json;
using SafeCode.Core;

namespace SafeCode.Desktop;

internal sealed class SavedLoginStore(string path, Func<byte[], byte[]> protect, Func<byte[], byte[]> unprotect)
{
    private readonly string backup = path + ".previous";
    internal bool Exists => File.Exists(path) || File.Exists(backup);

    internal ConnectionSettings? Load()
    {
        Exception? fault = null;
        foreach (string candidate in new[] { path, backup })
        {
            try { return Read(candidate); }
            catch (FileNotFoundException) { }
            catch (DirectoryNotFoundException) { }
            catch (Exception error) when (error is CryptographicException or JsonException or IOException or UnauthorizedAccessException)
            {
                fault ??= error;
            }
        }
        if (fault != null)
            throw new IOException("無法解鎖已儲存的登入資料。請確認當前系統帳戶與儲存時一致，或登入後重新儲存。", fault);
        return null;
    }

    internal void Save(ConnectionSettings settings)
    {
        byte[] plain = JsonSerializer.SerializeToUtf8Bytes(settings, JsonDefaults.Options);
        try
        {
            byte[] encrypted = protect(plain);
            // Commit the previous readable ciphertext first. If interrupted, either
            // it or the primary file still contains a complete saved login.
            Checkpoint();
            Write(path, encrypted);
        }
        finally { CryptographicOperations.ZeroMemory(plain); }
    }

    internal void Checkpoint()
    {
        if (!File.Exists(path)) return;
        byte[] previous;
        try
        {
            previous = File.ReadAllBytes(path);
            Decode(previous);
        }
        catch (Exception error) when (error is CryptographicException or JsonException or IOException or UnauthorizedAccessException)
        {
            // A bad primary must not overwrite a usable backup.
            return;
        }
        Write(backup, previous);
    }

    private ConnectionSettings Read(string candidate) => Decode(File.ReadAllBytes(candidate));

    private ConnectionSettings Decode(byte[] encrypted)
    {
        byte[] plain = unprotect(encrypted);
        try
        {
            var settings = JsonSerializer.Deserialize<ConnectionSettings>(plain, JsonDefaults.Options);
            if (settings == null || string.IsNullOrWhiteSpace(settings.Host) ||
                string.IsNullOrWhiteSpace(settings.Username) || settings.Password == null ||
                settings.Port is < 1 or > 65535)
                throw new JsonException("Saved login is incomplete.");
            return settings;
        }
        finally { CryptographicOperations.ZeroMemory(plain); }
    }

    private static void Write(string target, byte[] encrypted)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(target)!);
        string temporary = target + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            var options = new FileStreamOptions { Mode = FileMode.CreateNew, Access = FileAccess.Write, Share = FileShare.None };
            if (!OperatingSystem.IsWindows()) options.UnixCreateMode = UnixFileMode.UserRead | UnixFileMode.UserWrite;
            using (var stream = new FileStream(temporary, options))
            {
                stream.Write(encrypted);
                stream.Flush(true);
            }
            File.Move(temporary, target, true);
        }
        finally { File.Delete(temporary); }
    }
}
