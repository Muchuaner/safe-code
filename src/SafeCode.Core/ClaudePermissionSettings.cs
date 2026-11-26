using System.Text.Json.Nodes;

namespace SafeCode.Core;

// Change only the supported default. Existing folder/session choices remain the client's responsibility.
public sealed class ClaudePermissionSettings(string settingsPath,string backupPath)
{
    private const string Mode="auto";
    private static JsonObject Read(string path)=>File.Exists(path)?JsonNode.Parse(File.ReadAllText(path)) as JsonObject??throw new IOException("Claude 設定格式無效，未修改。"):new();
    private static void Save(string path,JsonObject json)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);string temp=path+".new";
        File.WriteAllText(temp,json.ToJsonString(new(){WriteIndented=true}));File.Move(temp,path,true);
    }
    public void Enable()
    {
        var settings=Read(settingsPath);
        if(settings["permissions"] is not null and not JsonObject)throw new IOException("Claude permissions 設定格式無效，未修改。");
        var permissions=settings["permissions"] as JsonObject??new();
        if(!File.Exists(backupPath))Save(backupPath,new JsonObject{["original"]=permissions["defaultMode"]?.DeepClone()});
        if(permissions["defaultMode"]?.ToString()==Mode)return;
        permissions["defaultMode"]=Mode;
        if(settings["permissions"]==null)settings["permissions"]=permissions;
        Save(settingsPath,settings);
    }
    public void Restore()
    {
        if(!File.Exists(backupPath))return;
        var backup=Read(backupPath);var settings=Read(settingsPath);
        if(settings["permissions"] is JsonObject permissions&&permissions["defaultMode"]?.ToString()==Mode)
        {
            if(backup["original"] is {} original)permissions["defaultMode"]=original.DeepClone();else permissions.Remove("defaultMode");
            Save(settingsPath,settings);
        }
        File.Delete(backupPath);
    }
}
