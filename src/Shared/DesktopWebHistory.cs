using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace SafeCode.Desktop;

internal static class DesktopWebHistory
{
    private const string Endpoint = "/v1/desktop-history";
    private const string Followups = "/v1/desktop-followups";
    private const string MirrorFormat = "text-auto-v1/";
    private const int ResponseLimit = 40 * 1024 * 1024;
    private sealed record Entry(string Stamp, string Cwd);
    private sealed record Pending(string Project, string Scope, string Session, string Title, long Updated, Entry Entry);

    internal static async Task SyncFollowupsAsync(HttpClient gateway, string userData, string? organization,
                                                  CancellationToken ct)
    {
        var configPath = Path.Combine(userData,"config.json");
        if (!File.Exists(configPath)) return;
        using var config = JsonDocument.Parse(File.ReadAllBytes(configPath));
        if (!Guid.TryParse(Text(config.RootElement,"lastKnownAccountUuid"),out var account)) return;
        var accountRoot = Path.Combine(userData,"claude-code-sessions",account.ToString("D"));
        if (!Directory.Exists(accountRoot)) return;
        var roots = organization != null
            ? new[]{Path.Combine(accountRoot, Uuid(organization))}
            : Directory.EnumerateDirectories(accountRoot).Where(path => Guid.TryParse(Path.GetFileName(path),out _)).Take(2).ToArray();
        if (roots.Length != 1) return;
        var root = roots[0];
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
        var hasResume = batch.TryGetProperty("resume",out var resume) && resume.ValueKind == JsonValueKind.Object;
        if (records.GetArrayLength() == 0 && !hasResume) return 0;
        var earliest = batch.TryGetProperty("after",out var after) ? after.GetInt64() : 0;
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
        string? parent = null, lastRole = null, lastUserText = null;
        long lastUserAt = 0;
        int assistantCount = 0, lastAssistantLine = -1;
        var lines = Encoding.UTF8.GetString(history).Split('\n');
        for (int index = 0; index < lines.Length; index++)
        {
            var line = lines[index];
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
            if (role == "user")
            {
                lastUserText = frame.TryGetProperty("message",out var user) &&
                    user.ValueKind == JsonValueKind.Object && user.TryGetProperty("content",out var userContent)
                    ? UserPrompt(userContent) : null;
                lastUserAt = Timestamp(frame,"timestamp",0);
                assistantCount = 0;
            }
            else
            {
                assistantCount++;
                lastAssistantLine = index;
            }
            if (role != "assistant" || Timestamp(frame,"timestamp",0) < earliest ||
                !frame.TryGetProperty("message",out var message) ||
                !message.TryGetProperty("content",out var parts)) continue;
            if (parts.ValueKind == JsonValueKind.Array)
                recent.Add(string.Concat(parts.EnumerateArray().Select(part => Text(part,"text"))));
        }
        if (parent is null) return 0;
        var matches = hasResume && MatchesRecoveredTurn(resume,lastUserText,lastUserAt);
        var recover = lastRole == "user" && matches;
        if (!recover && lastRole != "assistant") return 0;

        var activity = (long?)metadata["lastActivityAt"] ?? 0;
        var count = 0;
        if (matches && lastRole == "assistant" && assistantCount == 1 &&
            FinishPartialTurn(lines[lastAssistantLine], Text(resume,"text") ?? "") is {} completed)
        {
            lines[lastAssistantLine] = completed;
            history = Encoding.UTF8.GetBytes(string.Join("\n",lines));
            recent.Add(resume.GetProperty("text").GetString()!);
            activity = Math.Max(activity,Timestamp(resume,"timestamp",0));
            count++;
        }
        using var output = new MemoryStream(history.Length+4096);
        output.Write(history);
        if (history[^1] != (byte)'\n') output.WriteByte((byte)'\n');
        var pending = recover ? new[]{resume}.Concat(records.EnumerateArray()) : records.EnumerateArray().AsEnumerable();
        foreach (var record in pending)
        {
            var id = Uuid(Text(record,"id"));
            var text = record.GetProperty("text").GetString() ?? "";
            if (seen.Contains(id) || text.Length == 0 || recent.Any(value => value.Contains(text,StringComparison.Ordinal)))
                continue;
            var timestamp = record.GetProperty("timestamp").GetInt64();
            var model = Text(record,"model") ?? (string?)metadata["model"] ?? "claude";
            var body = recover && count == 0 ? text : "\n\n"+text;
            if (recover && count == 0) recent.Add(text);
            var message = new { id="msg_"+id.Replace("-",""),type="message",role="assistant",model,
                  content=new[]{new {type="text",text=body}},stop_reason="end_turn",
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

    private static string? UserPrompt(JsonElement content)
    {
        if (content.ValueKind == JsonValueKind.String) return content.GetString();
        if (content.ValueKind != JsonValueKind.Array) return null;
        var parts = content.EnumerateArray().ToArray();
        if (parts.Any(part => part.ValueKind != JsonValueKind.Object || Text(part,"type") == "tool_result"))
            return null;
        return string.Join("\n",parts.Where(part => Text(part,"type") == "text").Select(part => Text(part,"text")));
    }

    private static bool MatchesRecoveredTurn(JsonElement resume, string? prompt, long userAt)
    {
        var started = Timestamp(resume,"started",0);
        // Require the same visible prompt and a nearby timestamp in the same native session.
        if (prompt == null || userAt <= 0 || started <= 0 || userAt < started-300_000 || userAt > started+30_000)
            return false;
        var clean = Regex.Replace(prompt,@"<system-reminder>.*?</system-reminder>","",RegexOptions.Singleline).Trim();
        return clean.Length > 0 && Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(clean))) == Text(resume,"prompt");
    }

    private static string? FinishPartialTurn(string line, string reply)
    {
        var frame = JsonNode.Parse(line)!.AsObject();
        if (frame["isSidechain"]?.GetValue<bool>() == true || frame["message"] is not JsonObject message ||
            message["content"] is not JsonArray parts || parts.Any(part => part is not JsonObject obj ||
                (string?)obj["type"] is not ("text" or "thinking" or "redacted_thinking")))
            return null;
        var textParts = parts.OfType<JsonObject>().Where(part => (string?)part["type"] == "text").ToArray();
        var partial = string.Concat(textParts.Select(part => (string?)part["text"]));
        if (partial.Length == 0 || partial.Length >= reply.Length || !reply.StartsWith(partial,StringComparison.Ordinal))
            return null;
        textParts[^1]["text"] = (string?)textParts[^1]["text"] + reply[partial.Length..];
        message["stop_reason"] = "end_turn";
        return frame.ToJsonString();
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
                    var entry = new Entry(MirrorFormat+stamp, cwd);
                    next.Add(scope, entry);
                    var metadata = Path.Combine(root, "local_"+scope+".json");
                    if (before.TryGetValue(scope, out var prior) && File.Exists(metadata))
                    {
                        if (!prior.Stamp.StartsWith(MirrorFormat,StringComparison.Ordinal))
                            MigrateMirror(metadata, staging, scope);
                        else
                            SetMirrorAuto(metadata);
                        if (prior == entry || prior == new Entry(stamp,cwd))
                            continue;
                    }
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

    private static void SetMirrorAuto(string metadataPath)
    {
        var metadata = JsonNode.Parse(File.ReadAllBytes(metadataPath))!.AsObject();
        if ((string?)metadata["permissionMode"] == "auto") return;
        metadata["permissionMode"] = "auto";
        DesktopKeyAccess.WriteProfileFile(metadataPath,JsonSerializer.SerializeToUtf8Bytes(metadata));
    }

    // Full sync runs before Claude Desktop starts. Only migrate scopes owned by this mirror,
    // and preserve the IDs of every retained frame, including any newer local turns.
    internal static void MigrateMirror(string metadataPath, string staging, string scope, string? nativeProjects = null)
    {
        var metadata = JsonNode.Parse(File.ReadAllBytes(metadataPath))!.AsObject();
        var staged = Path.Combine(staging,scope+".jsonl");
        var nativeRoot = nativeProjects ?? Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),".claude","projects");
        var cwd = (string?)metadata["cwd"];
        Dictionary<string,string?> removed = new(StringComparer.Ordinal);
        if (File.Exists(staged)) StripMirroredTools(staged,scope,removed);
        if (cwd is not null && Path.IsPathFullyQualified(cwd))
        {
            var project = Regex.Replace(cwd,@"[^A-Za-z0-9]","-");
            var native = Path.Combine(nativeRoot,project,scope+".jsonl");
            if (File.Exists(native)) StripMirroredTools(native,scope,removed);
        }
        var lastAssistant = (string?)metadata["lastAssistantUuid"];
        var retained = RetainedParent(lastAssistant,removed);
        if ((string?)metadata["permissionMode"] != "auto" || retained != lastAssistant)
        {
            metadata["permissionMode"] = "auto";
            if (retained != lastAssistant)
            {
                if (retained == null) metadata.Remove("lastAssistantUuid");
                else metadata["lastAssistantUuid"] = retained;
            }
            DesktopKeyAccess.WriteProfileFile(metadataPath,JsonSerializer.SerializeToUtf8Bytes(metadata));
        }
    }

