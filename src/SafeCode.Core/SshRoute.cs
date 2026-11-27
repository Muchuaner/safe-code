using System.Diagnostics;
using System.Net.Sockets;
using System.Security.Cryptography;
using Renci.SshNet;
using Renci.SshNet.Common;
using Renci.SshNet.Messages.Transport;

namespace SafeCode.Core;

internal sealed record SshRoute(ProxyTypes Type, string? ProxyHost = null, int ProxyPort = 0)
{
    internal static readonly SshRoute Direct = new(ProxyTypes.None);
    private static readonly TimeSpan SampleTimeout = TimeSpan.FromSeconds(3);

    internal string Label => Type switch
    {
        ProxyTypes.Http => "系统 HTTP 代理",
        ProxyTypes.Socks5 => "系统 SOCKS5 代理",
        ProxyTypes.Socks4 => "系统 SOCKS4 代理",
        _ => "直连 TCP",
    };

    internal PasswordConnectionInfo Password(ConnectionSettings settings) =>
        new(settings.Host, settings.Port, settings.Username, settings.Password, Type, ProxyHost, ProxyPort);

    internal ConnectionInfo Connection(string host, int port, string user, AuthenticationMethod method) =>
        new(host, port, user, Type, ProxyHost, ProxyPort, string.Empty, string.Empty, method);

    internal static async Task<SshRoute[]> RankAsync(ConnectionSettings settings, CancellationToken ct)
    {
        // Proxy auto-configuration can take longer than a normal direct connection.
        SshRoute? proxy;
        try
        {
            proxy = await Task.Run(() => ResolveProxy(settings), ct).WaitAsync(TimeSpan.FromSeconds(1), ct);
        }
        catch (TimeoutException)
        {
            return [Direct];
        }

        if (proxy is null)
            return [Direct];

        var samples = await Task.WhenAll(Direct.ProbeAsync(settings, ct), proxy.ProbeAsync(settings, ct));
        var directSamples = samples[0];
        var proxySamples = samples[1];

        // Keep the existing direct trust flow if the two paths disagree about the server.
        if (directSamples.Concat(proxySamples).Select(sample => sample.Fingerprint).Distinct().Count() > 1)
            return [Direct];

        // A short sample can also miss an otherwise usable, slow path.
        if (proxySamples.Count == 0)
            return [Direct, proxy];

        if (proxySamples.Count > directSamples.Count ||
            proxySamples.Count == directSamples.Count &&
            proxySamples.Max(sample => sample.Elapsed) + TimeSpan.FromMilliseconds(30) < directSamples.Max(sample => sample.Elapsed))
            return [proxy, Direct];

        return [Direct, proxy];
    }

    private static SshRoute? ResolveProxy(ConnectionSettings settings)
    {
        try
        {
            var target = new UriBuilder(Uri.UriSchemeHttps, settings.Host, settings.Port).Uri;
            if (target.IsLoopback) return null;
            var endpoint = HttpClient.DefaultProxy.GetProxy(target);
            if (endpoint == null || endpoint == target || endpoint.Port is < 1 or > 65535 ||
                endpoint.UserInfo.Length != 0 || string.IsNullOrEmpty(endpoint.IdnHost))
                return null;

            return endpoint.Scheme switch
            {
                "http" when target.HostNameType != UriHostNameType.IPv6 => new(ProxyTypes.Http, endpoint.IdnHost, endpoint.Port),
                "socks5" or "socks5h" => new(ProxyTypes.Socks5, endpoint.IdnHost, endpoint.Port),
                "socks4" => new(ProxyTypes.Socks4, endpoint.IdnHost, endpoint.Port),
                _ => null,
            };
        }
        catch (Exception error) when (error is HttpRequestException or InvalidOperationException or ArgumentException or SocketException)
        {
            return null;
        }
    }

    private sealed record Sample(string Fingerprint, TimeSpan Elapsed);

    private async Task<List<Sample>> ProbeAsync(ConnectionSettings settings, CancellationToken ct)
    {
        var samples = new List<Sample>(2);
        for (var attempt = 0; attempt < 2; attempt++)
        {
            using var deadline = CancellationTokenSource.CreateLinkedTokenSource(ct);
            deadline.CancelAfter(SampleTimeout);
            const string probeUser = "safecode-probe";
            using var authentication = new NoneAuthenticationMethod(probeUser);
            var info = Connection(settings.Host, settings.Port, probeUser, authentication);
            info.Timeout = SampleTimeout;
            using var client = new SshClient(info);
            var started = Stopwatch.GetTimestamp();
            Sample? sample = null;
            client.HostKeyReceived += (_, args) =>
            {
                args.CanTrust = false;
                sample = new(Convert.ToBase64String(SHA256.HashData(args.HostKey)), Stopwatch.GetElapsedTime(started));
            };
            try
            {
                await client.ConnectAsync(deadline.Token);
            }
            catch (OperationCanceledException) when (!ct.IsCancellationRequested) { }
            catch (Exception error) when (IsNetworkFailure(error) ||
                error is SshConnectionException { DisconnectReason: DisconnectReason.HostKeyNotVerifiable }) { }
            ct.ThrowIfCancellationRequested();
            if (sample != null)
                samples.Add(sample);
        }
        return samples;
    }

    internal static bool IsNetworkFailure(Exception error) => error is SocketException or TimeoutException
        or SshOperationTimeoutException or ProxyException
        or SshConnectionException { DisconnectReason: not DisconnectReason.HostKeyNotVerifiable };
}
