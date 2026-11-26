using System.Net.Sockets;
using Renci.SshNet.Common;
using SafeCode.Core;
namespace SafeCode.Desktop;
internal static class ConnectionErrors
{
    public static bool CanRetry(Exception error) =>
        error is (SocketException or SshConnectionException or SshOperationTimeoutException or TimeoutException or OperationCanceledException)
        && !error.Message.Contains("Host key could not be verified", StringComparison.OrdinalIgnoreCase);

    public static string DescribeOperation(Exception error)=>error.Message.Contains("Host key could not be verified",StringComparison.OrdinalIgnoreCase)?"伺服器主機密鑰校驗失敗。請返回登入並重新連線；若提示指紋變化，請先在 VPS 控制台核對，確認後再繼續。":error.Message;
    public static string Describe(Exception error)
    {
        if(error.Message.Contains("Host key could not be verified",StringComparison.OrdinalIgnoreCase))return DescribeOperation(error);
        if(error is UnsupportedOsException)return error.Message;
        if(error is SshAuthenticationException)return "SSH 登入認證失敗，請核對用戶名和該帳戶當前的伺服器密碼。若剛修改過 root 密碼，請使用新密碼；工作用戶密碼與 root 密碼不同。";
        if(error is SocketException or SshConnectionException or SshOperationTimeoutException or TimeoutException or OperationCanceledException)
            return "伺服器連線不上，請檢查 IP 地址、用戶名或密碼，並確認伺服器已開機、SSH 連接埠可以訪問。";
        return error.Message;
    }
}
