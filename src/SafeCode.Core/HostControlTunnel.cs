using Renci.SshNet;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace SafeCode.Core;
public sealed record HostControlStatus(bool Enabled,string State,string Alias,string Command,string Message);
public sealed record HostControlIdentity(string PublicKey);
public sealed partial class SshSession
{
    private SshClient? hostTransport;
    private ForwardedPortRemote? hostForward;
    private CancellationTokenSource? hostLifetime;
    private Task? hostGuard;
    private readonly SemaphoreSlim hostGate=new(1,1);
    private string hostClientId="",hostAlias="",hostUser="",hostUserSid="",hostPublicKey="",hostCommand="",hostState="disabled",hostMessage="未启用";
    private uint hostLocalPort;
    public bool HasHostControl=>hostLifetime!=null;
    public HostControlStatus HostControlStatus=>new(HasHostControl,hostState,hostAlias,hostState=="connected"?hostCommand:"",hostMessage);
    public async Task<HostControlIdentity> PrepareHostControlAsync(string clientId,CancellationToken ct=default)
    {
        if(!Regex.IsMatch(clientId,"\\A[a-f0-9]{20}\\z"))throw new ArgumentException("本机控制标识无效。");
        if(HasHostControl)throw new InvalidOperationException("请先停用现有本机控制。");
        return await RequestAsync<HostControlIdentity>("host_bridge",new{clientId,action="prepare"},ct);
    }
    public async Task<HostControlStatus> StartHostControlAsync(string clientId,uint localPort,string username,string userSid,string hostKey,string alias,CancellationToken ct=default)
    {
        if(!Regex.IsMatch(clientId,"\\A[a-f0-9]{20}\\z")||!Regex.IsMatch(alias,"\\A[a-z][a-z0-9_-]{0,31}\\z")||localPort<1024||localPort>65535)throw new ArgumentException("本机控制连接参数无效。");
        await hostGate.WaitAsync(ct);
        try
        {
            if(HasHostControl)throw new InvalidOperationException("本机控制已启用。");
            hostClientId=clientId;hostAlias=alias;hostUser=username;hostUserSid=userSid;hostPublicKey=hostKey;hostLocalPort=localPort;
            hostState="connecting";hostMessage="正在验证本机命令行连接";
            try{await ConnectHostTunnelAsync(ct);hostLifetime=new();var guardToken=hostLifetime.Token;hostGuard=Task.Run(()=>GuardHostTunnelAsync(guardToken));return HostControlStatus;}
            catch{CloseHostTunnel();hostState="disabled";hostMessage="部署失败，未开放本机控制";throw;}
        }
        finally{hostGate.Release();}
    }
    private async Task ConnectHostTunnelAsync(CancellationToken ct)
    {
        CloseHostTunnel();var transport=NewMountConnection();ForwardedPortRemote? forward=null;
        try
        {
            await transport.ConnectAsync(ct);forward=new ForwardedPortRemote("127.0.0.1",0,"127.0.0.1",hostLocalPort);transport.AddForwardedPort(forward);await Task.Run(forward.Start,ct);
            await EnsureMountControlAsync(ct);
            var result=await RequestAsync<JsonElement>("host_bridge",new{clientId=hostClientId,action="configure",port=forward.BoundPort,username=hostUser,userSid=hostUserSid,hostKey=hostPublicKey,alias=hostAlias},ct);
            if(!result.GetProperty("verified").GetBoolean())throw new IOException("本机 SSH 未通过实际登录验证。");
            hostTransport=transport;hostForward=forward;hostCommand=result.GetProperty("command").GetString()!;hostState="connected";hostMessage="已验证，服务器 AI 可访问本机当前账户";
        }
        catch{DisposeMountResource(forward);DisposeMountResource(transport);throw;}
    }
    private async Task GuardHostTunnelAsync(CancellationToken ct)
    {
        using var timer=new PeriodicTimer(TimeSpan.FromSeconds(5));
        try
        {
            while(await timer.WaitForNextTickAsync(ct))
            {
                if(!await hostGate.WaitAsync(0,ct))continue;
                try
                {
                    if(hostTransport?.IsConnected==true&&hostForward?.IsStarted==true)
                    {using var probe=hostTransport.CreateCommand("true");probe.CommandTimeout=TimeSpan.FromSeconds(8);await probe.ExecuteAsync(ct);if(probe.ExitStatus!=0)throw new IOException("本机控制通道检查失败。");continue;}
                    if(mountTrustFailed){hostState="attention";hostMessage="服务器身份变化，已停止自动重连";continue;}
                    hostState="reconnecting";hostMessage="本机控制连接中断，正在重连";await ConnectHostTunnelAsync(ct);
                }
                catch(OperationCanceledException)when(ct.IsCancellationRequested){break;}
                catch{CloseHostTunnel();hostState=mountTrustFailed?"attention":"reconnecting";hostMessage=mountTrustFailed?"服务器身份变化，请核对后重新启用":"连接中断，命令暂不可用；正在重连";}
                finally{hostGate.Release();}
            }
        }
        catch(OperationCanceledException){}
    }
    private void CloseHostTunnel(){DisposeMountResource(hostForward);hostForward=null;DisposeMountResource(hostTransport);hostTransport=null;}
    public async Task StopHostControlAsync(CancellationToken ct=default)
    {
        hostLifetime?.Cancel();await hostGate.WaitAsync(ct);
        try
        {
            CloseHostTunnel();hostLifetime?.Dispose();hostLifetime=null;hostState="disabled";hostCommand="";hostMessage="本机控制已停用";
            if(hostClientId.Length>0&&IsConnected){try{using var cleanup=CancellationTokenSource.CreateLinkedTokenSource(ct);cleanup.CancelAfter(TimeSpan.FromSeconds(12));await RequestAsync<JsonElement>("host_bridge",new{clientId=hostClientId,action="remove"},cleanup.Token);}catch{Log?.Invoke("本机控制通道已关闭，服务器专用配置待下次连接清理。");}}
        }
        finally{hostGate.Release();}
    }
}
