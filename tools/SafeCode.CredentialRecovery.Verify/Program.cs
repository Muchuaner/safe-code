using System.Reflection;
using System.Text.Json;
using Renci.SshNet;
using Renci.SshNet.Common;
using SafeCode.Core;
using SafeCode.Desktop;

using var config=JsonDocument.Parse(File.ReadAllText(args[0]));var value=config.RootElement;
string directory=value.GetProperty("directory").GetString()!;int port=value.GetProperty("port").GetInt32();
byte[] key=Convert.FromBase64String(value.GetProperty("key").GetString()!);
static FieldInfo Field(string name)=>typeof(SshSession).GetField(name,BindingFlags.NonPublic|BindingFlags.Instance)!;
static void Check(bool condition,string message){if(!condition)throw new Exception(message);}
using var session=new SshSession(directory){MountGuardInterval=TimeSpan.FromHours(1)};
var settings=new ConnectionSettings("127.0.0.1","root","fixture-old",port);
var client=new SshClient(settings.Host,settings.Port,settings.Username,settings.Password);await client.ConnectAsync(default);
Field("client").SetValue(session,client);Field("connection").SetValue(session,settings);Field("trustedKey").SetValue(session,key);Field("remoteDirectory").SetValue(session,directory);
typeof(SshSession).GetProperty("Fingerprint")!.SetValue(session,"fixture-fingerprint");
typeof(SshSession).GetProperty("Server")!.SetValue(session,new ServerInfo{MountReady=true});
var prefs=new ClientPreferences(Path.Combine(directory,"preferences"));
Check(!prefs.RequiresAccountConfirmation(session),"First account configuration prompted");
prefs.SaveDraft(session,new("workspace","fixture-host","fixture-work-password"));
Check(prefs.RequiresAccountConfirmation(session),"Second account configuration did not prompt");prefs.ClearDraft(session);
Check(prefs.RequiresAccountConfirmation(session),"Account confirmation history lost after deployment");
Console.WriteLine("ACCOUNT_FIRST_USE_AND_PERSISTED_CONFIRMATION_PASSED");
var result=await session.ApplyMountsAsync([new(directory,"/home/workspace/fixture")]);
var bridge=Field("fileServer").GetValue(session);var id=Field("mountClientId").GetValue(session);var remotePort=Field("mountRemotePort").GetValue(session);
await session.ExecuteTextAsync("fixture.rotate");
((SshClient)Field("mountTransport").GetValue(session)!).Dispose();Field("mountTransport").SetValue(session,null);await session.RecoverMountsAsync();
Check(session.GetMountStatus().AuthenticationRequired&&session.GetMountStatus().State=="attention","Old password did not pause recovery");
try{await session.ReauthenticateAsync("wrong");throw new Exception("Wrong password accepted");}catch(SshAuthenticationException){}
Check(ReferenceEquals(bridge,Field("fileServer").GetValue(session))&&session.HasMounts,"Wrong password destroyed file bridge");
Check(((ConnectionSettings)Field("connection").GetValue(session)!).Password=="fixture-old","Failed authentication replaced credentials");
Check(session.IsConnected,"Failed authentication closed working control connection");
await session.ExecuteTextAsync("fixture.fail-sftp");
try{await session.ReauthenticateAsync("fixture-new");throw new Exception("Failed SFTP preparation accepted");}catch(SftpPermissionDeniedException){}
Check(ReferenceEquals(bridge,Field("fileServer").GetValue(session))&&session.IsConnected,"Failed recovery preparation destroyed old resources");
Check(((ConnectionSettings)Field("connection").GetValue(session)!).Password=="fixture-old","Failed preparation committed new credentials");
Console.WriteLine("AUTHENTICATED_BUT_FAILED_SFTP_PREPARATION_PRESERVES_OLD_SESSION_PASSED");
await session.ReauthenticateAsync("fixture-new");
Check(session.GetMountStatus().State=="connected"&&!session.GetMountStatus().AuthenticationRequired,"New password did not recover mount");
Check(ReferenceEquals(bridge,Field("fileServer").GetValue(session)),"Recovery replaced file bridge");
Check(Equals(id,Field("mountClientId").GetValue(session))&&Equals(remotePort,Field("mountRemotePort").GetValue(session)),"Recovery changed mount identity or port");
Check(((ConnectionSettings)Field("connection").GetValue(session)!).Password=="fixture-new","New credentials not installed");
await session.RequestAsync<ServerInfo>("inspect");await session.UnmountAsync();Check(!session.HasMounts,"Safe unmount failed after password recovery");
Console.WriteLine("ROOT_PASSWORD_ROTATION_WRONG_PASSWORD_PRESERVATION_SAME_BRIDGE_PORT_AND_SAFE_UNMOUNT_PASSED");
Field("trustedKey").SetValue(session,new byte[key.Length]);
try{await session.ReauthenticateAsync("fixture-new");throw new Exception("Changed host identity accepted");}catch(SshConnectionException){}
Check(session.IsConnected,"Host identity rejection closed old connection");Console.WriteLine("CHANGED_HOST_IDENTITY_REJECTED_PASSED");

// Fixture-only protection; all state lives in a disposable directory, no OS credentials are used.
namespace SafeCode.Desktop
{
    internal static class LocalSecrets
    {
        public static byte[] Protect(byte[] bytes)=>bytes.ToArray();
        public static byte[] Unprotect(byte[] bytes)=>bytes.ToArray();
    }
}
