namespace SafeCode.Core;

public static class SshClientOptions
{
    // OpenSSH 的 -o 值会再次经过配置解析器；外层 shell 引号只保护 argv，
    // 含空格的路径还需要内层双引号，否则 UserKnownHostsFile 会被截断。
    public static string UserKnownHostsFile(string path)=>"UserKnownHostsFile=\""+path.Replace("\"","\\\"")+"\"";
}
