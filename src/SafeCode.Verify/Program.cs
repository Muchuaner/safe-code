using SafeCode.Core;
using System.Net.Sockets;
using System.Text.Json;

if(args.Length>0&&args[0]=="geo"){
    string fixture="""{"country":"Hong Kong","countryCode":"hk","trust_score":78,"cidr":"203.0.113.0/24","geo_sources":[{"country":"Hong Kong","country_code":"hk"},{"country":"Japan","country_code":"jp","city":"Tokyo"},{"country":"Japan","country_code":"jp","city":"Tokyo"},{"country":"Japan","country_code":"jp","city":"Shinagawa"}]}""";
    var chosen=IpIntelligenceService.ParseLookup("203.0.113.9",fixture);if(chosen.RegionCode!="jp"||chosen.City!="Tokyo"||!chosen.HasGeoConflict)throw new Exception("多源多数定位失败。");
    if(args.Length>1){var live=IpIntelligenceService.ParseLookup("fixture",File.ReadAllText(args[1]));Console.WriteLine(JsonSerializer.Serialize(live,JsonDefaults.Options));if(live.RegionCode!="jp")throw new Exception("详情页综合定位未选择日本。");}
    Console.WriteLine("GEO_CONSENSUS_JAPAN_OVERRIDES_REGISTERED_LOCATION_OK");return 0;
}
if (args.Length>0 && args[0]=="webdav") { await TestWebDavAsync(); return 0; }
if (args.Length < 1) { Console.Error.WriteLine("用法：SafeCode.Verify 私密测试配置.json [inspect|deploy|check|cloud]"); return 2; }
try
{
    using var config = JsonDocument.Parse(File.ReadAllText(args[0])); var root = config.RootElement;
    string Read(string key) => root.GetProperty(key).GetString()!;
    string phase = args.Length > 1 ? args[1] : "inspect";
    using var session = new SshSession(Path.Combine(Path.GetDirectoryName(Path.GetFullPath(args[0]))!, "verify-state"));
    session.Log += line => Console.WriteLine(line);
    var settings = new ConnectionSettings(Read("host"), Read("username"), Read("password"), root.TryGetProperty("port", out var port) ? port.GetInt32() : 22);
    var server = await session.ConnectAsync(settings, key => key.Fingerprint == Read("fingerprint"));
    Console.WriteLine("SSH_CONNECTED " + server.OsName + " " + server.Architecture);
    if(phase=="localization"){
        server=await session.DeployAsync("localization",DeploymentCatalog.Locales[0]);
        if(!server.Statuses.GetValueOrDefault("localization"))throw new Exception("本土化语言包未就绪。");Console.WriteLine("LOCALE_TIMEZONE_AND_LANGUAGE_PACKS_VERIFIED");
    }
    if (phase == "deploy")
    {
        foreach (var task in DeploymentCatalog.Steps)
        {
            server = await session.DeployAsync(task.Id, DeploymentCatalog.Locales[0]);
            if (!server.Statuses.GetValueOrDefault(task.Id)) throw new Exception(task.Title + " 验证未通过。");
            Console.WriteLine("VERIFIED " + task.Id);
        }
    }
    if (phase is "check" or "deploy")
    {
        await session.DeployAsync("ssh", null);
        await session.VerifyWorkspaceKeyAsync(); Console.WriteLine("WORKSPACE_KEY_OK");
        uint local = await session.ForwardAsync(3389);
        using var tcp = new TcpClient(); await tcp.ConnectAsync("127.0.0.1", (int)local);
        byte[] request = [3,0,0,19,14,224,0,0,0,0,0,1,0,8,0,3,0,0,0];
        await tcp.GetStream().WriteAsync(request);
        byte[] response = new byte[1024]; using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(12));
        int count = await tcp.GetStream().ReadAsync(response, timeout.Token);
        if (count < 11 || response[0] != 3 || response[5] != 208) throw new Exception("RDP 协议协商验证失败。");
        Console.WriteLine("RDP_TUNNEL_NEGOTIATED " + count);
    }
    if(phase=="durable")
    {
        var job=await session.RequestAsync<RemoteJob>("claude");
        string active=await session.ExecuteTextAsync("systemctl is-active safe-code-job-"+job.JobId+".service");
        if(active.Trim()!="active")throw new Exception("任务未处于运行状态，无法验证断线恢复。");
        Console.WriteLine("DURABLE_JOB_STARTED_ACTIVE "+job.JobId);session.Dispose();
        using var resumed=new SshSession(Path.Combine(Path.GetDirectoryName(Path.GetFullPath(args[0]))!,"verify-state"));resumed.Log+=line=>Console.WriteLine(line);
        await resumed.ConnectAsync(settings,key=>key.Fingerprint==Read("fingerprint"));
        server=await resumed.ResumeJobAsync(job.JobId);
        if(!server.Statuses.GetValueOrDefault("claude"))throw new Exception("断线后任务未完成。");
        Console.WriteLine("REMOTE_JOB_SURVIVED_DISCONNECT_AND_RESUMED");
    }
    if (phase == "ip")
    {
        var policy=new IpPolicySettings(settings.Host,settings.Host,true,true,settings.Port);
        server=await session.ApplyIpPolicyAsync(policy);
        if(server.IpPolicy?.LockEgress!=true||server.IpPolicy.LockIngress!=true)throw new Exception("IP 策略提交状态错误。");
        string table=await session.ExecuteTextAsync("nft list table inet safe_code_ip");
        if(!table.Contains("egress_guard")||!table.Contains(settings.Host))throw new Exception("实际防火墙规则缺失。");
        Console.WriteLine("IP_POLICY_COMMITTED_EGRESS_AND_NEW_SSH_VERIFIED");
    }
    if(phase=="iprollback")
    {
        string before=await session.ExecuteTextAsync("nft list table inet safe_code_ip");
        var prepared=await session.RequestAsync<IpPrepareResult>("ip_prepare",new IpPolicySettings(settings.Host,settings.Host,false,false,settings.Port));
        await session.RequestAsync<JsonElement>("ip_apply",new{token=prepared.Token});
        Console.WriteLine("ROLLBACK_TIMER_ARMED "+prepared.Token);
        await Task.Delay(TimeSpan.FromSeconds(130));
        string after=await session.ExecuteTextAsync("nft list table inet safe_code_ip");
        if(!after.Contains("ip saddr != "+settings.Host+" drop")||!after.Contains("ip daddr != "+settings.Host+" tcp dport "+settings.Port+" drop"))throw new Exception("自动回退未恢复入口 / 出口阻断规则。");
        string pending=await session.ExecuteTextAsync("test ! -e /var/lib/safe-code/ip-pending-"+prepared.Token+".json && printf removed");
        if(pending!="removed")throw new Exception("自动回退记录尚未结束。");
        Console.WriteLine("AUTOMATIC_120_SECOND_ROLLBACK_VERIFIED");
    }
    if (phase == "mount")
    {
        await session.RequestAsync<ServerInfo>("mount_install");
        string localRoot=Path.Combine(Path.GetDirectoryName(Path.GetFullPath(args[0]))!,"mount-fixture");
        Directory.CreateDirectory(localRoot);Directory.CreateDirectory(Path.Combine(localRoot,"two"));
        await File.WriteAllTextAsync(Path.Combine(localRoot,"from-local.txt"),"local-to-server");
        await File.WriteAllTextAsync(Path.Combine(localRoot,"two","second.txt"),"second-mapping");
        string remoteRoot="/home/"+server.Username+"/shared-verify-"+Guid.NewGuid().ToString("N")[..10];
        string mountClientId="";
        try
        {
            var mounted=await session.ApplyMountsAsync([new(localRoot,remoteRoot+"/one"),new(Path.Combine(localRoot,"two"),remoteRoot+"/two")]);
            mountClientId=mounted.ClientId;
            string read=await session.ExecuteTextAsync("cat "+SshSession.Quote(remoteRoot+"/one/from-local.txt"));
            if(read!="local-to-server")throw new Exception("本地到服务器文件读取失败。");
            read=await session.ExecuteTextAsync("cat "+SshSession.Quote(remoteRoot+"/two/second.txt"));
            if(read!="second-mapping")throw new Exception("第二组目录映射失败。");
            await session.ExecuteTextAsync("runuser -u "+SshSession.Quote(server.Username)+" -- sh -c "+SshSession.Quote("printf server-to-local > "+SshSession.Quote(remoteRoot+"/one/from-server.txt")));
            for(int i=0;i<30&&!File.Exists(Path.Combine(localRoot,"from-server.txt"));i++)await Task.Delay(500);
            if(!File.Exists(Path.Combine(localRoot,"from-server.txt"))||await File.ReadAllTextAsync(Path.Combine(localRoot,"from-server.txt"))!="server-to-local")throw new Exception("服务器到本地文件写入失败。");
            Console.WriteLine("MOUNT_TWO_WAY_AND_MULTI_OK "+mounted.Paths.Count);
            string cloudPid=(await session.ExecuteTextAsync("systemctl show safe-code-cloudcli -p MainPID --value")).Trim();
            if(int.TryParse(cloudPid,out int cp)&&cp>0){
                await session.ExecuteTextAsync("nsenter -t "+cp+" -m -- runuser -u "+SshSession.Quote(server.Username)+" -- sh -c "+SshSession.Quote("printf cloud-namespace-write > "+SshSession.Quote(remoteRoot+"/one/from-cloud.txt")));
                for(int i=0;i<30&&!File.Exists(Path.Combine(localRoot,"from-cloud.txt"));i++)await Task.Delay(500);
                if(!File.Exists(Path.Combine(localRoot,"from-cloud.txt"))||await File.ReadAllTextAsync(Path.Combine(localRoot,"from-cloud.txt"))!="cloud-namespace-write")throw new Exception("CloudCLI 命名空间无法写入挂载目录。");
                Console.WriteLine("CLOUD_NAMESPACE_MOUNT_WRITE_OK");
            }
        }
        finally { await session.UnmountAsync(); }
        string mounts=await session.ExecuteTextAsync("findmnt -rn -t fuse.davfs -o TARGET || true");
        if(mountClientId.Length>0&&mounts.Contains(mountClientId))throw new Exception("退出后仍残留挂载。");
        Console.WriteLine("MOUNT_CLEANUP_OK");
    }
    if (phase == "cloud")
    {
        server = await session.RequestAsync<ServerInfo>("cloud");
        uint local = await session.ForwardAsync(3001);
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(20) };
        string html = await http.GetStringAsync("http://127.0.0.1:" + local);
        if (!html.Contains("<html", StringComparison.OrdinalIgnoreCase)) throw new Exception("CloudCLI 未返回网页。");
        Console.WriteLine("CLOUD_HTTP_OK " + html.Length);
    }
    Console.WriteLine(JsonSerializer.Serialize(new { success = true, server }, JsonDefaults.Options)); return 0;
}
catch (Exception error) { Console.Error.WriteLine("VERIFY_FAILED " + error.Message); return 1; }

