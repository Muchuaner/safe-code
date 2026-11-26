namespace SafeCode.Core;

public static class SshClientOptions
{
    // OpenSSH 的 -o 值會再次經過配置解析器；外層 shell 引號只保護 argv，
    // 含空格的路徑還需要內層雙引號，否則 UserKnownHostsFile 會被截斷。
    public static string UserKnownHostsFile(string path)=>"UserKnownHostsFile=\""+path.Replace("\"","\\\"")+"\"";
}
