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
    private static readonly TimeSpan SshConnectTimeout=TimeSpan.FromSeconds(18);
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
    public string LoginUsername => connection?.Username ?? "";
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
            throw new ArgumentException("伺服器地址或連接埠無效。");
        if (!Regex.IsMatch(settings.Username, @"^[a-zA-Z_][a-zA-Z0-9_.-]{0,31}$")) throw new ArgumentException("用戶名格式無效。");
        connection = settings;
        var info = new PasswordConnectionInfo(settings.Host, settings.Port, settings.Username, settings.Password) { Timeout = SshConnectTimeout, RetryAttempts = 2 };
        client = new MountSshClient(info) { KeepAliveInterval = TimeSpan.FromSeconds(20) };
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
            Log?.Invoke("SSH 認證成功，檢查作業系統…");
            string osRelease;
            try{osRelease=await ExecuteTextAsync("cat /etc/os-release", cancellationToken);}
            catch(InvalidOperationException){throw new UnsupportedOsException("無法安裝環境：未能識別為 Ubuntu，請確認系統類型並檢查連線；未寫入部署檔案。");}
            Log?.Invoke("作業系統資訊已讀取。");
            if (!ParseOsRelease(osRelease).TryGetValue("ID", out var id) || id != "ubuntu")
                throw new UnsupportedOsException("無法安裝環境：本軟件僅支援 Ubuntu。當前伺服器未通過系統檢查，未寫入任何部署檔案。");
            string uid = (await ExecuteTextAsync("id -u", cancellationToken)).Trim();
            if (uid != "0")
            {
                try{await ExecuteTextAsync("sudo -n true", cancellationToken);}
                catch(InvalidOperationException){throw new InvalidOperationException("此帳戶沒有免密 sudo 安裝權限，請使用 root 或配置免密 sudo 後重新連線。");}
                prefix = "sudo -n ";
            }
            remoteDirectory = (await ExecuteTextAsync("mktemp -d /tmp/safe-code.XXXXXXXX", cancellationToken)).Trim();
            Log?.Invoke("準備遠端部署模組…");
            using var sftp = MakeSftp(); await sftp.ConnectAsync(cancellationToken);
            await UploadManagementModulesAsync(sftp, remoteDirectory, cancellationToken);
            WriteKnownHost();
            Log?.Invoke("部署模組已就緒，檢查伺服器環境…");
            Server = await RequestAsync<ServerInfo>("inspect", null, cancellationToken);
            Log?.Invoke("檢測伺服器實際出口 IP…");
            try{observedEgressIp=(await RequestAsync<EgressProbe>("egress_probe",null,cancellationToken)).Ip;Server.ObservedEgressIp=observedEgressIp;}catch{Log?.Invoke("出口 IP 暫未確認，可在進階設定中選擇。");}
            return Server;
        }
        catch { DisposeMountResource(client); client = null;if(trustRequired!=null)throw trustRequired;throw; }
    }
    public static Dictionary<string,string> ParseOsRelease(string input) => input.Split('\n').Where(x => x.Contains('=')).Select(x => x.Split('=', 2)).ToDictionary(x => x[0].Trim(), x => x[1].Trim().Trim('"'));

    private static async Task UploadManagementModulesAsync(SftpClient sftp, string directory, CancellationToken ct)
    {
        foreach (var name in new[] { "agent.py", "network.py", "socks_egress.py" })
        {
            using var resource = Assembly.GetExecutingAssembly().GetManifestResourceStream("SafeCode.Core.Remote." + name)
                ?? throw new FileNotFoundException("缺少 Ubuntu 部署模組。", name);
            string path = directory + "/" + name;
            await sftp.UploadFileAsync(resource, path, ct);
            sftp.ChangePermissions(path, 600);
        }
        using var usage = Assembly.GetExecutingAssembly().GetManifestResourceStream("SafeCode.Core.PublicApi.claude_usage.py")!;
        await sftp.UploadFileAsync(usage, directory + "/claude_usage.py", ct);
        sftp.ChangePermissions(directory + "/claude_usage.py", 600);
    }

    public Task<SocksEgressProbe> ProbeSocksEgressAsync(string proxyUrl, CancellationToken ct = default) =>
        RequestAsync<SocksEgressProbe>("socks_probe", new { proxyUrl }, ct);

    public async Task<ServerInfo> SetSocksEgressAsync(string? proxyUrl, CancellationToken ct = default)
    {
        var server = Server ?? throw new InvalidOperationException("尚未連接伺服器。");
        var status = proxyUrl == null
            ? await RequestAsync<SocksEgressStatus>("socks_disable", null, ct)
            : await RequestAsync<SocksEgressStatus>("socks_enable", new { proxyUrl }, ct);
        server.SocksEgress = status;
        observedEgressIp = status.ObservedIp ?? "";
        if (!status.Enabled)
        {
            try { observedEgressIp = (await RequestAsync<EgressProbe>("egress_probe", null, ct)).Ip; }
            catch (RemoteOperationException) { Log?.Invoke("出口 IP 暫未確認。"); }
        }
        server.ObservedEgressIp = observedEgressIp;
        return server;
    }
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
    private SftpClient MakeSftp(ConnectionSettings? settings = null)
    {
        settings ??= connection ?? throw new InvalidOperationException("尚未連接伺服器。");
        var sftp = new SftpClient(settings.Host, settings.Port, settings.Username, settings.Password);
        PinHostKeyAlgorithms(sftp.ConnectionInfo);
        sftp.HostKeyReceived += (_, e) => e.CanTrust = trustedKey != null && CryptographicOperations.FixedTimeEquals(trustedKey, e.HostKey);
        return sftp;
    }
    public async Task<string> ExecuteTextAsync(string text, CancellationToken ct = default)
    {
        if (client == null) throw new InvalidOperationException("伺服器未連線。");
        using var command = client.CreateCommand(text); command.CommandTimeout = TimeSpan.FromMinutes(45);
        await command.ExecuteAsync(ct);
        if (command.ExitStatus != 0) throw new InvalidOperationException((command.Error + "\n" + command.Result).Trim());
        return command.Result;
    }
    public async Task<JsonElement> ListDirectoriesAsync(string path,CancellationToken ct=default)
    {
        if(client?.IsConnected!=true)throw new InvalidOperationException("SSH 已斷開，請重新連線。");
        string options=Convert.ToBase64String(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new{path},JsonDefaults.Options)));
        using var command=client.CreateCommand(prefix+"python3 -u "+Quote(remoteDirectory+"/agent.py")+" --list-directories "+Quote(options));
        command.CommandTimeout=TimeSpan.FromSeconds(10);
        var executing=command.ExecuteAsync(ct);
        var output=Task.Run(async()=>{using var reader=new StreamReader(command.OutputStream,Encoding.UTF8);string? result=null;while(await reader.ReadLineAsync(ct) is {} line){if(line.StartsWith("SC_RESULT:"))result=line[10..];else if(line.StartsWith("SC_ERROR:"))throw new InvalidOperationException(line[9..]);else DirectoryLog?.Invoke(line);}return result;},ct);
        await executing;string? json=await output;
        if(command.ExitStatus!=0||json==null)throw new InvalidOperationException("無法讀取伺服器目錄，請重試。");
        return JsonSerializer.Deserialize<JsonElement>(json,JsonDefaults.Options);
    }
    public Task<JsonElement> CreateDirectoryAsync(string path,string name,CancellationToken ct=default)=>RequestAsync<JsonElement>("create_directory",new{path,name},ct);

    public async Task RequirePublicGatewayAsync(CancellationToken ct)
    {
        var status = await ReadPublicGatewayAsync(false, ct);
        if (!status.TryGetProperty("enabled", out var enabled) || enabled.ValueKind != JsonValueKind.True)
            throw new InvalidOperationException("請先在 Safe Code 的公網網關設定中點擊「啟用」。");
    }

    public async Task<T> RequestAsync<T>(string operation, object? options = null, CancellationToken ct = default)
    {
        Log?.Invoke("正在等待任務通道…");
        await operations.WaitAsync(ct);
        Log?.Invoke("正在建立伺服器任務通道…");
        string requestPath = remoteDirectory + "/request-" + Guid.NewGuid().ToString("N") + ".json";
        try
        {
            if (client?.IsConnected != true) throw new InvalidOperationException("SSH 連線已斷開，請重新連線。");
            using (var sftp = MakeSftp())
            {
                if (operation is "safe_code_api" or "safe_code_api_repair") sftp.OperationTimeout = TimeSpan.FromSeconds(12);
                await sftp.ConnectAsync(ct);
                using var bytes = new MemoryStream(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new { op = operation, options, background = IsLongOperation(operation) }, JsonDefaults.Options)));
                await sftp.UploadFileAsync(bytes, requestPath, ct); sftp.ChangePermissions(requestPath, 600);
            }
            Log?.Invoke("任務請求已上載，正在啟動遠端操作…");
            using var command = client.CreateCommand(prefix + "python3 -u " + Quote(remoteDirectory + "/agent.py") + " " + Quote(requestPath));
            command.CommandTimeout = TimeSpan.FromMinutes(45);
            var execution = command.ExecuteAsync(ct);
            string? json = null; string? error = null;string? structuredError=null;string? remoteErrorCode=null;
            var stdout = Task.Run(async () =>
            {
                using var reader = new StreamReader(command.OutputStream, Encoding.UTF8, false, 4096, true);
                while (await reader.ReadLineAsync(ct) is { } line)
                {
                    if (line.StartsWith("SC_RESULT:")) json = line[10..];
                    else if (line.StartsWith("SC_ERROR_CODE:")) remoteErrorCode=line[14..];
                    else if (line.StartsWith("SC_ERROR_JSON:")) structuredError=JsonSerializer.Deserialize<string>(line[14..]);
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
            if (command.ExitStatus != 0 || json == null) throw new RemoteOperationException(structuredError ?? error ?? "遠端任務未返回結果，請檢查連線與部署日誌。",remoteErrorCode);
            if(IsLongOperation(operation)&&typeof(T)!=typeof(RemoteJob))
            {
                var job=JsonSerializer.Deserialize<RemoteJob>(json,JsonDefaults.Options)??throw new InvalidDataException("缺少背景任務標識。");
                Log?.Invoke("遠端任務已啟動，正在讀取實際安裝日誌…");
                json=await WatchJobJsonAsync(job.JobId,ct);
            }
            var value = JsonSerializer.Deserialize<T>(json, JsonDefaults.Options) ?? throw new InvalidDataException("伺服器返回數據無效。");
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
    private static bool IsLongOperation(string operation)=>operation is "account" or "localization" or "desktop" or "chrome" or "input" or "claude" or "ssh" or "cloud" or "mount_install" or "safe_code_api_repair";
    private async Task<string> WatchJobJsonAsync(string jobId,CancellationToken ct)
    {
        if(!Regex.IsMatch(jobId,"^[a-f0-9]{32}$"))throw new ArgumentException("背景任務標識無效。");
        long offset=0;
        while(true)
        {
            string output=await ExecuteTextAsync(prefix+"python3 -u "+Quote(remoteDirectory+"/agent.py")+" --poll "+Quote(jobId)+" "+offset,ct);
            string json=output.Split('\n').LastOrDefault(x=>x.StartsWith("SC_RESULT:"))?[10..]??throw new InvalidDataException("背景任務未返回監控結果。");
            var poll=JsonSerializer.Deserialize<JobPoll>(json,JsonDefaults.Options)??throw new InvalidDataException("任務監控數據無效。");
            foreach(string line in poll.Lines)Log?.Invoke(line);offset=poll.NextOffset;
            if(poll.Error!=null)throw new RemoteOperationException(poll.Error,poll.ErrorCode);
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
            "localization" => throw new InvalidOperationException("請先手動選擇時區和系統語言。"),
            "ssh" => new { publicKey = CreateWorkspaceKey().PublicKey },
            _ => new { }
        };
        if (id != "account" && Server?.Statuses.GetValueOrDefault("account") != true) throw new InvalidOperationException("請先建立工作用戶。");
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
            if (process.ExitCode != 0) throw new IOException("無法保護本地 SSH 私鑰訪問權限。");
        }
        else File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite);
    }
    public async Task VerifyWorkspaceKeyAsync(CancellationToken ct = default)
    {
        CreateWorkspaceKey(); var account = await RequestAsync<AccountInfo>("account_info", null, ct);
        using var work = new SshClient(Host, Port, account.Username, new PrivateKeyFile(KeyPath));
        PinHostKeyAlgorithms(work.ConnectionInfo);
        work.HostKeyReceived += (_, e) => e.CanTrust = trustedKey != null && CryptographicOperations.FixedTimeEquals(trustedKey, e.HostKey);
        await work.ConnectAsync(ct); using var command = work.CreateCommand("id -un"); await command.ExecuteAsync(ct);
        if (command.Result.Trim() != account.Username) throw new InvalidOperationException("工作用戶公鑰登入驗證失敗。");
    }
    public async Task<uint> ForwardAsync(uint destinationPort, CancellationToken ct = default, string localHost = "127.0.0.1")
    {
        if (client?.IsConnected != true) throw new InvalidOperationException("SSH 已斷開。");
        await Task.Yield();
        if(!System.Net.IPAddress.TryParse(localHost,out var localAddress)||!System.Net.IPAddress.IsLoopback(localAddress))throw new ArgumentException("本地轉發必須限定為迴環地址。");
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
        lock (desktopForwardGate)
        {
            desktopForwardClosed = true;
            CloseDesktopForward();
        }
        hostLifetime?.Cancel();CloseHostTunnel();hostLifetime?.Dispose();hostLifetime=null;
        foreach (var tunnel in tunnels) { try { tunnel.Dispose(); } catch { } }
        tunnels.Clear();
        mountLifetime.Cancel();DisposeMountResource(fileForward);DisposeMountResource(mountTransport);DisposeMountResource(fileServer);
        if (client?.IsConnected == true && remoteDirectory.StartsWith("/tmp/safe-code."))
        { try { using var command = client.CreateCommand("rm -rf -- " + Quote(remoteDirectory)); command.CommandTimeout=TimeSpan.FromSeconds(2);command.Execute(); } catch { } }
        DisposeMountResource(client); client = null;
        connection = null;
    }
}
