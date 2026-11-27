using System.Net.Sockets;
using Renci.SshNet.Common;
using SafeCode.Core;
namespace SafeCode.Desktop;
internal static class ConnectionErrors
{
    public static bool CanRetry(Exception error) =>
        error is (SocketException or SshConnectionException or SshOperationTimeoutException or TimeoutException or OperationCanceledException or ProxyException)
        && !error.Message.Contains("Host key could not be verified", StringComparison.OrdinalIgnoreCase);

    public static string DescribeOperation(Exception error)=>error.Message.Contains("Host key could not be verified",StringComparison.OrdinalIgnoreCase)?"服务器主机密钥校验失败。请返回登录并重新连接；若提示指纹变化，请先在 VPS 控制台核对，确认后再继续。":error.Message;
    public static string Describe(Exception error)
    {
        if(error.Message.Contains("Host key could not be verified",StringComparison.OrdinalIgnoreCase))return DescribeOperation(error);
        if(error is UnsupportedOsException)return error.Message;
        if(error is SshAuthenticationException)return "SSH 登录认证失败，请核对用户名和该账户当前的服务器密码。若刚修改过 root 密码，请使用新密码；工作用户密码与 root 密码不同。";
        if(error is SocketException or SshConnectionException or SshOperationTimeoutException or TimeoutException or OperationCanceledException or ProxyException)
            return "SSH 连接未建立。请确认 VPS 已开机，IP 和 SSH 端口正确，且服务商入站规则允许访问。";
        return error.Message;
    }
}
