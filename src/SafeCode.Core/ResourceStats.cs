using System.Reflection;
using System.Text.Json;

namespace SafeCode.Core;

public sealed partial class SshSession
{
    private static readonly string ResourceProbe = LoadResourceProbe();
    private static string LoadResourceProbe()
    {
        using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("SafeCode.Core.Remote.resource_stats.py")
            ?? throw new FileNotFoundException("缺少伺服器監控模組。");
        using var reader = new StreamReader(stream);
        return reader.ReadToEnd();
    }

    public async Task<ServerResourceStats> ReadResourceStatsAsync(CancellationToken ct = default)
    {
        if (client?.IsConnected != true) throw new InvalidOperationException("SSH 已斷開，請重新連線。");
        // Run a short read-only command on its own SSH channel. Never enqueue behind
        // deployment tasks or upload a request/configuration file for each sample.
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(TimeSpan.FromSeconds(5));
        using var command = client.CreateCommand("python3 -c " + Quote(ResourceProbe));
        command.CommandTimeout = TimeSpan.FromSeconds(5);
        await command.ExecuteAsync(timeout.Token);
        if (command.ExitStatus != 0) throw new InvalidOperationException("伺服器資源監控暫不可用。");
        return JsonSerializer.Deserialize<ServerResourceStats>(command.Result, JsonDefaults.Options)
            ?? throw new InvalidOperationException("伺服器資源監控返回為空。");
    }
}
