using System.IO;

namespace SafeCode.Desktop;

internal enum RdpResizeMode { Original, Scale, Dynamic }

internal sealed class RdpResizePreferences(string directory)
{
    private string FilePath => Path.Combine(directory, "rdp-resize-mode.txt");

    internal RdpResizeMode Read()
    {
        try
        {
            return File.ReadAllText(FilePath).Trim() switch
            {
                nameof(RdpResizeMode.Scale) => RdpResizeMode.Scale,
                nameof(RdpResizeMode.Dynamic) => RdpResizeMode.Dynamic,
                _ => RdpResizeMode.Original
            };
        }
        catch (IOException) { return RdpResizeMode.Original; }
        catch (UnauthorizedAccessException) { return RdpResizeMode.Original; }
    }

    internal bool ReadFullscreen() => File.Exists(Path.Combine(directory, "rdp-fullscreen"));

    internal void SaveFullscreen(bool enabled)
    {
        var path = Path.Combine(directory, "rdp-fullscreen");
        if (enabled)
        {
            Directory.CreateDirectory(directory);
            using var marker = File.Open(path, FileMode.OpenOrCreate);
        }
        else if (File.Exists(path)) File.Delete(path);
    }

    internal void Save(RdpResizeMode mode)
    {
        Directory.CreateDirectory(directory);
        string temp = FilePath + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            File.WriteAllText(temp, mode.ToString());
            File.Move(temp, FilePath, true);
        }
        finally { File.Delete(temp); }
    }
}
