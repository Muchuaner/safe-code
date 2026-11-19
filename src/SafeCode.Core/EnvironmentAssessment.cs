using System.Reflection;
using System.Text.Json;

namespace SafeCode.Core;

public sealed record EnvironmentSignal(string Id, string Label, string Value, double Factor, int Weight, int Contribution);
public sealed record VpsEnvironmentAssessment(int? RiskScore, string Level, string Message, EnvironmentSignal[] Signals, string Username, string RuleVersion);

public sealed partial class SshSession
{
    private readonly SemaphoreSlim environmentSampling = new(1, 1);
    private VpsEnvironmentAssessment? environmentAssessment;
    private DateTimeOffset environmentChecked;
    public async Task<VpsEnvironmentAssessment> AssessEnvironmentAsync(CancellationToken ct = default)
    {
        await environmentSampling.WaitAsync(ct);
        try
        {
            if (client?.IsConnected != true) throw new InvalidOperationException("SSH 已断开，请重新连接。");
            if (environmentAssessment != null && DateTimeOffset.UtcNow - environmentChecked < TimeSpan.FromSeconds(environmentAssessment.RiskScore.HasValue ? 60 : 10)) return environmentAssessment;
            using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("SafeCode.Core.Remote.environment_probe.py")
                ?? throw new FileNotFoundException("缺少VPS环境检测模块。");
            using var reader = new StreamReader(stream);
            string program = await reader.ReadToEndAsync(ct);
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeout.CancelAfter(TimeSpan.FromSeconds(25));
            // Separate SSH channel: don't wait behind lengthy deployment operations.
            using var command = client.CreateCommand(prefix + "python3 -c " + Quote(program) + " " + Quote(Server?.Username ?? ""));
            command.CommandTimeout = TimeSpan.FromSeconds(25);
            await command.ExecuteAsync(timeout.Token);
            if (command.ExitStatus != 0) throw new InvalidOperationException("VPS环境检测暂不可用。");
            var result = JsonSerializer.Deserialize<VpsEnvironmentAssessment>(command.Result, JsonDefaults.Options)
                ?? throw new InvalidOperationException("VPS未返回环境检测结果。");
            if (result.RiskScore.HasValue && (result.RiskScore is < 0 or > 100 || result.Signals?.Length != 6 || result.Level is not ("low" or "medium" or "high")))
                throw new InvalidOperationException("VPS环境检测结果不完整。");
            environmentAssessment = result;
            environmentChecked = DateTimeOffset.UtcNow;
            return result;
        }
        finally { environmentSampling.Release(); }
    }
}
