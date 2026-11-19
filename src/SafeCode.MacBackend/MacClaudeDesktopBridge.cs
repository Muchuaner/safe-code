using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;
using SafeCode.Core;

namespace SafeCode.Desktop;

internal sealed class MacClaudeDesktopBridge(string dataDirectory, bool offline, string? profileDirectory = null) : IDisposable
{
    private sealed record ProfileState(GatewayPreference Server, string Id, string Token, int Port);
    private readonly SemaphoreSlim gate = new(1);
    private readonly string statePath = Path.Combine(dataDirectory, "desktop-ssh.aes");
    private readonly string library = profileDirectory ?? DesktopKeyAccess.ProfileLibrary();
    private volatile DesktopVpsGateway? gateway;
    private DateTimeOffset? started;
    private string? message;
    private DateTimeOffset nextHealthCheck;

    public bool Configured => File.Exists(statePath);
    public bool Running => gateway != null;
    private static bool Installed => File.Exists("/Applications/Claude.app/Contents/Info.plist")
        || File.Exists(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Applications/Claude.app/Contents/Info.plist"));

    private ProfileState ReadState()
    {
        byte[] data = LocalSecrets.Unprotect(File.ReadAllBytes(statePath));
        try
        {
            var state = JsonSerializer.Deserialize<ProfileState>(data);
            if (state == null || !Guid.TryParseExact(state.Id, "D", out _) || state.Port is < 1024 or > 65535)
                throw new IOException("保存的 SSH 接入配置无效。");
            return state;
        }
        finally { CryptographicOperations.ZeroMemory(data); }
    }

    public async Task<object> RefreshAsync(CancellationToken ct)
    {
        await gate.WaitAsync(ct);
        try
        {
            if (gateway != null)
            {
                bool applied;
                try { applied = IsApplied(ReadState()); }
                catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException or CryptographicException)
                {
                    applied = false;
                }
                if (!applied)
                {
                    await SuspendCoreAsync(ct);
                    message = "Claude Desktop 配置已切换或无法读取，SSH 网关已关闭。";
                }
                else if (DateTimeOffset.UtcNow >= nextHealthCheck)
                {
                    message = await gateway.RefreshAsync(ct) ? null : "SSH 网关暂不可用，请检查 VPS 连接。";
                    nextHealthCheck = DateTimeOffset.UtcNow.AddSeconds(4);
                }
            }
            return Status();
        }
        finally { gate.Release(); }
    }

    private object Status()
    {
        bool active = gateway?.Connected == true;
        bool managed = Configured;
        return new
        {
            active, managed, configured = managed, installed = !offline && Installed,
            state = active ? "active" : managed ? "attention" : "unconfigured",
            message = message ?? (active ? "SSH 网关已连接，重启 Claude Desktop 后可使用。" : managed
                ? "已保存 SSH 接入。连接原服务器后可恢复。" : "尚未启用 SSH 接入。"),
            endpoint = gateway?.Endpoint ?? "",
            startedAt = started,
            usage = gateway?.Usage ?? IntegrationUsage.Unavailable("尚未连接 VPS 网关"),
            usageSource = "VPS 原版 Claude Code assistant usage"
        };
    }

