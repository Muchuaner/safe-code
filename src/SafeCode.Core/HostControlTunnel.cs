using Renci.SshNet;
using Renci.SshNet.Common;
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
    private long hostVerifiedAt;
    private int hostNetworkGeneration,hostVerifiedGeneration;
    public int HostNetworkGeneration=>Volatile.Read(ref hostNetworkGeneration);
    public void InvalidateHostVerification()=>Interlocked.Increment(ref hostNetworkGeneration);
    private bool HostVerificationFresh(long milliseconds)
    {
        var age=DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()-Interlocked.Read(ref hostVerifiedAt);
        return age>=0 && age<milliseconds && Volatile.Read(ref hostVerifiedGeneration)==Volatile.Read(ref hostNetworkGeneration);
    }
    private void HostAddressChanged(object? sender,EventArgs args)=>InvalidateHostVerification();
    private string hostClientId="",hostAlias="",hostUser="",hostUserSid="",hostPublicKey="",hostCommand="",hostState="disabled",hostMessage="未启用";
    private uint hostLocalPort;
    public bool HasHostControl=>hostLifetime!=null;
    private bool HasVerifiedHostControl=>hostLifetime is { IsCancellationRequested:false }&&IsConnected&&hostState=="connected"&&
        hostTransport?.IsConnected==true&&hostForward?.IsStarted==true;
    public HostControlStatus HostControlStatus
    {
        get
        {
            if(hostState=="connected"&&(!HasVerifiedHostControl||!HostVerificationFresh(45000)))
                return new(HasHostControl,"reconnecting",hostAlias,"","正在重新验证网关到本机的 SSH 回连");
            return new(HasHostControl,hostState,hostAlias,hostState=="connected"?hostCommand:"",hostMessage);
        }
    }
    public void RequireVerifiedHostControl()
    {
        if(!HasVerifiedHostControl)
            throw new InvalidOperationException("请先在当前服务器上开启「本机远程控制」，并等待 SSH 登录验证通过。");
    }
    public Func<bool> CaptureVerifiedHostControl()
    {
        var lease = hostLifetime;
        RequireVerifiedHostControl();
        return () => ReferenceEquals(hostLifetime, lease) && HasVerifiedHostControl && HostVerificationFresh(45000);
    }
    public async Task<HostControlIdentity> PrepareHostControlAsync(string clientId,CancellationToken ct=default)
    {
        if(!Regex.IsMatch(clientId,"\\A[a-f0-9]{20}\\z"))throw new ArgumentException("本机控制标识无效。");
        if(HasHostControl)throw new InvalidOperationException("请先停用现有本机控制。");
        return await RequestAsync<HostControlIdentity>("host_bridge",new{clientId,action="prepare"},ct);
    }
    public async Task<HostControlStatus> StartHostControlAsync(string clientId,uint localPort,string username,string userSid,string hostKey,string alias,CancellationToken ct=default)
    {
        if(!Regex.IsMatch(clientId,"\\A[a-f0-9]{20}\\z")||!Regex.IsMatch(alias,"\\A[a-z][a-z0-9_-]{0,31}\\z")||(localPort<1024&&!(localPort==22&&userSid.StartsWith("macos:")))||localPort>65535)throw new ArgumentException("本机控制连接参数无效。");
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
            var result=await VerifyHostConnectionAsync("configure",forward.BoundPort,ct);
            hostTransport=transport;hostForward=forward;hostCommand=result.GetProperty("command").GetString()!;hostState="connected";hostMessage="已验证，服务器 AI 可访问本机当前账户";
        }
        catch{DisposeMountResource(forward);DisposeMountResource(transport);throw;}
    }
    private async Task<JsonElement> VerifyHostConnectionAsync(string action,uint port,CancellationToken ct)
    {
        int generation=Volatile.Read(ref hostNetworkGeneration);
        await EnsureMountControlAsync(ct);
        var result=await RequestAsync<JsonElement>("host_bridge",new{clientId=hostClientId,action,port,username=hostUser,userSid=hostUserSid,hostKey=hostPublicKey,alias=hostAlias},ct);
        if(!result.GetProperty("verified").GetBoolean())throw new IOException("本机 SSH 未通过实际登录验证。");
        Interlocked.Exchange(ref hostVerifiedAt,DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
        Volatile.Write(ref hostVerifiedGeneration,generation);
        return result;
    }
    private async Task GuardHostTunnelAsync(CancellationToken ct)
    {
        System.Net.NetworkInformation.NetworkChange.NetworkAddressChanged+=HostAddressChanged;
        using var timer=new PeriodicTimer(TimeSpan.FromSeconds(5));
        try
        {
            while(await timer.WaitForNextTickAsync(ct))
            {
                if(!await hostGate.WaitAsync(0,ct))continue;
                try{await MaintainHostTunnelAsync(ct);}
                finally{hostGate.Release();}
            }
        }
        catch(OperationCanceledException){}
        finally{System.Net.NetworkInformation.NetworkChange.NetworkAddressChanged-=HostAddressChanged;}
    }
    public async Task MaintainHostControlForChatAsync(CancellationToken ct,bool force=false)
    {
        if(!HasHostControl)return;
        using var budget=CancellationTokenSource.CreateLinkedTokenSource(ct);
        budget.CancelAfter(TimeSpan.FromSeconds(10));
        try
        {
            await hostGate.WaitAsync(budget.Token);
            try
            {
                if(hostLifetime is not {IsCancellationRequested:false} current)return;
                using var active=CancellationTokenSource.CreateLinkedTokenSource(current.Token,budget.Token);
                await MaintainHostTunnelAsync(active.Token,force);
            }
            finally{hostGate.Release();}
        }
        catch(OperationCanceledException)when(!ct.IsCancellationRequested){InvalidateHostVerification();}
        catch(ObjectDisposedException){}
    }
    private async Task MaintainHostTunnelAsync(CancellationToken ct,bool force=false)
    {
        if(mountTrustFailed||mountAuthenticationFailed)
        {
            hostState="attention";
            hostMessage=mountTrustFailed?"服务器身份变化，已停止自动重连":"SSH 认证失败，请在主界面更新登录密码";
            return;
        }
        try
        {
            if(HasVerifiedHostControl)
            {
                if(!force && HostVerificationFresh(30000))return;
                if(!force && operations.CurrentCount==0)return;
                using var probe=CancellationTokenSource.CreateLinkedTokenSource(ct);
                probe.CancelAfter(TimeSpan.FromSeconds(18));
                await VerifyHostConnectionAsync("verify",hostForward!.BoundPort,probe.Token);
                return;
            }
            hostState="reconnecting";
            hostMessage="本机控制连接中断，正在重连";
            using var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeout.CancelAfter(TimeSpan.FromSeconds(30));
            await ConnectHostTunnelAsync(timeout.Token);
        }
        catch(OperationCanceledException)when(ct.IsCancellationRequested){throw;}
        catch(Exception error)
        {
            InvalidateHostVerification();
            CloseHostTunnel();
            if(error is SshAuthenticationException)mountAuthenticationFailed=true;
            hostState=mountTrustFailed||mountAuthenticationFailed?"attention":"reconnecting";
            hostMessage=mountTrustFailed?"服务器身份变化，请核对后重新启用":mountAuthenticationFailed?"SSH 认证失败，请在主界面更新登录密码":"连接中断，命令暂不可用；正在重连";
        }
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
