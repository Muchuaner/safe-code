using System.Net;
using System.Net.Http.Headers;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;
using Renci.SshNet;

namespace SafeCode.Core;

public sealed record DesktopGatewayRemote(bool Ready,int Port,string Instance,string? Warning=null);
public sealed record DesktopGatewayModel(string Id,
    [property:JsonPropertyName("display_name")] string DisplayName,
    [property:JsonPropertyName("supported_effort_levels")] string[] SupportedEffortLevels,
    [property:JsonPropertyName("supports_1m")] bool Supports1m,
    [property:JsonPropertyName("anthropic_family_tier")] string? Family,
    [property:JsonPropertyName("auto_mode_classifier")] bool AutoModeClassifier=false);

/// <summary>只创建 VPS 回环网关和 SSH 本地转发，不持有模型 API key。</summary>
public sealed class DesktopVpsGateway : IDisposable
{
    private readonly SshSession session;
    private readonly string clientId;
    private readonly string token;
    private readonly uint localPort;
    private readonly string instance=Guid.NewGuid().ToString("N");
    private readonly HttpClient http=new(new HttpClientHandler{UseProxy=false}){Timeout=TimeSpan.FromSeconds(4)};
    private ForwardedPortLocal? tunnel;
    public bool Connected{get;private set;}
    public string Endpoint{get;private set;}="";
    public string LocalToken=>token;
    public IntegrationUsage Usage{get;private set;}=IntegrationUsage.Unavailable("等待 VPS 原生 CLI usage");
    public long TurnsStarted{get;private set;}
    public DesktopVpsGateway(SshSession session,string clientId,string? localToken=null,uint localPort=0)
    {this.session=session;this.clientId=clientId;this.localPort=localPort;token=localToken??Convert.ToBase64String(RandomNumberGenerator.GetBytes(32)).TrimEnd('=').Replace('+','-').Replace('/','_');}
    private static async Task<string> ResourceAsync(string name,CancellationToken ct)
    {
        using var stream=Assembly.GetExecutingAssembly().GetManifestResourceStream("SafeCode.Core.Remote."+name)??throw new IOException("缺少 Desktop VPS 网关组件。");
        using var buffer=new MemoryStream();await stream.CopyToAsync(buffer,ct);return Convert.ToBase64String(buffer.ToArray());
    }
    private async Task<DesktopGatewayRemote> RequestAsync(string action,CancellationToken ct)
        =>await session.RequestAsync<DesktopGatewayRemote>("desktop_gateway",new{action,clientId,token,instance,source=await ResourceAsync("desktop_gateway.py",ct),bridgeSource=action=="start"?await ResourceAsync("codex_bridge.zip",ct):""},ct);
    public async Task StartAsync(CancellationToken ct=default)
    {
        if(!session.IsConnected)throw new IOException("请先连接目标 VPS。");
        session.RequireVerifiedHostControl();
        try
        {
            var remote=await RequestAsync("start",ct);
            if(!remote.Ready||remote.Instance!=instance||remote.Port is <1024 or >65535)throw new IOException("VPS 网关未确认启动。");
            tunnel=await session.OpenDesktopTunnelAsync((uint)remote.Port,ct,localPort);
            Endpoint="http://127.0.0.1:"+tunnel.BoundPort;
            if(!await RefreshAsync(ct))throw new IOException("VPS 网关未通过 SSH 隧道验证；未改写 Desktop 配置。");
            session.RequireVerifiedHostControl();
        }
        catch
        {
            CloseLocal();try{using var timeout=new CancellationTokenSource(TimeSpan.FromSeconds(15));await RequestAsync("stop",timeout.Token);}catch{}
            throw;
        }
    }
    public async Task<bool> RefreshAsync(CancellationToken ct=default)
    {
        if(tunnel?.IsStarted!=true||!session.IsConnected){Connected=false;return false;}
        try
        {
            using var request=new HttpRequestMessage(HttpMethod.Get,Endpoint+"/health");request.Headers.Authorization=new AuthenticationHeaderValue("Bearer",token);
            using var response=await http.SendAsync(request,ct);response.EnsureSuccessStatusCode();
            using var document=JsonDocument.Parse(await response.Content.ReadAsStringAsync(ct));var root=document.RootElement;
            Connected=root.GetProperty("ok").GetBoolean()&&root.GetProperty("bridge").GetString()=="vps-claude-code"&&root.GetProperty("instance").GetString()==instance;
            if(!Connected){CloseLocal();return false;}
            TurnsStarted=root.GetProperty("turnsStarted").GetInt64();
            if(root.GetProperty("usageAvailable").GetBoolean())
            {
                var usage=root.GetProperty("usage");
                Usage=IntegrationUsage.FromAnthropic(usage.GetProperty("input_tokens").GetInt64(),usage.GetProperty("output_tokens").GetInt64(),usage.GetProperty("cache_read_input_tokens").GetInt64(),usage.GetProperty("cache_creation_input_tokens").GetInt64(),"VPS 原版 Claude Code assistant usage");
            }
            return true;
        }
        catch(OperationCanceledException)when(ct.IsCancellationRequested){throw;}
        catch{Connected=false;return false;}
    }
    public async Task<DesktopGatewayModel[]> ModelsAsync(CancellationToken ct=default)
    {
        using var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct);timeout.CancelAfter(TimeSpan.FromSeconds(35));
        using var catalogHttp=new HttpClient(new HttpClientHandler{UseProxy=false}){Timeout=Timeout.InfiniteTimeSpan};
        using var request=new HttpRequestMessage(HttpMethod.Get,Endpoint+"/v1/models");
        request.Headers.Authorization=new AuthenticationHeaderValue("Bearer",token);
        using var response=await catalogHttp.SendAsync(request,timeout.Token);response.EnsureSuccessStatusCode();
        using var document=JsonDocument.Parse(await response.Content.ReadAsStringAsync(timeout.Token));
        var models=document.RootElement.GetProperty("data").Deserialize<DesktopGatewayModel[]>(JsonDefaults.Options)??[];
        if(models.Length==0)throw new IOException("VPS 原版 Claude Code 未返回可用模型列表。");
        return models;
    }
    public async Task<string?> StopAsync(CancellationToken ct=default)
    {
        CloseLocal();
        try{var result=await RequestAsync("stop",ct);return result.Warning;}
        catch{return "本机端口已关闭；VPS 清理未确认，失去守护后网关将在 90 秒租约到期时停止。";}
    }
    private void CloseLocal(){Connected=false;if(tunnel!=null){try{tunnel.Stop();}catch{}tunnel.Dispose();tunnel=null;}}
    public void Dispose(){CloseLocal();http.Dispose();}
}

public sealed partial class SshSession
{
    internal async Task<ForwardedPortLocal> OpenDesktopTunnelAsync(uint destinationPort,CancellationToken ct,uint localPort=0)
    {
        ct.ThrowIfCancellationRequested();await Task.Yield();
        if(client?.IsConnected!=true)throw new IOException("SSH 已断开。");
        var forward=new ForwardedPortLocal("127.0.0.1",localPort,"127.0.0.1",destinationPort);
        try{client.AddForwardedPort(forward);forward.Start();tunnels.Add(forward);return forward;}
        catch{forward.Dispose();throw;}
    }
}
