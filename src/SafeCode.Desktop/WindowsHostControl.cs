using SafeCode.Core;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace SafeCode.Desktop;
internal sealed class WindowsHostControl : IDisposable
{
    private readonly string machineId;
    private SshSession? session;
    private CancellationTokenSource? heartbeat;
    private Task? heartbeatTask;
    private string nonce="";
    private readonly SemaphoreSlim gate=new(1,1);
    public bool Enabled=>heartbeat!=null;
    public string DefaultAlias=>"win-"+machineId[..6];
    public HostControlStatus Status=>session?.HostControlStatus??new(false,"disabled",DefaultAlias,"","未启用");
    public WindowsHostControl(string dataDirectory)
    {
        string path=Path.Combine(dataDirectory,"host-machine-id");
        machineId=File.Exists(path)?File.ReadAllText(path).Trim():Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
        if(!Regex.IsMatch(machineId,@"\A[a-f0-9]{32}\z"))throw new IOException("本机控制标识异常，请检查本机配置。");
        Directory.CreateDirectory(dataDirectory);if(!File.Exists(path))File.WriteAllText(path,machineId);
    }
    public async Task<HostControlStatus> EnableAsync(SshSession target,string alias,CancellationToken ct)
    {
        if(!Regex.IsMatch(alias,@"\A[a-z][a-z0-9_-]{0,31}\z"))throw new ArgumentException("别名只能使用小写字母、数字、短横线和下划线，并以字母开头。");
        await gate.WaitAsync(ct);
        if(Enabled){gate.Release();throw new InvalidOperationException("本机远程控制已启用，请先停用。");}
        string clientId=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(machineId+target.Fingerprint)))[..20].ToLowerInvariant();
        try
        {
            var key=await target.PrepareHostControlAsync(clientId,ct);
            var ssh=await StartLocalAsync(key.PublicKey,ct);
            session=target;
            return await target.StartHostControlAsync(clientId,(uint)ssh.Port,ssh.Username,ssh.Sid,ssh.HostKey,alias,ct);
        }
        catch
        {
            StopLease();
            try{await target.StopHostControlAsync(CancellationToken.None);}catch{}
            try{await target.RequestAsync<JsonElement>("host_bridge",new{clientId,action="remove"},CancellationToken.None);}catch{}
            throw;
        }
        finally{gate.Release();}
    }
    internal sealed record LocalSsh(int Port,string Username,string Sid,string HostKey);

    internal async Task<LocalSsh> EnableDeviceAsync(string publicKey,CancellationToken ct)
    {
        await gate.WaitAsync(ct);
        try
        {
            if(Enabled)throw new InvalidOperationException("本机远程控制已启用，请先停用。");
            session=null;
            try{return await StartLocalAsync(publicKey,ct);}
            catch{StopLease();throw;}
        }
        finally{gate.Release();}
    }

    private async Task<LocalSsh> StartLocalAsync(string publicKey,CancellationToken ct)
    {
        string requestPath="";
        try
        {
            string payload=Path.Combine(AppContext.BaseDirectory,"Payloads","openssh","OpenSSH-Win64.zip");
            if(!File.Exists(payload))throw new IOException("当前安装包未包含本机 SSH 组件，请使用完整本机控制测试包。");
            using(var input=File.OpenRead(payload))if(!Convert.ToHexString(await SHA256.HashDataAsync(input,ct)).Equals(HostBridgeInstaller.PayloadSha256,StringComparison.OrdinalIgnoreCase))throw new IOException("本机 SSH 组件校验失败，未请求管理员权限。");
            int port=0;
            for(int i=0;i<25;i++){try{var probe=new TcpListener(IPAddress.Loopback,RandomNumberGenerator.GetInt32(49152,65536));try{probe.Start();port=((IPEndPoint)probe.LocalEndpoint).Port;break;}finally{probe.Stop();}}catch(SocketException){}}
            if(port==0)throw new IOException("未找到空闲的本机 SSH 回环端口。");
            using var identity=WindowsIdentity.GetCurrent();nonce=Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
            string requests=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SafeCode","HostBridge");Directory.CreateDirectory(requests);
            requestPath=Path.Combine(requests,"request-"+nonce+".json");await File.WriteAllTextAsync(requestPath,JsonSerializer.Serialize(new HostBridgeRequest(nonce,identity.User!.Value,port,publicKey)),ct);
            var start=new ProcessStartInfo(Environment.ProcessPath!){UseShellExecute=true,Verb="runas",WorkingDirectory=AppContext.BaseDirectory};start.ArgumentList.Add("--host-bridge-install");start.ArgumentList.Add(requestPath);
            using var installer=Process.Start(start)??throw new IOException("未启动管理员安装任务。");await installer.WaitForExitAsync(ct);
            if(installer.ExitCode!=0)throw new IOException("本机 SSH 未完成部署或管理员授权已取消。");
            using var result=JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(HostBridgeInstaller.Root,"result.json"),ct));var root=result.RootElement;
            if(root.GetProperty("Nonce").GetString()!=nonce||!root.GetProperty("Ready").GetBoolean()||root.GetProperty("Port").GetInt32()!=port)throw new IOException("本机 SSH 安装结果不匹配，未建立控制通道。");
            heartbeat=new();
            var heartbeatToken=heartbeat.Token;
            heartbeatTask=Task.Run(()=>HeartbeatAsync(heartbeatToken));
            var hostKey=root.GetProperty("HostKey").GetString()!;
            return new(port,root.GetProperty("Username").GetString()!,identity.User.Value,hostKey);
        }
        finally{if(requestPath.Length>0)File.Delete(requestPath);}
    }

    private async Task HeartbeatAsync(CancellationToken ct)
    {
        using var timer=new PeriodicTimer(TimeSpan.FromSeconds(2));
        try{do{string stopped=Path.Combine(HostBridgeInstaller.Root,"stopped.txt");if(File.Exists(stopped)&&File.ReadAllText(stopped).Trim()==nonce)throw new IOException("本机 SSH 进程已停止。");File.SetLastWriteTimeUtc(Path.Combine(HostBridgeInstaller.Root,"lease"),DateTime.UtcNow);}while(await timer.WaitForNextTickAsync(ct));}
        catch(OperationCanceledException){}catch{StopLease();if(session!=null)try{await session.StopHostControlAsync();}catch{}}
    }
    private void StopLease()
    {
        heartbeat?.Cancel();heartbeat?.Dispose();heartbeat=null;
        try{string path=Path.Combine(HostBridgeInstaller.Root,"lease");if(File.Exists(path)&&File.ReadAllText(path).Trim()==nonce)File.Delete(path);}catch{}
    }
    public async Task<HostControlStatus> DisableAsync(CancellationToken ct=default)
    {
        await gate.WaitAsync(ct);
        try
        {
            StopLease();if(session!=null)await session.StopHostControlAsync(ct);
            for(int i=0;i<40;i++)
            {
                string stopped=Path.Combine(HostBridgeInstaller.Root,"stopped.txt");
                if(!Directory.Exists(HostBridgeInstaller.Root)||(File.Exists(stopped)&&File.ReadAllText(stopped).Trim()==nonce))break;
                await Task.Delay(250,ct);
            }
            return Status;
        }
        finally{gate.Release();}
    }
    public void Dispose()=>StopLease();
}