    private static string? RetainedParent(string? id, Dictionary<string,string?> removed)
    {
        while (id != null && removed.TryGetValue(id,out var parent))
            id = parent;
        return id;
    }

    private static void StripMirroredTools(string path, string scope, Dictionary<string,string?> removed)
    {
        var file = new FileInfo(path);
        if (file.Length == 0 || file.Length > 64*1024*1024 || file.Attributes.HasFlag(FileAttributes.ReparsePoint) ||
            file.Directory!.Attributes.HasFlag(FileAttributes.ReparsePoint)) return;
        var changed = false;
        using var output = new MemoryStream();
        foreach (var line in File.ReadLines(path))
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            var frame = JsonNode.Parse(line)!.AsObject();
            var parent = (string?)frame["parentUuid"];
            var retained = RetainedParent(parent,removed);
            if ((string?)frame["sessionId"] == scope && LegacyMirroredTool(frame) && (string?)frame["uuid"] is string id &&
                id != retained)
            {
                removed[id] = retained;
                changed = true;
                continue;
            }
            if (retained != parent)
            {
                frame["parentUuid"] = retained;
                JsonSerializer.Serialize(output,frame);
                changed = true;
            }
            else
                output.Write(Encoding.UTF8.GetBytes(line));
            output.WriteByte((byte)'\n');
        }
        if (changed) DesktopKeyAccess.WriteProfileFile(path,output.ToArray());
    }

    private static bool LegacyMirroredTool(JsonObject frame)
    {
        var id = (string?)frame["uuid"];
        if ((string?)frame["type"] != "assistant" || (string?)frame["userType"] != "external" ||
            id == null || frame["message"] is not JsonObject message ||
            (string?)message["id"] != "msg_"+id.Replace("-","") ||
            (string?)message["stop_reason"] != "end_turn" ||
            message["usage"] is not JsonObject usage || usage.Count != 2 ||
            (long?)usage["input_tokens"] != 0 || (long?)usage["output_tokens"] != 0 ||
            message["content"] is not JsonArray parts) return false;
        if (parts.Count == 2 && parts[0] is JsonObject call && parts[1] is JsonObject result &&
            (string?)call["type"] == "server_tool_use" && (string?)call["name"] == "bash_code_execution" &&
            (string?)result["type"] == "bash_code_execution_tool_result" &&
            (string?)call["id"] is string callId && callId.StartsWith("srvtoolu_sc",StringComparison.Ordinal) &&
            (string?)result["tool_use_id"] == callId) return true;
        if (parts.Count != 1 || parts[0] is not JsonObject part || (string?)part["type"] != "text" ||
            (string?)part["text"] is not string text) return false;
        var header = Regex.Match(text,@"\A\*\*[a-zA-Z0-9_]+\*\*\n(?=\{)");
        if (!header.Success) return false;
        try
        {
            return JsonNode.Parse(text[header.Length..]) is JsonObject tool &&
                   (tool.ContainsKey("isError") || tool.ContainsKey("toolUseResult"));
        }
        catch (JsonException) { return false; }
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
            var role = Text(message,"role");
            var text = Text(message,"content");
            var kind = Text(message,"kind");
            if (role is not ("user" or "assistant") || string.IsNullOrEmpty(text) ||
                kind is not (null or "text") || Text(message,"toolName") is not null ||
                Text(message,"parentToolUseId") is not null ||
                role == "user" && text.TrimStart().StartsWith("<task-notification>",StringComparison.Ordinal))
                continue;
            var time = Timestamp(message, "timestamp", updated);
            if (created == 0) created = time;
            updated = Math.Max(updated, time);
            var id = Guid.NewGuid().ToString("D");
            model = Text(message,"model") ?? model;
            object body = role == "assistant"
                ? new { role, id = "msg_"+id.Replace("-", ""), type = "message", model,
                        content = new[]{new {type="text",text}}, stop_reason="end_turn",
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
            model, isArchived=false, permissionMode="auto", importedFrom="local-1p-code", stagedTranscriptPath=transcript
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
