using Renci.SshNet;
using Renci.SshNet.Common;
using System.Diagnostics;
using System.Net.NetworkInformation;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace SafeCode.Core;

public sealed partial class SshSession : IDisposable
{
    private SshClient? client;
    private string remoteDirectory = "";
    private string prefix = "";
    private string observedEgressIp = "";
    private readonly SemaphoreSlim operations = new(1, 1);
    private readonly List<IDisposable> tunnels = [];
    private readonly string dataDirectory;
    private byte[]? trustedKey;
    private string hostKeyName = "";
    private ConnectionSettings? connection;
    public ServerInfo? Server { get; private set; }
    public string Fingerprint { get; private set; } = "";
    public string Host => connection?.Host ?? "";
    public int Port => connection?.Port ?? 22;
    public string KeyPath { get; private set; } = "";
    public string KnownHostsPath => Path.Combine(dataDirectory, "known_hosts");
    public event Action<string>? Log;
    public event Action<string>? DirectoryLog;
    public bool IsConnected => client?.IsConnected == true;

    public SshSession(string? dataDirectory = null)
    {
        this.dataDirectory = dataDirectory ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SafeCode");
        Directory.CreateDirectory(this.dataDirectory);
    }
    public async Task<ServerInfo> ConnectAsync(ConnectionSettings settings, Func<HostKeyInfo, bool> confirmHost, CancellationToken cancellationToken = default)
    {
        if (Uri.CheckHostName(settings.Host) == UriHostNameType.Unknown || settings.Port is < 1 or > 65535)
            throw new ArgumentException("服务器地址或端口无效。");
        if (!Regex.IsMatch(settings.Username, @"^[a-zA-Z_][a-zA-Z0-9_.-]{0,31}$")) throw new ArgumentException("用户名格式无效。");
        connection = settings;
        var info = new PasswordConnectionInfo(settings.Host, settings.Port, settings.Username, settings.Password) { Timeout = TimeSpan.FromSeconds(18), RetryAttempts = 2 };
        client = new SshClient(info) { KeepAliveInterval = TimeSpan.FromSeconds(20) };
        HostTrustRequiredException? trustRequired=null;
        client.HostKeyReceived += (_, e) =>
        {
            var fingerprint = "SHA256:" + Convert.ToBase64String(SHA256.HashData(e.HostKey)).TrimEnd('=');
            try{e.CanTrust=VerifyOrConfirm(settings,fingerprint,confirmHost);}
            catch(HostTrustRequiredException error){trustRequired=error;e.CanTrust=false;}
            if (e.CanTrust)
            {
                trustedKey = e.HostKey.ToArray(); Fingerprint = fingerprint;
                using var bytes = new MemoryStream(e.HostKey); using var reader = new BinaryReader(bytes);
                int length = System.Net.IPAddress.NetworkToHostOrder(reader.ReadInt32());
                hostKeyName = Encoding.ASCII.GetString(reader.ReadBytes(length));
            }
        };
        try
        {
            await client.ConnectAsync(cancellationToken);
            Log?.Invoke("SSH 认证成功，检查操作系统…");
            string osRelease;
            try{osRelease=await ExecuteTextAsync("cat /etc/os-release", cancellationToken);}
            catch{throw new UnsupportedOsException("无法安装环境：未能识别为 Ubuntu，请确认系统类型并检查连接；未写入部署文件。");}
            Log?.Invoke("操作系统信息已读取。");
            if (!ParseOsRelease(osRelease).TryGetValue("ID", out var id) || id != "ubuntu")
                throw new UnsupportedOsException("无法安装环境：本软件仅支持 Ubuntu。当前服务器未通过系统检查，未写入任何部署文件。");
            string uid = (await ExecuteTextAsync("id -u", cancellationToken)).Trim();
            if (uid != "0")
            {
                try{await ExecuteTextAsync("sudo -n true", cancellationToken);}
                catch{throw new InvalidOperationException("此账户没有免密 sudo 安装权限，请使用 root 或配置免密 sudo 后重新连接。");}
                prefix = "sudo -n ";
            }
            remoteDirectory = (await ExecuteTextAsync("mktemp -d /tmp/safe-code.XXXXXXXX", cancellationToken)).Trim();
            Log?.Invoke("准备远端部署模块…");
            using var sftp = MakeSftp(); await sftp.ConnectAsync(cancellationToken);
            using var agent = Assembly.GetExecutingAssembly().GetManifestResourceStream("SafeCode.Core.Remote.agent.py") ?? throw new FileNotFoundException("缺少 Ubuntu 部署模块。");
            await sftp.UploadFileAsync(agent, remoteDirectory + "/agent.py",cancellationToken); sftp.ChangePermissions(remoteDirectory + "/agent.py", 600);
            using var network = Assembly.GetExecutingAssembly().GetManifestResourceStream("SafeCode.Core.Remote.network.py")!;
            await sftp.UploadFileAsync(network, remoteDirectory + "/network.py",cancellationToken); sftp.ChangePermissions(remoteDirectory + "/network.py", 600);
            WriteKnownHost();
            Log?.Invoke("部署模块已就绪，检查服务器环境…");
            Server = await RequestAsync<ServerInfo>("inspect", null, cancellationToken);
            Log?.Invoke("检测服务器实际出口 IP…");
            try{observedEgressIp=(await RequestAsync<EgressProbe>("egress_probe",null,cancellationToken)).Ip;Server.ObservedEgressIp=observedEgressIp;}catch{Log?.Invoke("出口 IP 暂未确认，可在进阶设置中选择。");}
            return Server;
        }
        catch { client?.Dispose(); client = null;if(trustRequired!=null)throw trustRequired;throw; }
    }
    public static Dictionary<string,string> ParseOsRelease(string input) => input.Split('\n').Where(x => x.Contains('=')).Select(x => x.Split('=', 2)).ToDictionary(x => x[0].Trim(), x => x[1].Trim().Trim('"'));
    private bool VerifyOrConfirm(ConnectionSettings settings, string fingerprint, Func<HostKeyInfo,bool> confirm)
    {
        var store=new HostTrustStore(dataDirectory);var key=store.Describe(settings.Host,settings.Port,fingerprint);
        if(store.Matches(key))return true;
        if(!confirm(key))throw new HostTrustRequiredException(key);
        store.Accept(key);return true;
    }