    public async Task<object> StartAsync(SshSession target, CancellationToken ct)
    {
        await gate.WaitAsync(ct);
        try
        {
            _ = LocalSecrets.RequireKey();
            if (gateway?.Connected == true) return Status();
            if (gateway != null) await SuspendCoreAsync(ct);
            if (Configured)
            {
                var state = ReadState();
                if (!state.Server.Matches(target))
                    throw new InvalidOperationException("保存的 SSH 接入绑定到原服务器。请连接原服务器，或先停用后重新设置。");
                if (!IsApplied(state)) throw new IOException("Claude Desktop 已切换配置。请先停用旧接入后重新设置。");
                var saved = new DesktopVpsGateway(target, Guid.Parse(state.Id).ToString("N"), state.Token, (uint)state.Port);
                try { await saved.StartAsync(ct); }
                catch { saved.Dispose(); throw; }
                gateway = saved;
            }
            else
            {
                if (!Installed) throw new InvalidOperationException("请先安装官方 Claude Desktop。");
                EnsureProfileSlotAvailable();
                var id = Guid.NewGuid();
                var pending = new DesktopVpsGateway(target, id.ToString("N"));
                try
                {
                    await pending.StartAsync(ct);
                    var models = await pending.ModelsAsync(ct);
                    int port = new Uri(pending.Endpoint).Port;
                    var state = new ProfileState(GatewayPreference.For(target), id.ToString("D"), pending.LocalToken, port);
                    var profile = MakeProfile(pending.Endpoint, state.Token, models);
                    Activate(state, profile);
                    gateway = pending;
                }
                catch
                {
                    using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(15));
                    await pending.StopAsync(timeout.Token);
                    pending.Dispose();
                    throw;
                }
            }
            started = DateTimeOffset.UtcNow;
            nextHealthCheck = started.Value.AddSeconds(4);
            message = null;
            return Status();
        }
        finally { gate.Release(); }
    }

    public async Task<string?> ResumeAsync(SshSession target, CancellationToken ct)
    {
        if (!Configured || !LocalSecrets.IsUnlocked) return null;
        try
        {
            if (ReadState().Server.Matches(target)) await StartAsync(target, ct);
            return null;
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; }
        catch (Exception error) { return "Claude SSH 网关恢复失败：" + error.Message; }
    }

    public async Task<object> StopAsync(CancellationToken ct, bool suspend = false)
    {
        await gate.WaitAsync(ct);
        try
        {
            var state = !suspend && Configured ? ReadState() : null;
            await SuspendCoreAsync(ct);
            if (state != null) RemoveProfile(state);
            return Status();
        }
        finally { gate.Release(); }
    }

    private async Task SuspendCoreAsync(CancellationToken ct)
    {
        if (gateway == null) return;
        var current = gateway;
        gateway = null;
        try { message = await current.StopAsync(ct); }
        finally { current.Dispose(); started = null; }
    }

    private string MetaPath => Path.Combine(library, "_meta.json");
    private string ProfilePath(ProfileState state) => Path.Combine(library, state.Id + ".json");

    private JsonObject EnsureProfileSlotAvailable()
    {
        var meta = DesktopKeyAccess.ReadProfileMeta(library);
        if (!string.IsNullOrEmpty((string?)meta["appliedId"]) || meta["hybridPointer"] != null)
            throw new InvalidOperationException("Claude Desktop 已启用其他配置，请先在 Claude Desktop 中停用该配置。");
        return meta;
    }

    private static bool OwnsProfile(ProfileState state, JsonNode? profile) =>
        (string?)profile?["inferenceGatewayBaseUrl"] == "http://127.0.0.1:" + state.Port &&
        (string?)profile?["inferenceGatewayApiKey"] == state.Token;

    private bool IsApplied(ProfileState state)
    {
        string path = ProfilePath(state);
        return (string?)DesktopKeyAccess.ReadProfileMeta(library)["appliedId"] == state.Id
            && File.Exists(path) && OwnsProfile(state, JsonNode.Parse(File.ReadAllBytes(path)));
    }

    private void Activate(ProfileState state, JsonObject profile)
    {
        var meta = EnsureProfileSlotAvailable();
        ((JsonArray)meta["entries"]!).Add(new JsonObject { ["id"] = state.Id, ["name"] = "SafeCode SSH" });
        meta["appliedId"] = state.Id;
        byte[] data = JsonSerializer.SerializeToUtf8Bytes(state);
        try { DesktopKeyAccess.WriteProfileFile(statePath, LocalSecrets.Protect(data)); }
        finally { CryptographicOperations.ZeroMemory(data); }
        try
        {
            DesktopKeyAccess.WriteProfileFile(ProfilePath(state), JsonSerializer.SerializeToUtf8Bytes(profile));
            DesktopKeyAccess.WriteProfileFile(MetaPath, JsonSerializer.SerializeToUtf8Bytes(meta));
        }
        catch
        {
            RemoveProfile(state);
            throw;
        }
    }

    private void RemoveProfile(ProfileState state)
    {
        var meta = DesktopKeyAccess.ReadProfileMeta(library);
        var entries = (JsonArray)meta["entries"]!;
        string path = ProfilePath(state);
        // Leave an externally edited profile and its metadata under the user's control.
        if (!File.Exists(path) || OwnsProfile(state, JsonNode.Parse(File.ReadAllBytes(path))))
        {
            var entry = entries.FirstOrDefault(node => (string?)node?["id"] == state.Id);
            if (entry != null) entries.Remove(entry);
            bool applied = (string?)meta["appliedId"] == state.Id;
            if (applied) meta["appliedId"] = "";
            if (entry != null || applied) DesktopKeyAccess.WriteProfileFile(MetaPath, JsonSerializer.SerializeToUtf8Bytes(meta));
            File.Delete(path);
        }
        File.Delete(statePath);
    }

    internal static JsonObject MakeProfile(string endpoint, string token, DesktopGatewayModel[] models)
    {
        var entries = new JsonArray();
        foreach (var model in models.Where(model => !model.AutoModeClassifier).OrderByDescending(model => model.Family == "opus"))
        {
            var entry = new JsonObject
            {
                ["name"] = model.Id, ["labelOverride"] = model.DisplayName,
                ["maxEffort"] = model.SupportedEffortLevels.LastOrDefault() ?? "low",
                ["supports1m"] = model.Supports1m
            };
            if (!string.IsNullOrEmpty(model.Family))
            {
                entry["anthropicFamilyTier"] = model.Family;
                entry["isFamilyDefault"] = true;
            }
            entries.Add(entry);
        }
        if (entries.Count == 0) throw new IOException("VPS 原版 Claude Code 未返回可用聊天模型。");
        var profile = new JsonObject
        {
            ["inferenceProvider"] = "gateway", ["inferenceGatewayBaseUrl"] = endpoint,
            ["inferenceGatewayAuthScheme"] = "bearer", ["inferenceCredentialKind"] = "static",
            ["inferenceGatewayApiKey"] = token, ["inferenceModels"] = entries,
            ["deploymentMode"] = "3p", ["disableDeploymentModeChooser"] = true,
            ["deploymentDisplayName"] = "SafeCode", ["modelDiscoveryEnabled"] = false,
            ["autoModeEnabled"] = true, ["chatTabEnabled"] = true
        };
        if (models.FirstOrDefault(model => model.AutoModeClassifier) is {} classifier)
            DesktopKeyAccess.SetClassifier(profile, classifier.Id);
        return profile;
    }

    public void Dispose() => gateway?.Dispose();
}
