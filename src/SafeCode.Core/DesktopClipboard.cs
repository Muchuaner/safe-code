using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Renci.SshNet;

namespace SafeCode.Core;

public sealed record DesktopImagePart(string Mime, byte[] Bytes);

public static class DesktopImageWire
{
    public const int MaxBytes = 32 * 1024 * 1024;
    public static bool IsImageType(string mime) => mime is "image/png" or "image/jpeg" or "image/gif"
        or "image/webp" or "image/bmp" or "image/tiff";

    public static async Task WriteAsync(Stream stream, IReadOnlyList<DesktopImagePart> parts, CancellationToken ct)
    {
        if (parts.Count is < 1 or > 6 || parts.Any(p => !IsImageType(p.Mime) || p.Bytes.Length == 0)
            || parts.Select(p => p.Mime).Distinct().Count() != parts.Count
            || parts.Sum(p => (long)p.Bytes.Length) > MaxBytes)
            throw new InvalidDataException("图片格式或大小超出限制（合计最多 32 MiB）。");
        var header = JsonSerializer.SerializeToUtf8Bytes(new { v = 1, formats = parts.Select(p => new { mime = p.Mime, size = p.Bytes.Length }) });
        await stream.WriteAsync(header, ct);
        await stream.WriteAsync(new byte[] { 10 }, ct);
        foreach (var part in parts) await stream.WriteAsync(part.Bytes, ct);
    }
}

public sealed partial class SshSession
{
    // Capture the same transport that owns this window's RDP tunnel. Never reconnect to a different host.
    public Func<IReadOnlyList<DesktopImagePart>, CancellationToken, Task> BindDesktopImagePaste(string user, uint port)
    {
        SshClient owner;
        if (user == "root" || !Regex.IsMatch(user, @"\A[a-z_][a-z0-9_-]{0,31}\z"))
            throw new InvalidOperationException("图片粘贴需要普通桌面工作账户。");
        lock (desktopForwardGate)
        {
            if (desktopForwardOwner is null || desktopForward?.BoundPort != port)
                throw new InvalidOperationException("当前远程桌面隧道不可用。");
            owner = desktopForwardOwner;
        }
        var privilege = prefix;
        return (parts, ct) => Task.Run(async () =>
        {
            lock (desktopForwardGate)
                if (!ReferenceEquals(owner, desktopForwardOwner) || desktopForward?.BoundPort != port
                    || desktopForward?.IsStarted != true || !owner.IsConnected)
                    throw new InvalidOperationException("当前桌面的 SSH 已断开，请重新打开远程桌面。");
            using var resource = Assembly.GetExecutingAssembly().GetManifestResourceStream("SafeCode.Core.Remote.desktop_clipboard.py")
                ?? throw new InvalidOperationException("缺少图片粘贴组件。");
            using var scriptReader = new StreamReader(resource, Encoding.UTF8);
            string script = scriptReader.ReadToEnd();
            // No shell interpolation of image data, remote file, extra port, or persistent service.
            using var command = owner.CreateCommand(privilege + "/usr/sbin/runuser -u " + Quote(user)
                + " -- /usr/bin/python3 -I -c " + Quote(script));
            using var deadline = CancellationTokenSource.CreateLinkedTokenSource(ct);
            deadline.CancelAfter(TimeSpan.FromSeconds(45));
            var token = deadline.Token;
            var execution = command.ExecuteAsync(token);
            try
            {
                using var stdout = new StreamReader(command.OutputStream, Encoding.UTF8);
                // Resolve an unambiguous logged-in XRDP desktop and its X authority before sending any pixels.
                if (await stdout.ReadLineAsync(token) != "SCIMAGE READY")
                    throw new IOException("未找到唯一可用的远程桌面会话，或缺少 GTK 3。请登录云桌面后重试。");
                using (var input = command.CreateInputStream())
                    await DesktopImageWire.WriteAsync(input, parts, token);
                var ack = await stdout.ReadLineAsync(token);
                await execution;
                if (command.ExitStatus != 0 || ack != "SCIMAGE OWNED")
                    throw new IOException("云桌面未确认接收图片，这次没有发送粘贴按键。");
            }
            finally
            {
                // Observe cancellation/failure of the command as well as the pipe. Do not leave a reader waiting.
                if (!execution.IsCompleted) deadline.Cancel();
                try { await execution; } catch when (deadline.IsCancellationRequested) { }
            }
        }, ct);
    }
}
