using System.IO;
using System.Text.Json;
using SafeCode.Core;

namespace SafeCode.Desktop;

internal sealed record DesktopScalingOptions(bool Enabled = false);

internal sealed class DesktopScalingPreferences(string directory)
{
    private string SettingsPath => Path.Combine(directory, "desktop-scaling.json");

    internal bool Read()
    {
        try
        {
            return JsonSerializer.Deserialize<DesktopScalingOptions>(File.ReadAllText(SettingsPath),
                JsonDefaults.Options)?.Enabled ?? false;
        }
        catch (IOException) { return false; }
        catch (JsonException) { return false; }
    }

    internal void Save(bool enabled)
    {
        Directory.CreateDirectory(directory);
        string pending = SettingsPath + ".new";
        File.WriteAllText(pending, JsonSerializer.Serialize(new DesktopScalingOptions(enabled), JsonDefaults.Options));
        File.Move(pending, SettingsPath, true);
    }
}