    private void WriteKnownHost()
    {
        string host = Port == 22 ? Host : $"[{Host}]:{Port}";
        var lines = File.Exists(KnownHostsPath) ? File.ReadAllLines(KnownHostsPath).Where(x => !x.StartsWith(host + " ")).ToList() : [];
        lines.Add(host + " " + hostKeyName + " " + Convert.ToBase64String(trustedKey!));
        File.WriteAllLines(KnownHostsPath, lines);
    }
    private SftpClient MakeSftp()
    {
        if (connection == null) throw new InvalidOperationException("尚未连接服务器。");
        var sftp = new SftpClient(connection.Host, connection.Port, connection.Username, connection.Password);
        sftp.HostKeyReceived += (_, e) => e.CanTrust = trustedKey != null && CryptographicOperations.FixedTimeEquals(trustedKey, e.HostKey);
        return sftp;
    }
    public async Task<string> ExecuteTextAsync(string text, CancellationToken ct = default)
    {
        if (client == null) throw new InvalidOperationException("服务器未连接。");
        using var command = client.CreateCommand(text); command.CommandTimeout = TimeSpan.FromMinutes(45);
        await command.ExecuteAsync(ct);
        if (command.ExitStatus != 0) throw new InvalidOperationException((command.Error + "\n" + command.Result).Trim());
        return command.Result;
    }
    public async Task<JsonElement> ListDirectoriesAsync(string path,CancellationToken ct=default)
    {
        if(client?.IsConnected!=true)throw new InvalidOperationException("SSH 已断开，请重新连接。");
        string options=Convert.ToBase64String(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new{path},JsonDefaults.Options)));
        using var command=client.CreateCommand(prefix+"python3 -u "+Quote(remoteDirectory+"/agent.py")+" --list-directories "+Quote(options));
        command.CommandTimeout=TimeSpan.FromSeconds(10);
        var executing=command.ExecuteAsync(ct);
        var output=Task.Run(async()=>{using var reader=new StreamReader(command.OutputStream,Encoding.UTF8);string? result=null;while(await reader.ReadLineAsync(ct) is {} line){if(line.StartsWith("SC_RESULT:"))result=line[10..];else if(line.StartsWith("SC_ERROR:"))throw new InvalidOperationException(line[9..]);else DirectoryLog?.Invoke(line);}return result;},ct);
        await executing;string? json=await output;
        if(command.ExitStatus!=0||json==null)throw new InvalidOperationException("无法读取服务器目录，请重试。");
        return JsonSerializer.Deserialize<JsonElement>(json,JsonDefaults.Options);
    }
    public async Task<T> RequestAsync<T>(string operation, object? options = null, CancellationToken ct = default)
    {
        Log?.Invoke("正在等待任务通道…");
        await operations.WaitAsync(ct);
        Log?.Invoke("正在建立服务器任务通道…");
        string requestPath = remoteDirectory + "/request-" + Guid.NewGuid().ToString("N") + ".json";
        try
        {
            if (client?.IsConnected != true) throw new InvalidOperationException("SSH 连接已断开，请重新连接。");
            using (var sftp = MakeSftp())
            {
                await sftp.ConnectAsync(ct);
                using var bytes = new MemoryStream(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new { op = operation, options, background = IsLongOperation(operation) }, JsonDefaults.Options)));
                sftp.UploadFile(bytes, requestPath); sftp.ChangePermissions(requestPath, 600);
            }
            Log?.Invoke("任务请求已上传，正在启动远端操作…");
            using var command = client.CreateCommand(prefix + "python3 -u " + Quote(remoteDirectory + "/agent.py") + " " + Quote(requestPath));
            command.CommandTimeout = TimeSpan.FromMinutes(45);
            var execution = command.ExecuteAsync(ct);
            string? json = null; string? error = null;
            var stdout = Task.Run(async () =>
            {
                using var reader = new StreamReader(command.OutputStream, Encoding.UTF8, false, 4096, true);
                while (await reader.ReadLineAsync(ct) is { } line)
                {
                    if (line.StartsWith("SC_RESULT:")) json = line[10..];
                    else if (line.StartsWith("SC_ERROR:")) error = line[9..];
                    else Log?.Invoke(line);
                }
            }, ct);
            var stderr = Task.Run(async () =>
            {
                using var reader = new StreamReader(command.ExtendedOutputStream, Encoding.UTF8, false, 4096, true);
                while (await reader.ReadLineAsync(ct) is { } line) { error = line; Log?.Invoke(line); }
            }, ct);
            await execution; await Task.WhenAll(stdout, stderr);
            if (command.ExitStatus != 0 || json == null) throw new InvalidOperationException(error ?? "远端任务未返回结果，请检查连接与部署日志。");
            if(IsLongOperation(operation)&&typeof(T)!=typeof(RemoteJob))
            {
                var job=JsonSerializer.Deserialize<RemoteJob>(json,JsonDefaults.Options)??throw new InvalidDataException("缺少后台任务标识。");
                Log?.Invoke("远端任务已启动，正在读取实际安装日志…");
                json=await WatchJobJsonAsync(job.JobId,ct);
            }
            var value = JsonSerializer.Deserialize<T>(json, JsonDefaults.Options) ?? throw new InvalidDataException("服务器返回数据无效。");
            if (value is ServerInfo server)
            {
                if(server.Statuses.GetValueOrDefault("ssh") && server.Username.Length>0)
                {
                    var ownKey=CreateWorkspaceKey();
                    string check=prefix+"runuser -u "+Quote(server.Username)+" -- /usr/bin/grep -Fqx -- "+Quote(ownKey.PublicKey)+" "+Quote((server.HomeDirectory.Length>0?server.HomeDirectory:"/home/"+server.Username)+"/.ssh/authorized_keys")+" 2>/dev/null && printf yes || true";
                    server.Statuses["ssh"]=(await ExecuteTextAsync(check,ct)).Trim()=="yes";
                }
                server.ObservedEgressIp=observedEgressIp;
                Server = server;
            }
            return value;
        }
        finally
        {
            operations.Release();
            if(!ct.IsCancellationRequested)_=Task.Run(()=>{try{using var sftp=MakeSftp();sftp.ConnectionInfo.Timeout=TimeSpan.FromSeconds(3);sftp.OperationTimeout=TimeSpan.FromSeconds(2);sftp.Connect();sftp.DeleteFile(requestPath);}catch{}});
        }
    }
    private static bool IsLongOperation(string operation)=>operation is "account" or "localization" or "desktop" or "chrome" or "input" or "claude" or "ssh" or "cloud" or "mount_install";
    private async Task<string> WatchJobJsonAsync(string jobId,CancellationToken ct)
    {
        if(!Regex.IsMatch(jobId,"^[a-f0-9]{32}$"))throw new ArgumentException("后台任务标识无效。");
        long offset=0;
        while(true)
        {
            string output=await ExecuteTextAsync(prefix+"python3 -u "+Quote(remoteDirectory+"/agent.py")+" --poll "+Quote(jobId)+" "+offset,ct);
            string json=output.Split('\n').LastOrDefault(x=>x.StartsWith("SC_RESULT:"))?[10..]??throw new InvalidDataException("后台任务未返回监控结果。");
            var poll=JsonSerializer.Deserialize<JobPoll>(json,JsonDefaults.Options)??throw new InvalidDataException("任务监控数据无效。");
            foreach(string line in poll.Lines)Log?.Invoke(line);offset=poll.NextOffset;
            if(poll.Error!=null)throw new InvalidOperationException(poll.Error);
            if(poll.Done&&poll.Value is {} value)return value.GetRawText();
            await Task.Delay(500,ct);
        }
    }
    public async Task<ServerInfo> ResumeJobAsync(string jobId,CancellationToken ct=default)
    {
        await operations.WaitAsync(ct);
        try{await WatchJobJsonAsync(jobId,ct);}finally{operations.Release();}
        return await RequestAsync<ServerInfo>("inspect",null,ct);
    }
    public async Task<ServerInfo> DeployAsync(string id, LocaleSettings? locale, string username = "workspace", CancellationToken ct = default, string password = "", string hostname = "")
    {
        object options = id switch
        {
            "account" => new { username, password, hostname },
            "localization" when locale != null => new { timezone = locale.Timezone, locale = locale.Locale },
            "localization" => throw new InvalidOperationException("请先选择与出口所在地一致的时区和语言。"),
            "ssh" => new { publicKey = CreateWorkspaceKey().PublicKey },
            _ => new { }
        };
        if (id != "account" && Server?.Statuses.GetValueOrDefault("account") != true) throw new InvalidOperationException("请先创建工作用户。");
        return await RequestAsync<ServerInfo>(id, options, ct);
    }
    public (string Path, string PublicKey) CreateWorkspaceKey()
    {
        string directory = Path.Combine(dataDirectory, "keys"); Directory.CreateDirectory(directory);
        string name = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(Host + ":" + Port)))[..16].ToLowerInvariant();
        KeyPath = Path.Combine(directory, name + ".pem");
        if (!File.Exists(KeyPath))
        {
            using var rsa = RSA.Create(3072); File.WriteAllText(KeyPath, rsa.ExportRSAPrivateKeyPem());
            var parameters = rsa.ExportParameters(false);
            using var stream = new MemoryStream();
            void Put(byte[] bytes) { stream.Write(BitConverter.GetBytes(System.Net.IPAddress.HostToNetworkOrder(bytes.Length))); stream.Write(bytes); }
            static byte[] Positive(byte[] bytes) => (bytes[0] & 128) != 0 ? [0, ..bytes] : bytes;
            Put(Encoding.ASCII.GetBytes("ssh-rsa")); Put(Positive(parameters.Exponent!)); Put(Positive(parameters.Modulus!));
            File.WriteAllText(KeyPath + ".pub", "ssh-rsa " + Convert.ToBase64String(stream.ToArray()) + " safe-code-" + name);
        }
        ProtectKeyFile(KeyPath);
        return (KeyPath, File.ReadAllText(KeyPath + ".pub"));
    }
    private static void ProtectKeyFile(string path)
    {
        if (OperatingSystem.IsWindows())
        {
            var start = new ProcessStartInfo("icacls.exe") { CreateNoWindow = true, UseShellExecute = false, RedirectStandardError = true, RedirectStandardOutput = true };
            foreach (string arg in new[] { path, "/inheritance:r", "/grant:r", Environment.UserDomainName + "\\" + Environment.UserName + ":F" }) start.ArgumentList.Add(arg);
            using var process = Process.Start(start)!; process.WaitForExit();
            if (process.ExitCode != 0) throw new IOException("无法保护本地 SSH 私钥访问权限。");
        }
        else File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite);
    }
    public async Task VerifyWorkspaceKeyAsync(CancellationToken ct = default)
    {
        CreateWorkspaceKey(); var account = await RequestAsync<AccountInfo>("account_info", null, ct);
        using var work = new SshClient(Host, Port, account.Username, new PrivateKeyFile(KeyPath));
        work.HostKeyReceived += (_, e) => e.CanTrust = trustedKey != null && CryptographicOperations.FixedTimeEquals(trustedKey, e.HostKey);
        await work.ConnectAsync(ct); using var command = work.CreateCommand("id -un"); await command.ExecuteAsync(ct);
        if (command.Result.Trim() != account.Username) throw new InvalidOperationException("工作用户公钥登录验证失败。");
    }
    public async Task<uint> ForwardAsync(uint destinationPort, CancellationToken ct = default, string localHost = "127.0.0.1")
    {
        if (client?.IsConnected != true) throw new InvalidOperationException("SSH 已断开。");
        await Task.Yield();
        if(!System.Net.IPAddress.TryParse(localHost,out var localAddress)||!System.Net.IPAddress.IsLoopback(localAddress))throw new ArgumentException("本地转发必须限定为回环地址。");
        var forward = new ForwardedPortLocal(localHost, 0, "127.0.0.1", destinationPort);
        client.AddForwardedPort(forward); forward.Start(); tunnels.Add(forward); return forward.BoundPort;
    }
    public async Task<NetworkStats> MeasureNetworkAsync(CancellationToken ct = default)
    {
        ct.ThrowIfCancellationRequested();
        long? latency=null;
        try{using var ping=new Ping();var reply=await ping.SendPingAsync(Host,1500);if(reply.Status==IPStatus.Success)latency=reply.RoundtripTime;}catch{}
        networkSamples.Enqueue(latency.HasValue);while(networkSamples.Count>20)networkSamples.Dequeue();
        return new(latency,Math.Round(100.0*networkSamples.Count(ok=>!ok)/networkSamples.Count,1),networkSamples.Count);
    }
    private readonly Queue<bool> networkSamples=new();

    public static string Quote(string value) => "'" + value.Replace("'", "'\"'\"'") + "'";
    public void Dispose()
    {
        foreach (var tunnel in tunnels) { try { tunnel.Dispose(); } catch { } }
        tunnels.Clear();
        fileForward?.Dispose(); fileServer?.Dispose();
        if (client?.IsConnected == true && remoteDirectory.StartsWith("/tmp/safe-code."))
        { try { using var command = client.CreateCommand("rm -rf -- " + Quote(remoteDirectory)); command.CommandTimeout=TimeSpan.FromSeconds(2);command.Execute(); } catch { } }
        client?.Dispose(); client = null;
        connection = null;
    }
}
