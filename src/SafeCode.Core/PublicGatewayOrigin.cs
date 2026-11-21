using Renci.SshNet.Common;
using System.Net;
using System.Security.Cryptography;
using System.Text.Json;

namespace SafeCode.Core;

public sealed partial class SshSession
{
    private async Task TryConfigurePublicOriginAsync(CancellationToken ct)
    {
        using var budget = CancellationTokenSource.CreateLinkedTokenSource(ct);
        budget.CancelAfter(TimeSpan.FromSeconds(10));
        string sshHost = Host;
        var assignedIps = Server?.AssignedIps.ToArray() ?? [];
        try
        {
            var info = await RequestGatewayAsync(new { action = "origin_candidates", sshHost, assignedIps }, budget.Token);
            if (!info.TryGetProperty("pin", out var pinValue) ||
                !info.TryGetProperty("candidates", out var candidates) || candidates.ValueKind != JsonValueKind.Array) return;
            string? pinText = pinValue.GetString();
            if (pinText?.Length != 64) return;
            byte[] pin = Convert.FromHexString(pinText);
            foreach (var candidate in candidates.EnumerateArray())
            {
                string? origin = candidate.GetString();
                if (!Uri.TryCreate(origin, UriKind.Absolute, out var endpoint) || endpoint.Scheme != Uri.UriSchemeHttps ||
                    !await ProbeGatewayOriginAsync(endpoint, pin, budget.Token)) continue;
                await RequestGatewayAsync(new { action = "set_default_origin", sshHost, assignedIps, origin, pin = pinText },
                    budget.Token);
                return;
            }
        }
        catch (Exception error) when (!ct.IsCancellationRequested &&
            error is InvalidOperationException or HttpRequestException or SshException or IOException or
                TimeoutException or OperationCanceledException or JsonException or FormatException)
        {
            // Discovery is optional. A failed probe leaves the manual address and the requested operation available.
        }
    }

    internal static async Task<bool> ProbeGatewayOriginAsync(Uri origin, byte[] pin, CancellationToken ct)
    {
        using var handler = new HttpClientHandler { UseProxy = false, UseCookies = false, AllowAutoRedirect = false };
        handler.ServerCertificateCustomValidationCallback = (_, cert, _, _) =>
            cert != null && DateTime.UtcNow >= cert.NotBefore.ToUniversalTime() &&
            DateTime.UtcNow <= cert.NotAfter.ToUniversalTime() &&
            CryptographicOperations.FixedTimeEquals(pin, cert.GetCertHash(HashAlgorithmName.SHA256));
        using var http = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(2.5), MaxResponseContentBufferSize = 4096 };
        try
        {
            // Without a Key, the public gateway rejects this request before model discovery.
            using var request = new HttpRequestMessage(HttpMethod.Get, new Uri(origin, "/v1/models"));
            using var response = await http.SendAsync(request, ct);
            if (response.StatusCode != HttpStatusCode.Unauthorized) return false;
            using var body = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync(ct), cancellationToken: ct);
            return body.RootElement.ValueKind == JsonValueKind.Object &&
                body.RootElement.TryGetProperty("error", out var error) && error.ValueKind == JsonValueKind.Object &&
                error.TryGetProperty("type", out var type) && type.ValueKind == JsonValueKind.String &&
                type.GetString() == "authentication_error";
        }
        catch (HttpRequestException) { return false; }
        catch (JsonException) { return false; }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested) { return false; }
    }
}
