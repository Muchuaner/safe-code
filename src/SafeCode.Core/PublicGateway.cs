using Renci.SshNet.Common;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace SafeCode.Core;

public sealed partial class SshSession
{
    private const string PublicGatewayPreferenceFile = "public-gateway-preference.json";
    private readonly SemaphoreSlim gatewayControl = new(1, 1);
    private long lastGatewayCheck;
    private bool gatewayAuthenticationFailed;
    private bool publicGatewayEnabled;
    private Task? gatewayHeartbeat;
    private JsonObject? gatewaySubscription;
    private sealed record GatewayVersionSample(int? Revision, long At);
    private GatewayVersionSample? gatewayVersion;

    public static bool CanResumePublicGateway(string directory, ConnectionSettings settings)
    {
        var saved = GatewayPreference.Load(Path.Combine(directory, PublicGatewayPreferenceFile));
        try
        {
            var fingerprint = new HostTrustStore(directory).Describe(settings.Host, settings.Port, "").PreviousFingerprint;
            return fingerprint != null && saved.Matches(settings, fingerprint);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException)
        {
            return false;
        }
    }

    private static readonly Lazy<int> BundledGatewayRevision = new(() =>
    {
        using var stream = typeof(SshSession).Assembly.GetManifestResourceStream("SafeCode.Core.PublicApi.server.py")
            ?? throw new IOException("缺少 Safe Code 网关安装组件。");
        using var reader = new StreamReader(stream);
        while (reader.ReadLine() is { } line)
        {
            const string prefix = "GATEWAY_REVISION = ";
            if (line.StartsWith(prefix, StringComparison.Ordinal) && int.TryParse(line.AsSpan(prefix.Length), out var revision))
                return revision;
        }
        throw new InvalidDataException("无法读取客户端内置的网关版本。");
    });

    public async Task<JsonElement> ReadPublicGatewayAsync(bool runtime, CancellationToken ct)
    {
        var status = await RequestGatewayAsync(new { action = "public_status", runtime }, ct, remember: true);
        var value = JsonNode.Parse(status.GetRawText())!.AsObject();
        gatewayVersion = SampleGatewayVersion(status);
        value["bundledGatewayRevision"] = BundledGatewayRevision.Value;
        if (runtime && publicGatewayEnabled && gatewaySubscription is { } subscription)
            value["subscription"] = subscription.DeepClone();
        return JsonSerializer.SerializeToElement(value);
    }

    public async Task<JsonElement> RefreshPublicGatewaySubscriptionAsync(CancellationToken ct)
    {
        var result = await RequestGatewayAsync(new { action = "refresh_subscription" }, ct);
        var value = JsonNode.Parse(result.GetRawText())!.AsObject();
        if ((string?)value["state"] != "available" && gatewaySubscription?["sampled_at"] != null)
        {
            value["state"] = "stale";
            value["windows"] = gatewaySubscription["windows"]?.DeepClone();
            value["sampled_at"] = gatewaySubscription["sampled_at"]?.DeepClone();
        }
        gatewaySubscription = value;
        return JsonSerializer.SerializeToElement(value);
    }

    private static GatewayVersionSample SampleGatewayVersion(JsonElement status) =>
        new(status.TryGetProperty("gatewayRevision", out var revision) && revision.ValueKind == JsonValueKind.Number
            && revision.TryGetInt32(out var number) && number > 0 ? number : null, Environment.TickCount64);

    private async Task<JsonElement> WithGatewayOverviewAsync(JsonElement result, bool background, CancellationToken ct)
    {
        var sample = gatewayVersion;
        if (!background || sample is null || Environment.TickCount64 - sample.At >= 60000)
        {
            // Older gateways expose the running revision through public_status, separate from list.
            try
            {
                var status = await RequestGatewayAsync(new { action = "public_status" }, ct, timeout: TimeSpan.FromSeconds(8));
                sample = SampleGatewayVersion(status);
            }
            catch (Exception error) when (!ct.IsCancellationRequested && !mountLifetime.IsCancellationRequested &&
                error is InvalidOperationException or IOException or SshException or SocketException or TimeoutException or OperationCanceledException)
            {
                sample = new(null, Environment.TickCount64);
            }
            gatewayVersion = sample;
        }
        var value = JsonNode.Parse(result.GetRawText())!.AsObject();
        value["gatewayRevision"] = sample.Revision;
        value["gatewayUpdateAvailable"] = sample.Revision is int installed && installed < BundledGatewayRevision.Value;
        if (gatewaySubscription is { } subscription) value["subscription"] = subscription.DeepClone();
        return JsonSerializer.SerializeToElement(value);
    }

