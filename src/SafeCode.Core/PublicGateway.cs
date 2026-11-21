using Renci.SshNet.Common;
using System.Diagnostics;
using System.Reflection;
using System.Text.Json;

namespace SafeCode.Core;

public sealed partial class SshSession
{
    private const string PublicGatewayPreferenceFile = "public-gateway-preference.json";
    private readonly SemaphoreSlim gatewayControl = new(1, 1);
    private long lastGatewayCheck;
    private bool gatewayAuthenticationFailed;
    private bool publicGatewayEnabled;
    private Task? gatewayHeartbeat;

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

    public Task<JsonElement> ReadPublicGatewayAsync(bool runtime, CancellationToken ct) =>
        RequestGatewayAsync(new { action = "public_status", runtime }, ct, remember: true);

    public Task<JsonElement> EnablePublicGatewayAsync(CancellationToken ct) =>
        RequestGatewayAsync(new { action = "enable_public", accepted = true }, ct, remember: true);

    public async Task<JsonElement> InstallPublicGatewayAsync(CancellationToken ct)
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
        return await RequestGatewayAsync(new { action = "install", accepted = true, files }, ct,
            timeout: TimeSpan.FromSeconds(100));
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
