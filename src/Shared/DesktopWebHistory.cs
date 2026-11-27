using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace SafeCode.Desktop;

internal static class DesktopWebHistory
{
    private const string Endpoint = "/v1/desktop-history";
    private const string Followups = "/v1/desktop-followups";
    private const int ResponseLimit = 40 * 1024 * 1024;
    private sealed record Entry(string Stamp, string Cwd);
    private sealed record Pending(string Project, string Scope, string Session, string Title, long Updated, Entry Entry);

    internal static async Task SyncFollowupsAsync(HttpClient gateway, string userData, string organization,
                                                  CancellationToken ct)
    {
        var configPath = Path.Combine(userData,"config.json");
        if (!File.Exists(configPath)) return;
        using var config = JsonDocument.Parse(File.ReadAllBytes(configPath));
        if (!Guid.TryParse(Text(config.RootElement,"lastKnownAccountUuid"),out var account)) return;
        var root = Path.Combine(userData,"claude-code-sessions",account.ToString("D"),Uuid(organization));
        if (!Directory.Exists(root)) return;
        using var index = await GetAsync(gateway,Followups,optional:true,ct);
        if (index is null) return;
        var scopes = index.RootElement.GetProperty("scopes").EnumerateArray()
                          .Select(value => Uuid(value.GetString())).ToHashSet();
        var nativeProjects = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),".claude","projects");
        var records = Directory.EnumerateFiles(root,"local_*.json").Select(path =>
        {
            using var metadata = JsonDocument.Parse(File.ReadAllBytes(path));
            return (Path:path, Scope:Text(metadata.RootElement,"cliSessionId"));
        }).Where(row => row.Scope is not null && scopes.Contains(row.Scope));
        await Parallel.ForEachAsync(records, new ParallelOptions { MaxDegreeOfParallelism=4,CancellationToken=ct },
            async (row, token) =>
            {
                using var batch = await GetAsync(gateway,Followups+"/"+row.Scope,optional:true,token);
                if (batch is not null) ApplyFollowups(batch.RootElement,row.Path,nativeProjects);
            });
    }

    internal static int ApplyFollowups(JsonElement batch, string metadataPath, string nativeProjects)
    {
        var records = batch.GetProperty("messages");
        if (records.GetArrayLength() == 0) return 0;
        var earliest = batch.GetProperty("after").GetInt64();
        var metadataBytes = File.ReadAllBytes(metadataPath);
        var metadata = JsonNode.Parse(metadataBytes)!.AsObject();
        var cli = Uuid((string?)metadata["cliSessionId"]);
        var cwd = (string?)metadata["cwd"];
        if (string.IsNullOrEmpty(cwd) || !Path.IsPathFullyQualified(cwd)) return 0;
        var project = Regex.Replace(cwd,@"[^A-Za-z0-9]","-");
        var transcript = Path.Combine(nativeProjects,project,cli+".jsonl");
        if (!File.Exists(transcript) && Directory.Exists(nativeProjects))
        {
            var candidates = Directory.EnumerateDirectories(nativeProjects).Select(path => Path.Combine(path,cli+".jsonl"))
                                      .Where(File.Exists).Take(2).ToArray();
            if (candidates.Length != 1) return 0;
            transcript = candidates[0];
        }
        if (!File.Exists(transcript) || File.GetAttributes(transcript).HasFlag(FileAttributes.ReparsePoint) ||
            File.GetAttributes(Path.GetDirectoryName(transcript)!).HasFlag(FileAttributes.ReparsePoint) ||
            File.GetAttributes(metadataPath).HasFlag(FileAttributes.ReparsePoint)) return 0;
        var original = new FileInfo(transcript);
        var length = original.Length;
        var modified = original.LastWriteTimeUtc;
        if (length == 0 || length > 64 * 1024 * 1024) return 0;
        var history = File.ReadAllBytes(transcript);
        var seen = new HashSet<string>();
        var recent = new List<string>();
        string? parent = null, lastRole = null;
        foreach (var line in Encoding.UTF8.GetString(history).Split('\n'))
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            using var entry = JsonDocument.Parse(line);
            var frame = entry.RootElement;
            var id = Text(frame,"uuid");
            if (id is null || Text(frame,"sessionId") != cli) continue;
            seen.Add(id);
            var role = Text(frame,"type");
            if (role is not ("user" or "assistant")) continue;
            parent = id;
            lastRole = role;
            if (role != "assistant" || Timestamp(frame,"timestamp",0) < earliest ||
                !frame.TryGetProperty("message",out var message) ||
                !message.TryGetProperty("content",out var parts)) continue;
            if (parts.ValueKind == JsonValueKind.Array)
                recent.Add(string.Concat(parts.EnumerateArray().Select(part => Text(part,"text"))));
        }
        if (parent is null || lastRole != "assistant") return 0;

        var count = 0;
        var activity = (long?)metadata["lastActivityAt"] ?? 0;
        using var output = new MemoryStream(history.Length+4096);
        output.Write(history);
        if (history[^1] != (byte)'\n') output.WriteByte((byte)'\n');
        foreach (var record in records.EnumerateArray())
        {
            var id = Uuid(Text(record,"id"));
            var text = record.GetProperty("text").GetString() ?? "";
            if (seen.Contains(id) || text.Length == 0 || recent.Any(value => value.Contains(text,StringComparison.Ordinal)))
                continue;
            var timestamp = record.GetProperty("timestamp").GetInt64();
            var model = Text(record,"model") ?? (string?)metadata["model"] ?? "claude";
            var message = new { id="msg_"+id.Replace("-",""),type="message",role="assistant",model,
                  content=new[]{new {type="text",text="\n\n"+text}},stop_reason="end_turn",
                  usage=new {input_tokens=0,output_tokens=0} };
            JsonSerializer.Serialize(output,new { parentUuid=parent,isSidechain=false,userType="external",cwd,
                sessionId=cli,timestamp=DateTimeOffset.FromUnixTimeMilliseconds(timestamp).ToString("O"),
                type="assistant",uuid=id,message });
            output.WriteByte((byte)'\n');
            parent = id;
            activity = Math.Max(activity,timestamp);
            count++;
        }
        if (count == 0) return 0;
        original.Refresh();
        if (original.Length != length || original.LastWriteTimeUtc != modified ||
            !File.ReadAllBytes(metadataPath).AsSpan().SequenceEqual(metadataBytes))
            throw new IOException("桌面会话正在写入，已保留原记录。");
        metadata["lastAssistantUuid"] = parent;
        metadata["lastAssistantCliSessionId"] = cli;
        metadata["lastActivityAt"] = activity;
        DesktopKeyAccess.WriteProfileFile(transcript,output.ToArray());
        DesktopKeyAccess.WriteProfileFile(metadataPath,JsonSerializer.SerializeToUtf8Bytes(metadata));
        return count;
    }

    internal static async Task SyncAsync(HttpClient gateway, string userData, string dataDirectory,
                                        string organization, string fallbackModel, CancellationToken ct)
    {
        using var index = await GetAsync(gateway, Endpoint, optional:true, ct);
        if (index is null) return;
        var configPath = Path.Combine(userData, "config.json");
        if (!File.Exists(configPath)) return;
        using var config = JsonDocument.Parse(File.ReadAllBytes(configPath));
        if (!Guid.TryParse(Text(config.RootElement, "lastKnownAccountUuid"), out var account)) return;
        var root = Path.Combine(userData, "claude-code-sessions", account.ToString("D"), Uuid(organization));
        var staging = Path.Combine(root, "imported-staging");
        var manifest = Path.Combine(root, "safecode-web-history.json");
        var before = File.Exists(manifest)
            ? JsonSerializer.Deserialize<Dictionary<string, Entry>>(File.ReadAllBytes(manifest)) ?? new()
            : new Dictionary<string, Entry>();
        var enabled = index.RootElement.GetProperty("developer").GetBoolean();
        var projectManifest = Path.Combine(root, "safecode-web-projects.json");
        var oldFolders = File.Exists(projectManifest)
            ? JsonSerializer.Deserialize<string[]>(File.ReadAllBytes(projectManifest)) ?? [] : Array.Empty<string>();
        if (!enabled && before.Count == 0 && oldFolders.Length == 0) return;
        var projects = Path.Combine(dataDirectory, "CloudCLI Projects", organization);
        var folders = new HashSet<string>();
        var native = new HashSet<string>();
        var removed = new HashSet<string>();
        var next = new Dictionary<string, Entry>();
        var pending = new List<Pending>();
        if (enabled)
        {
            if (index.RootElement.TryGetProperty("removedScopes", out var stale))
                foreach (var value in stale.EnumerateArray()) removed.Add(Uuid(value.GetString()));
            foreach (var project in index.RootElement.GetProperty("projects").EnumerateArray())
            {
                var id = Uuid(Text(project, "id"));
                var name = Regex.Replace(Text(project, "name") ?? "", @"[^\p{L}\p{M}\p{N} ._()·-]", "_").Trim(' ', '.');
                var cwd = Path.Combine(projects, id, name.Length == 0 ? "CloudCLI" : name[..Math.Min(name.Length, 60)]);
                Directory.CreateDirectory(cwd);
                folders.Add(cwd);
                var directory = Text(project,"directory");
                if (directory != null && Path.IsPathFullyQualified(directory) && Directory.Exists(directory))
                    cwd = directory;
                foreach (var row in project.GetProperty("sessions").EnumerateArray())
                {
                    var count = row.GetProperty("count");
                    var scope = Uuid(Text(row, "scope"));
                    if (row.TryGetProperty("localScopes", out var localScopes))
                    {
                        foreach (var local in localScopes.EnumerateArray())
                        {
                            var localId = Uuid(local.GetString());
                            if (localId != scope && File.Exists(Path.Combine(root, "local_"+localId+".json")))
                                native.Add(scope);
                        }
                        if (native.Contains(scope)) continue;
                    }
                    var session = Uuid(Text(row, "id"));
                    var updated = Timestamp(row, "updated", 0);
                    var stamp = row.GetProperty("updated").GetRawText()+"/"+count.GetRawText()+"/"+Text(row,"title");
                    var entry = new Entry(stamp, cwd);
                    next.Add(scope, entry);
                    if (before.TryGetValue(scope, out var prior) && prior == entry &&
                        File.Exists(Path.Combine(root, "local_"+scope+".json")))
                        continue;
                    pending.Add(new(id, scope, session, Text(row, "title") ?? "CloudCLI", updated, entry));
                }
            }
        }
        Directory.CreateDirectory(staging);
        // Register ownership before writing the first visible record. If a launch
        // is interrupted, the next run can refresh or hide those exact records.
        var tracked = new Dictionary<string, Entry>(before);
        foreach (var (scope, entry) in next)
            tracked.TryAdd(scope, entry with { Stamp = "" });
        DesktopKeyAccess.WriteProfileFile(manifest, JsonSerializer.SerializeToUtf8Bytes(tracked));
        var empty = new System.Collections.Concurrent.ConcurrentBag<string>();
        await Parallel.ForEachAsync(pending, new ParallelOptions { MaxDegreeOfParallelism = 2, CancellationToken = ct },
            async (row, token) =>
            {
                using var response = await GetAsync(gateway, $"{Endpoint}/{row.Project}/{row.Session}", optional:false, token);
                if (Text(response!.RootElement, "scope") != row.Scope)
                    throw new InvalidDataException("CloudCLI 返回了错误的网页会话。");
                var messages = response.RootElement.GetProperty("messages");
                WriteSession(root, staging, row, messages, fallbackModel);
                if (messages.GetArrayLength() == 0) empty.Add(row.Scope);
            });
        // CloudCLI can publish the session to its list before its transcript is indexed.
        foreach (var scope in empty) next[scope] = next[scope] with { Stamp = "" };
        foreach (var scope in before.Keys.Concat(removed).Except(next.Keys))
        {
            Uuid(scope);
            var file = Path.Combine(root, "local_"+scope+".json");
            if (File.Exists(file))
                File.Move(file, Path.Combine(staging, "safecode-inactive-"+scope+".json"), overwrite:true);
        }
        foreach (var folder in oldFolders.Concat(before.Values.Select(entry => entry.Cwd)).Distinct().Except(folders))
        {
            var parent = Path.GetDirectoryName(folder);
            if (parent == null || Path.GetDirectoryName(parent) != projects ||
                !Guid.TryParseExact(Path.GetFileName(parent), "D", out _) || !Directory.Exists(folder) ||
                Path.GetFullPath(folder) != folder || File.GetAttributes(parent).HasFlag(FileAttributes.ReparsePoint)) continue;
            // Only remove empty directories directly created by this mirror.
            foreach (var path in new[] {folder, parent})
                if (Directory.Exists(path) && !File.GetAttributes(path).HasFlag(FileAttributes.ReparsePoint) &&
                    !Directory.EnumerateFileSystemEntries(path).Any()) Directory.Delete(path);
        }
        DesktopKeyAccess.WriteProfileFile(projectManifest, JsonSerializer.SerializeToUtf8Bytes(folders));
        DesktopKeyAccess.WriteProfileFile(manifest, JsonSerializer.SerializeToUtf8Bytes(next));
    }

    private static void WriteSession(string root, string staging, Pending row, JsonElement messages, string fallbackModel)
    {
        var sessionId = "local_"+row.Scope;
        var transcript = Path.Combine(staging, row.Scope+".jsonl");
        var model = fallbackModel;
        var created = 0L;
        var updated = row.Updated;
        string? parent = null;
        using var output = new MemoryStream();
        foreach (var message in messages.EnumerateArray())
        {
            var role = Text(message, "role");
            var text = Text(message, "content");
            object? bash = null;
            if (Text(message, "toolName") == "Bash" && message.TryGetProperty("toolInput", out var input) &&
                Text(input, "command") is string command && message.TryGetProperty("toolResult", out var toolResult) &&
                toolResult.ValueKind == JsonValueKind.Object)
            {
                var card = "srvtoolu_sc" + Guid.NewGuid().ToString("N");
                var failed = toolResult.TryGetProperty("isError", out var err) && err.ValueKind == JsonValueKind.True;
                var result = Text(toolResult, "content") ?? "";
                bash = new object[] {
                    new { type="server_tool_use", name="bash_code_execution", id=card, input=new {command} },
                    new { type="bash_code_execution_tool_result", tool_use_id=card, content=new {
                        type="bash_code_execution_result", stdout=failed ? "" : result, stderr=failed ? result : "",
                        return_code=failed ? 1 : 0, content=Array.Empty<object>() } }
                };
                role = "assistant";
            }
            if (bash is null && (role is not ("user" or "assistant") || string.IsNullOrEmpty(text)))
            {
                var tool = Text(message, "toolName");
                if (tool is null) continue;
                role = "assistant";
                text = "**"+tool+"**\n"+(Text(message,"toolResult") ??
                    (message.TryGetProperty("toolResult", out var result) ? result.GetRawText() : ""));
            }
            var time = Timestamp(message, "timestamp", updated);
            if (created == 0) created = time;
            updated = Math.Max(updated, time);
            var id = Guid.NewGuid().ToString("D");
            model = Text(message,"model") ?? model;
            object body = role == "assistant"
                ? new { role, id = "msg_"+id.Replace("-", ""), type = "message", model,
                        content = bash ?? new[]{new {type="text",text}}, stop_reason="end_turn",
                        usage = new { input_tokens=0, output_tokens=0 } }
                : new { role, content = text };
            var frame = new { parentUuid=parent, isSidechain=false, userType="external", cwd=row.Entry.Cwd,
                              sessionId=row.Scope, timestamp=DateTimeOffset.FromUnixTimeMilliseconds(time).ToString("O"),
                              type=role, uuid=id, message=body };
            JsonSerializer.Serialize(output,frame);
            output.WriteByte((byte)'\n');
            parent = id;
        }
        DesktopKeyAccess.WriteProfileFile(transcript, output.ToArray());
        DesktopKeyAccess.WriteProfileFile(Path.Combine(root, sessionId+".json"), JsonSerializer.SerializeToUtf8Bytes(new
        {
            sessionId, cliSessionId=row.Scope, cwd=row.Entry.Cwd, originCwd=row.Entry.Cwd, title=row.Title,
            createdAt=created == 0 ? updated : created, lastActivityAt=updated, indexedAt=updated, titleSetAtMs=updated,
            model, isArchived=false, permissionMode="default", importedFrom="local-1p-code", stagedTranscriptPath=transcript
        }));
    }

    internal static async Task<JsonDocument?> GetAsync(HttpClient client, string url, bool optional, CancellationToken ct)
    {
        for (var attempt = 1; ; attempt++)
        {
            try
            {
                using var request = new HttpRequestMessage(HttpMethod.Get,url);
                if (url == Endpoint) request.Headers.Add("X-SafeCode-History-Mirror","1");
                using var response = await client.SendAsync(request,HttpCompletionOption.ResponseHeadersRead,ct);
                if (optional && (response.StatusCode == HttpStatusCode.NotFound ||
                    (response.StatusCode == HttpStatusCode.Conflict && url.StartsWith(Followups+"/",StringComparison.Ordinal)))) return null;
                if (response.IsSuccessStatusCode)
                {
                    await response.Content.LoadIntoBufferAsync(ResponseLimit,ct);
                    return JsonDocument.Parse(await response.Content.ReadAsByteArrayAsync(ct));
                }
                if (attempt == 3 || response.StatusCode is not
                    (HttpStatusCode.BadGateway or HttpStatusCode.ServiceUnavailable or HttpStatusCode.GatewayTimeout))
                    throw new HttpRequestException($"CloudCLI 历史同步失败（HTTP {(int)response.StatusCode}）。", null, response.StatusCode);
            }
            catch (HttpRequestException error) when (attempt < 3 && !ct.IsCancellationRequested &&
                error.StatusCode == null && error.HttpRequestError != HttpRequestError.SecureConnectionError) { }
            catch (HttpIOException) when (attempt < 3 && !ct.IsCancellationRequested) { }
            await Task.Delay(TimeSpan.FromMilliseconds(attempt * 400),ct);
        }
    }

    private static string Uuid(string? value) => Guid.TryParseExact(value,"D",out var id)
        ? id.ToString("D") : throw new InvalidDataException("CloudCLI 返回了无效的历史标识。");

    private static string? Text(JsonElement element, string key) =>
        element.ValueKind == JsonValueKind.Object && element.TryGetProperty(key,out var value) &&
        value.ValueKind == JsonValueKind.String ? value.GetString() : null;

    private static long Timestamp(JsonElement element,string key,long fallback)
    {
        if (!element.TryGetProperty(key,out var value)) return fallback;
        if (value.ValueKind == JsonValueKind.Number && value.TryGetInt64(out var number) && number > 0 && number < 253402300799000)
            return number < 100000000000 ? number*1000 : number;
        if (value.ValueKind == JsonValueKind.String && DateTimeOffset.TryParse(value.GetString(),CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal,out var date)) return date.ToUnixTimeMilliseconds();
        return fallback;
    }
}