    public Task<JsonElement> EnablePublicGatewayAsync(CancellationToken ct) =>
        RequestGatewayAsync(new { action = "enable_public", accepted = true }, ct, remember: true);

    public async Task<bool> CanReuseHostControlForKeyAsync(PublicGatewayTicket ticket, CancellationToken ct)
    {
        string keyId = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(ticket.Key)));
        try
        {
            var response = await RequestGatewayAsync(new { action = "key_host_reuse", keyId, pin = ticket.Pin }, ct,
                timeout: TimeSpan.FromSeconds(8));
            return response.TryGetProperty("allowed", out var allowed) && allowed.ValueKind == JsonValueKind.True;
        }
        catch (Exception error) when (!ct.IsCancellationRequested &&
            error is InvalidOperationException or IOException or SshException or OperationCanceledException or TimeoutException or SocketException)
        {
            return false;
        }
    }

    public Task<JsonElement> BindPublicGatewayHostAsync(PublicGatewayTicket ticket, string hostname, CancellationToken ct)
    {
        RequireVerifiedHostControl();
        string keyId = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(ticket.Key)));
        return RequestGatewayAsync(new
        {
            action = "bind_ssh_device", keyId, pin = ticket.Pin, hostClientId, hostname
        }, ct, timeout: TimeSpan.FromSeconds(15));
    }

    public async Task<JsonElement> ManagePublicGatewayAsync(Dictionary<string, JsonElement> request, CancellationToken ct)
    {
        request.Remove("background", out var background);
        string? action = request.GetValueOrDefault("action").ValueKind == JsonValueKind.String
            ? request["action"].GetString() : null;
        if (action == "refresh_subscription") return await RefreshPublicGatewaySubscriptionAsync(ct);
        if (action is not ("issue" or "refresh_ticket"))
        {
            var result = await RequestGatewayAsync(request, ct);
            return action == "list"
                ? await WithGatewayOverviewAsync(result, background.ValueKind == JsonValueKind.True, ct)
                : result;
        }

        string sshHost = Host;
        string[] resolved = [];
        if (!IPAddress.TryParse(sshHost, out _) && Uri.CheckHostName(sshHost) == UriHostNameType.Dns)
        {
            using var budget = CancellationTokenSource.CreateLinkedTokenSource(ct);
            budget.CancelAfter(TimeSpan.FromSeconds(2));
            try
            {
                resolved = (await Dns.GetHostAddressesAsync(sshHost, budget.Token))
                    .Select(address => address.ToString()).Take(8).ToArray();
            }
            catch (SocketException) { }
            catch (OperationCanceledException) when (!ct.IsCancellationRequested) { }
        }
        var hints = new { sshHost, resolved, assignedIps = Server?.AssignedIps, egressIp = Server?.ObservedEgressIp };
        if (action == "refresh_ticket")
        {
            var saved = PublicGatewayTicket.Parse(request["ticket"].GetString()?.Trim() ?? "");
            string keyId = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(saved.Key)));
            var endpoint = await RequestGatewayAsync(new { action = "ticket_endpoint", keyId, hints }, ct);
            var updated = saved with { Url = endpoint.GetProperty("origin").GetString()!, Pin = endpoint.GetProperty("pin").GetString()! };
            bool changed = !new Uri(updated.Url).Equals(new Uri(saved.Url)) || updated.Pin != saved.Pin;
            return JsonSerializer.SerializeToElement(new { ticket = changed ? updated.Encode() : null, origin = updated.Url, changed });
        }
        request["hints"] = JsonSerializer.SerializeToElement(hints);
        var issued = await RequestGatewayAsync(request, ct);
        string ticket = issued.GetProperty("ticket").GetString()!;
        return JsonSerializer.SerializeToElement(new
        {
            id = issued.GetProperty("id"),
            ticket,
            origin = PublicGatewayTicket.Parse(ticket).Url
        });
    }

    public async Task<JsonElement> InstallPublicGatewayAsync(CancellationToken ct, JsonElement? cloudcli = null)
    {
        var files = await LoadGatewayPackageAsync(ct);
        var result = await RequestGatewayAsync(new { action = "install", accepted = true, files, cloudcli }, ct,
            timeout: TimeSpan.FromMinutes(12));
        if (Server is { } server
            && (result.TryGetProperty("backend", out var backend) && backend.GetString() == "cloudcli"
                || result.TryGetProperty("cloudcliKeyRequired", out var keyRequired) && keyRequired.ValueKind == JsonValueKind.True))
            server.CloudReady = true;
        return result;
    }

    public async Task<JsonElement> RepairPublicGatewayAsync(bool reinstallCloudCLI, CancellationToken ct)
    {
        var files = await LoadGatewayPackageAsync(ct);
        var result = await RequestAsync<JsonElement>("safe_code_api_repair",
            new { action = "repair", accepted = true, files, reinstallCloudCLI }, ct);
        RememberPublicGateway(result);
        return result;
    }

    private static async Task<Dictionary<string, string>> LoadGatewayPackageAsync(CancellationToken ct)
    {
        var assembly = Assembly.GetExecutingAssembly();
        var files = new Dictionary<string, string>();
        const string api = "SafeCode.Core.PublicApi.";
        foreach (var resource in assembly.GetManifestResourceNames().Where(name => name.StartsWith(api, StringComparison.Ordinal))
                     .Concat(["SafeCode.Core.Remote.desktop_gateway.py", "SafeCode.Core.Remote.codex_bridge.zip"]))
        {
            using var stream = assembly.GetManifestResourceStream(resource)
                ?? throw new IOException("缺少 Safe Code API 安装组件。");
            using var buffer = new MemoryStream();
            await stream.CopyToAsync(buffer, ct);
            int prefix = resource.StartsWith(api, StringComparison.Ordinal) ? api.Length : "SafeCode.Core.Remote.".Length;
            files.Add(resource[prefix..],
                Convert.ToBase64String(buffer.ToArray()));
        }
        return files;
    }

    private void RememberPublicGateway(JsonElement result)
    {
        var path = Path.Combine(dataDirectory, PublicGatewayPreferenceFile);
        var current = GatewayPreference.Load(path);
        var enabled = result.TryGetProperty("enabled", out var value) && value.ValueKind == JsonValueKind.True;
        publicGatewayEnabled = enabled;
        if (enabled) gatewayHeartbeat ??= KeepGatewayControlAliveAsync(mountLifetime.Token);
        try
        {
            if (enabled && !current.Matches(this)) GatewayPreference.For(this).Save(path);
            if (!enabled && current.Matches(this)) File.Delete(path);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            // The VPS remains the source of truth if this computer cannot save the resume preference.
        }
    }

    public async Task<JsonElement> RequestGatewayAsync(object options, CancellationToken ct, bool remember = false, TimeSpan? timeout = null)
    {
        using var budget = CancellationTokenSource.CreateLinkedTokenSource(ct, mountLifetime.Token);
        budget.CancelAfter(timeout ?? TimeSpan.FromSeconds(30));
        await gatewayControl.WaitAsync(budget.Token);
        try
        {
            await EnsureGatewayControlAsync(budget.Token);
            var result = await RequestAsync<JsonElement>("safe_code_api", options, budget.Token);
            budget.Token.ThrowIfCancellationRequested();
            if (remember) RememberPublicGateway(result);
            return result;
        }
        catch (SshAuthenticationException) when (!mountTrustFailed)
        {
            gatewayAuthenticationFailed = true;
            throw new InvalidOperationException("SSH 认证失败，请在主窗口更新登录密码。");
        }
        finally { gatewayControl.Release(); }
    }

    private async Task EnsureGatewayControlAsync(CancellationToken ct)
    {
        if (mountTrustFailed)
            throw new InvalidOperationException("服务器身份已变化，请返回登录并核对服务器指纹。");
        if (gatewayAuthenticationFailed)
            throw new InvalidOperationException("SSH 认证失败，请在主窗口更新登录密码。");
        if (IsConnected && lastGatewayCheck != 0 && Stopwatch.GetElapsedTime(lastGatewayCheck) < TimeSpan.FromSeconds(12))
            return;
        try
        {
            await EnsureMountControlAsync(ct);
            lastGatewayCheck = Stopwatch.GetTimestamp();
        }
        catch (SshAuthenticationException) when (!mountTrustFailed)
        {
            gatewayAuthenticationFailed = true;
            throw new InvalidOperationException("SSH 认证失败，请在主窗口更新登录密码。");
        }
        catch (Exception) when (mountTrustFailed)
        {
            throw new InvalidOperationException("服务器身份已变化，请返回登录并核对服务器指纹。");
        }
    }

    private async Task KeepGatewayControlAliveAsync(CancellationToken ct)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(15));
        try
        {
            while (await timer.WaitForNextTickAsync(ct))
            {
                if (!await gatewayControl.WaitAsync(0, ct)) continue;
                try
                {
                    if (!publicGatewayEnabled) continue;
                    using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
                    timeout.CancelAfter(TimeSpan.FromSeconds(25));
                    await EnsureGatewayControlAsync(timeout.Token);
                }
                catch (Exception error) when (error is SshException or IOException or TimeoutException or
                        System.Net.Sockets.SocketException or OperationCanceledException or InvalidOperationException)
                {
                    // The next tick can recover once the network returns. Trust and authentication failures stay paused.
                }
                finally { gatewayControl.Release(); }
            }
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested) { }
    }
}
