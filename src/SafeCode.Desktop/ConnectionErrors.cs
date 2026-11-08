using System.Net.Sockets;
using Renci.SshNet.Common;
using SafeCode.Core;
namespace SafeCode.Desktop;
internal static class ConnectionErrors
{
    public static string Describe(Exception error)
    {
        if(error is UnsupportedOsException)return error.Message;
        if(error is SshAuthenticationException)return "服务器连接不上，请检查 IP 地址、用户名或密码。";
        if(error is SocketException or SshConnectionException or SshOperationTimeoutException or TimeoutException or OperationCanceledException)
            return "服务器连接不上，请检查 IP 地址、用户名或密码，并确认服务器已开机、SSH 端口可以访问。";
        return error.Message;
    }
}
