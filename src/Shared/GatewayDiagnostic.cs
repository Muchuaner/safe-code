using System.Diagnostics;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace SafeCode.Desktop;

// Only this fixed set of scalar fields can cross from a VPS response to feedback.
internal sealed class GatewayDiagnostic
{
    private static readonly string[] ErrorTypes =
    [
        "invalid_request_error", "authentication_error", "billing_error", "permission_error",
        "not_found_error", "request_too_large", "rate_limit_error", "api_error", "overloaded_error"
    ];
    private static readonly Dictionary<string, string[]> Choices = new()
    {
        ["backend"] = ["cloudcli", "claude_code", "unknown"],
        ["stage"] = ["gateway_auth", "device_check", "request_decode", "gateway_validation",
            "gateway_queue", "session_route", "cloudcli_connect", "cloudcli_response", "cli_turn", "anthropic_response"],
        ["modelFamily"] = ["opus", "sonnet", "haiku", "fable", "mythos"],
        ["errorType"] = ErrorTypes,
        ["anthropicError"] = ErrorTypes,
        ["anthropicEvidence"] = ["cli_event", "cli_error_text"],
    };
    private static readonly Dictionary<string, int> Maximums = new()
    {
        ["active"] = 1000000, ["waiting"] = 1000000, ["slots"] = 1000000,
        ["gatewayMs"] = 900000, ["cloudcliHeadersMs"] = 900000,
        ["messageCount"] = 1000000, ["toolCount"] = 1000000, ["waitMs"] = 900000, ["requestBytes"] = 8388609,
        ["receivedBytes"] = 16777216, ["streamLines"] = 50001,
    };
    private const int MaxFrame = 4096;
    private readonly Stopwatch clock = Stopwatch.StartNew();
    private readonly byte[] line = new byte[MaxFrame];
    private readonly Dictionary<string, object> fields;
    private int offset;
    private bool overflow, errorEvent;
    internal bool SawError { get; private set; }
    internal bool Ended { get; private set; }

    internal GatewayDiagnostic(string endpoint)
    {
        fields = new()
        {
            ["endpoint"] = endpoint,
            ["platform"] = OperatingSystem.IsMacOS() ? "macos" : "windows",
        };
    }

    internal void Response(int status, IEnumerable<string>? headers)
    {
        fields["httpStatus"] = status;
        fields["headersMs"] = (int)Math.Min(clock.ElapsedMilliseconds, 900000);
        if (headers?.FirstOrDefault() is { Length: <= MaxFrame } value)
        {
            try { using var doc = JsonDocument.Parse(value); Accept(doc.RootElement); }
            catch (JsonException) { }
        }
    }

    internal void ObserveSse(ReadOnlySpan<byte> buffer)
    {
        while (!buffer.IsEmpty)
        {
            int newline = buffer.IndexOf((byte)'\n');
            var part = newline < 0 ? buffer : buffer[..newline];
            if (part.Length > line.Length - offset) overflow = true;
            if (!overflow)
            {
                part.CopyTo(line.AsSpan(offset));
                offset += part.Length;
            }
            if (newline < 0) return;
            if (!overflow)
                ObserveLine(line.AsSpan(0, offset));
            offset = 0;
            overflow = false;
            buffer = buffer[(newline + 1)..];
        }
    }

    private void ObserveLine(ReadOnlySpan<byte> value)
    {
        if (value.Length > 0 && value[^1] == '\r') value = value[..^1];
        if (value.SequenceEqual("event: error"u8))
        {
            SawError = true;
            errorEvent = true;
        }
        else if (value.SequenceEqual("event: message_stop"u8))
        {
            Ended = true;
        }
        else if (value.IsEmpty)
        {
            errorEvent = false;
        }
        else if (errorEvent && value.StartsWith("data: "u8))
        {
            try
            {
                using var doc = JsonDocument.Parse(value[6..].ToArray());
                if (doc.RootElement.ValueKind == JsonValueKind.Object &&
                    doc.RootElement.TryGetProperty("safeCodeDiagnostic", out var detail))
                    Accept(detail);
            }
            catch (JsonException) { }
        }
    }

    private void Accept(JsonElement detail)
    {
        if (detail.ValueKind != JsonValueKind.Object || !detail.TryGetProperty("protocol", out var protocol) ||
            protocol.ValueKind != JsonValueKind.Number || !protocol.TryGetInt32(out int version) || version != 1)
            return;
        foreach (var field in detail.EnumerateObject())
        {
            var value = field.Value;
            if (value.ValueKind == JsonValueKind.String)
            {
                string? content = value.GetString();
                if (content == null) continue;
                if (Choices.TryGetValue(field.Name, out var values) && values.Contains(content) ||
                    field.Name == "id" && Guid.TryParseExact(content, "D", out _) ||
                    field.Name == "anthropicRequestId" && Regex.IsMatch(content, @"\Areq_[A-Za-z0-9]{8,80}\z"))
                    fields[field.Name] = content;
            }
            else if (value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out var number))
            {
                if (Maximums.TryGetValue(field.Name, out int maximum) && number >= 0 && number <= maximum ||
                    field.Name is ("gatewayStatus" or "anthropicStatus" or "cloudcliStatus") && number is >= 100 and <= 599)
                    fields[field.Name] = number;
            }
        }
    }

    internal IReadOnlyDictionary<string, object> Finish(long bytes)
    {
        fields["responseBytes"] = Math.Clamp(bytes, 0, 16777216);
        fields["sseError"] = SawError;
        fields["streamComplete"] = Ended;
        return fields;
    }

    internal int? FailureStatus => fields.TryGetValue("gatewayStatus", out var value) && value is int status ? status : null;
}
