using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace SafeCode.Core;

public sealed record PublicGatewayTicket(string Url, string Pin, string Key)
{
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);
    // Shared with the gateway. Bundled keys deter casual inspection only.
    private static readonly byte[] TicketEncryptionKey = Convert.FromHexString("f04223626f9a64f32aa618705067a6a04a5d852f8e5b551c944d0b9cd2d7f54d");
    private static readonly byte[] TicketAuthKey = Convert.FromHexString("bf8145e375d11db0fdd2fb0d9734fdda504c5a480fc2eae4a567c1948aa86155");

    public static PublicGatewayTicket Parse(string text)
    {
        try
        {
            bool encrypted = text.StartsWith("sc2.", StringComparison.Ordinal);
            if (text.Length > 2048 || (!encrypted && !text.StartsWith("sc1.", StringComparison.Ordinal)))
                throw new FormatException();
            string payload = text[4..].Replace('-', '+').Replace('_', '/');
            byte[] decoded = Convert.FromBase64String(payload.PadRight((payload.Length + 3) / 4 * 4, '='));
            var ticket = JsonSerializer.Deserialize<PublicGatewayTicket>(encrypted ? DecryptTicket(decoded) : decoded, Json);
            if (ticket is null || !Uri.TryCreate(ticket.Url, UriKind.Absolute, out var endpoint)
                || endpoint.Scheme != Uri.UriSchemeHttps || endpoint.UserInfo != "" || endpoint.PathAndQuery != "/" || endpoint.Fragment != ""
                || ticket.Key is null || !Regex.IsMatch(ticket.Key, @"\A[A-Za-z0-9_-]{43}\z")
                || ticket.Pin is null || !Regex.IsMatch(ticket.Pin, @"\A[0-9a-f]{64}\z"))
                throw new FormatException();
            return ticket;
        }
        catch (Exception error) when (error is ArgumentException or FormatException or JsonException or CryptographicException)
        {
            throw new InvalidDataException("请输入完整的 SafeCode 接入 Key。");
        }
    }

    private static byte[] DecryptTicket(ReadOnlySpan<byte> encoded)
    {
        const int ivSize = 16;
        const int tagSize = 32;
        const int versionSize = 4;
        if (encoded.Length < ivSize * 2 + tagSize || (encoded.Length - tagSize) % ivSize != 0)
            throw new FormatException();
        var body = encoded[..^tagSize];
        Span<byte> authenticated = stackalloc byte[versionSize + body.Length];
        "sc2."u8.CopyTo(authenticated);
        body.CopyTo(authenticated[versionSize..]);
        Span<byte> tag = stackalloc byte[tagSize];
        HMACSHA256.HashData(TicketAuthKey, authenticated, tag);
        if (!CryptographicOperations.FixedTimeEquals(tag, encoded[^tagSize..]))
            throw new FormatException();
        using var aes = Aes.Create();
        aes.Key = TicketEncryptionKey;
        return aes.DecryptCbc(body[ivSize..], body[..ivSize]);
    }

    internal string Encode()
    {
        var iv = RandomNumberGenerator.GetBytes(16);
        using var aes = Aes.Create();
        aes.Key = TicketEncryptionKey;
        var ciphertext = aes.EncryptCbc(JsonSerializer.SerializeToUtf8Bytes(this, Json), iv);
        var authenticated = new byte[4 + iv.Length + ciphertext.Length];
        "sc2."u8.CopyTo(authenticated);
        iv.CopyTo(authenticated, 4);
        ciphertext.CopyTo(authenticated, 4 + iv.Length);
        var encoded = new byte[iv.Length + ciphertext.Length + 32];
        authenticated.AsSpan(4).CopyTo(encoded);
        HMACSHA256.HashData(TicketAuthKey, authenticated, encoded.AsSpan(encoded.Length - 32));
        return "sc2." + Convert.ToBase64String(encoded).TrimEnd('=').Replace('+', '-').Replace('/', '_');
    }
}