static async Task TestWebDavAsync()
{
    string root=Path.Combine(Path.GetTempPath(),"safe-code-davtest-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(root);
    try
    {
        await File.WriteAllTextAsync(Path.Combine(root,"read.txt"),"hello");
        using var server=new WebDavServer([new(root,"/unused")]);using var http=new HttpClient();string url="http://127.0.0.1:"+server.Port+"/m0/";
        if((int)(await http.GetAsync(url+"read.txt")).StatusCode!=401)throw new Exception("缺少认证未被拦截。");
        http.DefaultRequestHeaders.Authorization=new System.Net.Http.Headers.AuthenticationHeaderValue("Basic",Convert.ToBase64String(System.Text.Encoding.UTF8.GetBytes(server.Username+":"+server.Password)));
        if(await http.GetStringAsync(url+"read.txt")!="hello")throw new Exception("WebDAV 读取错误。");
        var put=await http.PutAsync(url+"write.txt",new StringContent("world"));if(!put.IsSuccessStatusCode||await File.ReadAllTextAsync(Path.Combine(root,"write.txt"))!="world")throw new Exception("WebDAV 写入错误。");
        if((int)(await http.DeleteAsync(url)).StatusCode!=403)throw new Exception("允许删除挂载根目录。");
        if((int)(await http.GetAsync(url+"..%2Foutside.txt")).StatusCode!=403)throw new Exception("路径穿越未被拦截。");
        if(!OperatingSystem.IsWindows()){
            string outside=Path.Combine(Path.GetTempPath(),"safe-code-outside-"+Guid.NewGuid().ToString("N"));await File.WriteAllTextAsync(outside,"not-shared");File.CreateSymbolicLink(Path.Combine(root,"link"),outside);
            if((int)(await http.GetAsync(url+"link")).StatusCode!=403)throw new Exception("符号链接逃逸未被拦截。");File.Delete(outside);
        }
        Console.WriteLine("WEBDAV_AUTH_READ_WRITE_TRAVERSAL_ROOT_PROTECTION_OK");
    }
    finally{Directory.Delete(root,true);}
}
