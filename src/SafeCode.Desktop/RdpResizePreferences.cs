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
