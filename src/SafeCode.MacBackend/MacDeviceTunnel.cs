using System.Diagnostics;
using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using SafeCode.Core;
using static SafeCode.Desktop.DesktopKeyAccess;
using Ticket = SafeCode.Core.PublicGatewayTicket;

namespace SafeCode.Desktop;

// The API-only Mac path uses the same per-device authorization as Windows.
// No VPS login is stored or reused by this tunnel.
internal sealed class MacDeviceTunnel : IAsyncDisposable
{
    private readonly Ticket ticket;
    private readonly DeviceBinding device;
    private readonly TcpListener listener = new(IPAddress.Loopback, 0);
    private readonly CancellationTokenSource lifetime = new();
    private Process? process;
    private Task? relay;
    private Task? renewal;
    private volatile bool connected;
    private string configPath = "";

    private MacDeviceTunnel(Ticket ticket, DeviceBinding device)
    {
        this.ticket = ticket;
        this.device = device;
    }

    internal static async Task<MacDeviceTunnel> ConnectAsync(Ticket ticket, DeviceBinding device,
        int sshPort, string dataDirectory, CancellationToken ct)
    {
        var tunnel = new MacDeviceTunnel(ticket, device);
        try
        {
            await tunnel.StartAsync(sshPort, dataDirectory, ct);
            return tunnel;
        }
        catch
        {
            await tunnel.DisposeAsync();
            throw;
        }
    }

    private DeviceReturnPath? returnPath;
    internal int Generation=>returnPath?.Generation??-1;
    internal void Invalidate()=>returnPath?.Invalidate();
    internal string Command => returnPath?.Command ?? "";
    internal string? ConnectionProblem(bool localSshReady) => !localSshReady
        ? "本機 SSH 服務未就緒，請在 SafeCode 恢復本機遠端控制。"
        : returnPath==null ? "正在建立裝置回連。" : returnPath.Problem;
    internal async Task CheckReturnAsync(bool force,bool localSshReady,CancellationToken ct)
    {
        if(!localSshReady || returnPath==null)throw new InvalidOperationException(ConnectionProblem(localSshReady));
        await returnPath.EnsureAsync(force,ct);
    }

    internal HostControlStatus Status(bool localSshReady)
    {
        var reason=ConnectionProblem(localSshReady);
        return new(true,reason==null?"connected":"reconnecting",device.Alias,Command,
            reason??"已驗證 Key 網關到本機的 SSH 連線。");
    }

    internal static async Task<DeviceBinding> PrepareAsync(Ticket ticket, CancellationToken ct)
    {
        var response = await DeviceControlAsync(ticket,
            new { action = "prepare", accepted = true, hostname = Environment.MachineName }, ct);
        var device = response.Deserialize<DeviceBinding>(new JsonSerializerOptions(JsonSerializerDefaults.Web))
            ?? throw new IOException("網關返回的裝置資訊無效。");
        if (!Regex.IsMatch(device.DeviceId, @"\A[a-f0-9]{20}\z") ||
            !Regex.IsMatch(device.DeviceProof, @"\A[A-Za-z0-9_-]{43}\z") ||
            device.Alias != "mac-" + device.DeviceId || device.Port is < 1024 or > 65535 ||
            !Regex.IsMatch(device.PublicKey, @"\Assh-ed25519 [A-Za-z0-9+/=]{40,128}\z"))
            throw new IOException("網關返回的裝置資訊無效。");
        return device;
    }

