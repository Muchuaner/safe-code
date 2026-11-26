using System.Buffers.Binary;
using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Authentication;
using System.Security.Cryptography;
using System.Text;
using Renci.SshNet;

namespace SafeCode.Core;

public sealed record RemoteDesktopConnection(uint Port, string ProfileId, string? CertificateFingerprint);

public sealed partial class SshSession
{
    public async Task<RemoteDesktopConnection> PrepareRemoteDesktopAsync(
        string username, bool acquireCertificate, CancellationToken ct)
    {
        byte[] identity = SHA256.HashData(Encoding.UTF8.GetBytes(
            $"{Host.ToLowerInvariant()}:{Port}|{Fingerprint}|{username}"));
        uint preferredPort = 20000u + BinaryPrimitives.ReadUInt32BigEndian(identity) % 20000u;
        uint port = ForwardRemoteDesktop(preferredPort, ct);
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(TimeSpan.FromSeconds(5));
        try
        {
            using var tcp = new TcpClient();
            await tcp.ConnectAsync(IPAddress.Loopback, (int)port, deadline.Token);
            using var stream = tcp.GetStream();
            byte[] request = [3, 0, 0, 19, 14, 0xe0, 0, 0, 0, 0, 0, 1, 0, 8, 0, 1, 0, 0, 0];
            await stream.WriteAsync(request, deadline.Token);
            byte[] header = new byte[11];
            await stream.ReadExactlyAsync(header, deadline.Token);
            int size = BinaryPrimitives.ReadUInt16BigEndian(header.AsSpan(2));
            if (header[0] != 3 || header[1] != 0 || size is < 11 or > 256 || header[4] < 6 || header[5] != 0xd0)
                throw new IOException("伺服器沒有返回有效的 RDP 握手。");

            string? fingerprint = null;
            if (size > 11)
            {
                byte[] negotiation = new byte[size - 11];
                await stream.ReadExactlyAsync(negotiation, deadline.Token);
                if (negotiation[0] == 3) throw new IOException("伺服器拒絕 RDP 安全協定協商。");
                if (acquireCertificate && (negotiation.Length != 8 || negotiation[0] != 2 ||
                    BinaryPrimitives.ReadUInt32LittleEndian(negotiation.AsSpan(4)) != 1))
                    throw new IOException("伺服器未協商 RDP TLS，無法安全傳入桌面憑據。");
            }
            else if (acquireCertificate)
            {
                throw new IOException("伺服器未協商 RDP TLS，無法安全傳入桌面憑據。");
            }

            if (acquireCertificate)
            {

                // The SSH channel already authenticates the host. Obtain the exact TLS
                // certificate there without sending credentials, then pin it in the client.
                using var tls = new SslStream(stream, true, (_, cert, _, _) => cert != null);
                await tls.AuthenticateAsClientAsync(new SslClientAuthenticationOptions { TargetHost = Host }, deadline.Token);
                fingerprint = Convert.ToHexString(tls.RemoteCertificate!.GetCertHash(HashAlgorithmName.SHA256));
            }
            return new(port, Convert.ToHexString(identity.AsSpan(0, 10)).ToLowerInvariant(), fingerprint);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; }
        catch (Exception error) when (error is OperationCanceledException or SocketException or IOException or AuthenticationException)
        {
            throw new IOException("SSH 隧道已建立，但遠端桌面握手失敗。請檢查伺服器上的 xrdp 和 TLS 設定。", error);
        }
    }

    private readonly object desktopForwardGate = new();
    private ForwardedPortLocal? desktopForward;
    private SshClient? desktopForwardOwner;
    private bool desktopForwardClosed;

    // Reuse only the desktop forward owned by this authenticated SSH transport.
    private uint ForwardRemoteDesktop(uint preferredPort, CancellationToken ct)
    {
        if (preferredPort is < 1024 or > 65535) throw new ArgumentOutOfRangeException(nameof(preferredPort));
        lock (desktopForwardGate)
        {
            ct.ThrowIfCancellationRequested();
            ObjectDisposedException.ThrowIf(desktopForwardClosed, this);
            var current = client;
            if (current?.IsConnected != true || trustedKey == null) throw new InvalidOperationException("SSH 已斷開。");
            if (ReferenceEquals(current, desktopForwardOwner) && desktopForward?.IsStarted == true)
                return desktopForward.BoundPort;

            CloseDesktopForward();
            try { desktopForward = StartForward(current, preferredPort); }
            catch (SocketException error) when (error.SocketErrorCode == SocketError.AddressAlreadyInUse)
            {
                ct.ThrowIfCancellationRequested();
                desktopForward = StartForward(current, 0);
            }
            desktopForwardOwner = current;
            return desktopForward.BoundPort;
        }
    }

    private static ForwardedPortLocal StartForward(SshClient owner, uint port)
    {
        var forward = new ForwardedPortLocal("127.0.0.1", port, "127.0.0.1", 3389);
        try
        {
            owner.AddForwardedPort(forward);
            forward.Start();
            return forward;
        }
        catch
        {
            try { owner.RemoveForwardedPort(forward); } catch { }
            DisposeMountResource(forward);
            throw;
        }
    }

    private void CloseDesktopForward()
    {
        lock (desktopForwardGate)
        {
            if (desktopForward == null) return;
            try { desktopForwardOwner?.RemoveForwardedPort(desktopForward); } catch { }
            DisposeMountResource(desktopForward);
            desktopForward = null;
            desktopForwardOwner = null;
        }
    }

}