    private async Task StartAsync(int sshPort, string dataDirectory, CancellationToken ct)
    {
        var binary = Path.Combine(AppContext.BaseDirectory, "Payloads", "frp", "frpc");
        var info = new FileInfo(binary);
        if (!info.Exists || info.LinkTarget != null)
            throw new IOException("當前安裝包未包含有效的 Mac FRP 組件。");
        await RenewAsync(ct);
        listener.Start(2);
        relay = RelayAsync(lifetime.Token);
        var localPort = ((IPEndPoint)listener.LocalEndpoint).Port;
        configPath = Path.Combine(dataDirectory, "frp-" + Guid.NewGuid().ToString("N") + ".toml");
        WriteProfileFile(configPath, Encoding.UTF8.GetBytes($"""
            serverAddr = "127.0.0.1"
            serverPort = {localPort}
            loginFailExit = false
            transport.tcpMux = true
            log.to = "console"
            log.level = "info"
            log.disablePrintColor = true
            metadatas.device = "{device.DeviceId}"
            metadatas.proof = "{device.DeviceProof}"
            [[proxies]]
            name = "{device.Alias}"
            type = "tcp"
            localIP = "127.0.0.1"
            localPort = {sshPort}
            remotePort = {device.Port}
            """));
        var start = new ProcessStartInfo(binary)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true
        };
        start.ArgumentList.Add("-c");
        start.ArgumentList.Add(configPath);
        var ready = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        process = new Process { StartInfo = start, EnableRaisingEvents = true };
        process.OutputDataReceived += (_, args) =>
        {
            if (args.Data?.Contains("start proxy success", StringComparison.Ordinal) == true)
                ready.TrySetResult();
        };
        process.ErrorDataReceived += (_, _) => { };
        process.Exited += (_, _) => ready.TrySetException(new IOException("FRP 客戶端已退出。"));
        if (!process.Start()) throw new IOException("未能啟動 FRP 客戶端。");
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        try { await ready.Task.WaitAsync(TimeSpan.FromSeconds(25), ct); }
        catch (TimeoutException) { throw new IOException("FRP 通道未能連到 Key 網關。請檢查網關的 HTTPS 轉發及 FRP 服務。"); }
        File.Delete(configPath);
        configPath = "";

    }

    internal async Task VerifyAsync(MacHostControl.LocalSsh ssh, CancellationToken ct)
    {
        var hostKey = string.Join(' ', ssh.HostKey.Split(' ', StringSplitOptions.RemoveEmptyEntries).Take(2));
        async Task<string> Verify(CancellationToken token)
        {
            var result=await DeviceControlAsync(ticket,new {action="verify",device.DeviceId,device.DeviceProof,ssh.Username,hostKey},token);
            if(result.ValueKind!=JsonValueKind.Object || !result.TryGetProperty("command",out var value) ||
                value.ValueKind!=JsonValueKind.String || string.IsNullOrWhiteSpace(value.GetString()))
                throw new IOException("網關未確認 SSH 連線。");
            return value.GetString()!;
        }
        returnPath=new DeviceReturnPath(RenewAsync,Verify,()=>connected,()=>lifetime.IsCancellationRequested || process?.HasExited!=false,lifetime.Token);
        await returnPath.EnsureAsync(true,ct);
        renewal=returnPath.MonitorAsync();
    }

    private Task<JsonElement> RenewAsync(CancellationToken ct) =>
        DeviceControlAsync(ticket,new { action="renew",device.DeviceId,device.DeviceProof },ct);

    private async Task RelayAsync(CancellationToken ct)
    {
        var gateway = new Uri(ticket.Url);
        var pin = Convert.FromHexString(ticket.Pin);
        while (!ct.IsCancellationRequested)
        {
            try
            {
                using var local = await listener.AcceptTcpClientAsync(ct);
                using var upstream = new TcpClient();
                using var deadline = CancellationTokenSource.CreateLinkedTokenSource(ct);
                deadline.CancelAfter(TimeSpan.FromSeconds(12));
                await upstream.ConnectAsync(gateway.IdnHost, gateway.Port, deadline.Token);
                using var tls = new SslStream(upstream.GetStream(), false, (_, cert, _, _) =>
                {
                    if (cert is null) return false;
                    using var leaf = X509CertificateLoader.LoadCertificate(cert.GetRawCertData());
                    return MatchesPin(pin, leaf);
                });
                await tls.AuthenticateAsClientAsync(new SslClientAuthenticationOptions { TargetHost = gateway.IdnHost }, deadline.Token);
                var request = Encoding.ASCII.GetBytes("CONNECT /v1/device-frp HTTP/1.1\r\n" +
                    "Host: " + gateway.Authority + "\r\n" +
                    "Authorization: Bearer " + ticket.Key + "\r\n" +
                    "X-SafeCode-Platform: macos\r\n" +
                    "X-SafeCode-Client: safecode-desktop\r\n" +
                    "X-SafeCode-Device: " + device.DeviceId + "\r\n" +
                    "X-SafeCode-Device-Proof: " + device.DeviceProof + "\r\n\r\n");
                await tls.WriteAsync(request, deadline.Token);
                await ConfirmTunnelAsync(tls, deadline.Token);
                connected = true;
                using var transfer = CancellationTokenSource.CreateLinkedTokenSource(ct);
                using var stream = local.GetStream();
                var uplink = stream.CopyToAsync(tls, transfer.Token);
                var downlink = tls.CopyToAsync(stream, transfer.Token);
                await Task.WhenAny(uplink, downlink);
                transfer.Cancel();
                await Task.WhenAll(uplink, downlink);
            }
            catch (Exception error) when (error is IOException or SocketException or
                OperationCanceledException or System.Security.Authentication.AuthenticationException or ObjectDisposedException)
            {
                if (ct.IsCancellationRequested) return;
            }
            finally { connected = false; returnPath?.Invalidate(); }
        }
    }

    private static async Task ConfirmTunnelAsync(Stream tls, CancellationToken ct)
    {
        var bytes = new byte[4096];
        for (var at = 0; at < bytes.Length; at++)
        {
            if (await tls.ReadAsync(bytes.AsMemory(at, 1), ct) == 0) break;
            if (at < 3 || !bytes.AsSpan(at - 3, 4).SequenceEqual("\r\n\r\n"u8)) continue;
            var response = bytes.AsSpan(0, at + 1);
            if (response.StartsWith("HTTP/1.1 200 "u8) || response.StartsWith("HTTP/1.0 200 "u8)) return;
            break;
        }
        throw new IOException("網關未接受 FRP 隧道。");
    }

    public async ValueTask DisposeAsync()
    {
        lifetime.Cancel();
        listener.Stop();
        if (process is { } frpc)
        {
            try
            {
                if (!frpc.HasExited)
                {
                    frpc.Kill(entireProcessTree: true);
                    await frpc.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
                }
            }
            catch (InvalidOperationException) { }
        }
        if (relay != null) await relay.ConfigureAwait(false);
        if (renewal != null) await renewal.ConfigureAwait(false);
        if (configPath.Length > 0) File.Delete(configPath);
        process?.Dispose();
        lifetime.Dispose();
    }
}
