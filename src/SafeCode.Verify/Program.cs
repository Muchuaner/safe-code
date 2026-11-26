using SafeCode.Core;
using System.Net.Sockets;
using System.Text.Json;

if(args.Length>1&&args[0]=="algorithm-fixture"){
    using var doc=JsonDocument.Parse(File.ReadAllText(args[1]));var c=doc.RootElement;
    string directory=c.GetProperty("directory").GetString()!,mode=c.GetProperty("mode").GetString()!;
    var settings=new ConnectionSettings("127.0.0.1","fixture","fixture-only",c.GetProperty("port").GetInt32());
    using var initial=new Renci.SshNet.SshClient(settings.Host,settings.Port,settings.Username,settings.Password);
    byte[]? key=null;initial.HostKeyReceived+=(_,e)=>{key=e.HostKey.ToArray();e.CanTrust=true;};await initial.ConnectAsync(default);
    using var session=new SshSession(directory);var flags=System.Reflection.BindingFlags.NonPublic|System.Reflection.BindingFlags.Instance;
    typeof(SshSession).GetField("trustedKey",flags)!.SetValue(session,key);
    typeof(SshSession).GetField("connection",flags)!.SetValue(session,settings);
    File.WriteAllText(mode,"expanded");
    async Task Check(bool changed){
        using var sftp=(Renci.SshNet.SftpClient)typeof(SshSession).GetMethod("MakeSftp",flags)!.Invoke(session,new object?[]{null})!;
        using var tunnel=(Renci.SshNet.SshClient)typeof(SshSession).GetMethod("NewMountConnection",flags)!.Invoke(session,new object?[]{null})!;
        foreach(var connection in new Renci.SshNet.BaseClient[]{sftp,tunnel}){
            bool rejected=false;try{await connection.ConnectAsync(default);}catch(Renci.SshNet.Common.SshConnectionException){rejected=true;}
            if(rejected!=changed)throw new Exception(changed?"替換密鑰未被拒絕。":"伺服器新增密鑰類型導致已信任連線失敗。");
        }
    }
    await Check(false);
    typeof(SshSession).GetField("client",flags)!.SetValue(session,initial);
    typeof(SshSession).GetField("remoteDirectory",flags)!.SetValue(session,"/tmp/fixture");
    var deployed=await session.RequestAsync<ServerInfo>("claude");
    if(!deployed.Statuses.GetValueOrDefault("claude"))throw new Exception("VPS 背景安裝任務沒有返回就緒狀態。");
    bool remoteFailure=false;
    try{await session.RequestAsync<ServerInfo>("claude");}
    catch(RemoteOperationException error){remoteFailure=error.Code=="dns_failure"&&error.Message=="VPS 下載失敗";}
    if(!remoteFailure)throw new Exception("VPS 下載失敗未原樣傳回。");
    Console.WriteLine("CLAUDE_VPS_JOB_WITHOUT_CLIENT_PAYLOAD_PASSED");
    File.WriteAllText(mode,"changed");await Check(true);
    Console.WriteLine("ADDED_HOST_KEY_TYPE_PRESERVES_SFTP_AND_TUNNEL; REPLACED_KEY_REJECTED");return 0;
}
if(args.Length>1&&args[0]=="ssh-known-hosts-option"){
    Console.WriteLine(SshClientOptions.UserKnownHostsFile(args[1]));return 0;
}
if(args.Length>1&&args[0]=="ip-rollback-fixture"){
    using var doc=JsonDocument.Parse(File.ReadAllText(args[1]));var c=doc.RootElement;string directory=c.GetProperty("directory").GetString()!;int port=c.GetProperty("port").GetInt32();
    using var client=new Renci.SshNet.SshClient("127.0.0.1",port,"fixture","fixture-only");client.HostKeyReceived+=(_,e)=>e.CanTrust=Convert.ToBase64String(e.HostKey)==c.GetProperty("key").GetString();await client.ConnectAsync(default);
    using var session=new SshSession(Path.Combine(directory,"settings")){IpVerificationTimeout=TimeSpan.FromSeconds(1),IpRollbackTimeout=TimeSpan.FromSeconds(1),IpSessionRollbackTimeout=TimeSpan.FromSeconds(5)};
    var flags=System.Reflection.BindingFlags.NonPublic|System.Reflection.BindingFlags.Instance;
    void Set(string name,object value)=>typeof(SshSession).GetField(name,flags)!.SetValue(session,value);
    Set("client",client);Set("connection",new ConnectionSettings("127.0.0.1","fixture","fixture-only",port));Set("trustedKey",Convert.FromBase64String(c.GetProperty("key").GetString()!));Set("remoteDirectory",directory);
    typeof(SshSession).GetProperty("Server")!.SetValue(session,new ServerInfo{Architecture="x86_64"});
    bool rolledBack=false;
    try{await session.ApplyIpPolicyAsync(new IpPolicySettings("127.0.0.1","203.0.113.1",true,true,port));}
    catch(InvalidOperationException error)when(error.InnerException is TimeoutException&&error.Message.Contains("已執行回退")){rolledBack=true;}
    if(!rolledBack)throw new Exception("新連線不可達時未透過現有控制通道執行回退。");
    Console.WriteLine("IP_ROLLBACK_USES_ESTABLISHED_CONTROL_CHANNEL");return 0;
}
if(args.Length>1&&args[0]=="mount-fixture"){
    using var doc=JsonDocument.Parse(File.ReadAllText(args[1]));var c=doc.RootElement;string directory=c.GetProperty("directory").GetString()!;int port=c.GetProperty("port").GetInt32();
    using var client=new Renci.SshNet.SshClient("127.0.0.1",port,"fixture","fixture-only");client.HostKeyReceived+=(_,e)=>e.CanTrust=Convert.ToBase64String(e.HostKey)==c.GetProperty("key").GetString();await client.ConnectAsync(default);
    using var session=new SshSession(Path.Combine(directory,"settings")){MountGuardInterval=TimeSpan.FromHours(1)};var flags=System.Reflection.BindingFlags.NonPublic|System.Reflection.BindingFlags.Instance;
    void Set(string name,object value)=>typeof(SshSession).GetField(name,flags)!.SetValue(session,value);
    Set("client",client);Set("connection",new ConnectionSettings("127.0.0.1","fixture","fixture-only",port));Set("trustedKey",Convert.FromBase64String(c.GetProperty("key").GetString()!));Set("remoteDirectory",directory);
    typeof(SshSession).GetProperty("Server")!.SetValue(session,new ServerInfo{MountReady=true});
    Directory.CreateDirectory(Path.Combine(directory,"local"));var mappings=new List<MountMapping>{new(Path.Combine(directory,"local"),"/home/fixture/shared")};
    var first=await session.ApplyMountsAsync(mappings);var second=await session.ApplyMountsAsync(mappings);
    if(first.ClientId.Length!=20||second.ClientId!=first.ClientId||!session.HasMounts)throw new Exception("重掛載客戶端標識丟失。");
    if(session.ReadMountMappings().Count!=1)throw new Exception("掛載設定未儲存。");
    var oldChannel=typeof(SshSession).GetField("fileServer",flags)!.GetValue(session);bool refused=false;
    try{await session.ApplyMountsAsync([new(mappings[0].Local,"/home/fixture/nonempty")]);}catch(InvalidOperationException e){refused=e.Message.Contains("不是空目錄");}
    if(!refused||!ReferenceEquals(oldChannel,typeof(SshSession).GetField("fileServer",flags)!.GetValue(session))||session.ReadMountMappings()[0].Remote!=mappings[0].Remote)throw new Exception("無效目錄導致舊檔案通道或設定丟失。");
    await session.ApplyMountsAsync([]);if(session.HasMounts||session.ReadMountMappings().Count!=0)throw new Exception("清空掛載失敗。");
    var third=await session.ApplyMountsAsync(mappings);if(third.ClientId!=first.ClientId)throw new Exception("重新掛載身份改變。");await session.UnmountAsync();
    bool completeError=false;try{await session.RequestAsync<JsonElement>("fixture_error");}catch(InvalidOperationException e){completeError=e.Message=="掛載失敗\n第二行原因\n最後一行";}
    if(!completeError)throw new Exception("多行遠端錯誤被截斷。");
    Console.WriteLine("REMOUNT_ID_INVALID_TARGET_PRESERVES_CHANNEL_SETTINGS_CLEAR_REAPPLY_AND_MULTILINE_ERROR_PASSED");return 0;
}
if(args.Length>1&&args[0]=="directory-fixture"){
    using var doc=JsonDocument.Parse(File.ReadAllText(args[1]));var c=doc.RootElement;string fp=c.GetProperty("fingerprint").GetString()!;
    using var client=new Renci.SshNet.SshClient("127.0.0.1",c.GetProperty("port").GetInt32(),"fixture","fixture-only");client.HostKeyReceived+=(_,e)=>e.CanTrust="SHA256:"+Convert.ToBase64String(System.Security.Cryptography.SHA256.HashData(e.HostKey)).TrimEnd('=')==fp;await client.ConnectAsync(default);
    using var session=new SshSession(c.GetProperty("directory").GetString()!);var flags=System.Reflection.BindingFlags.NonPublic|System.Reflection.BindingFlags.Instance;typeof(SshSession).GetField("client",flags)!.SetValue(session,client);
    var gate=(SemaphoreSlim)typeof(SshSession).GetField("operations",flags)!.GetValue(session)!;await gate.WaitAsync();
    try{using var timeout=new CancellationTokenSource(TimeSpan.FromSeconds(3));var dirs=await session.ListDirectoriesAsync("/normal",timeout.Token);if(dirs.GetProperty("path").GetString()!="/normal")throw new Exception("directory response incorrect");}finally{gate.Release();}
    bool cancelled=false;using(var timeout=new CancellationTokenSource(200)){try{await session.ListDirectoriesAsync("/slow",timeout.Token);}catch(OperationCanceledException){cancelled=true;}}
    if(!cancelled)throw new Exception("directory cancellation failed");Console.WriteLine("DIRECTORY_READ_BYPASSES_DEPLOYMENT_QUEUE_AND_CANCELS_PASSED");return 0;
}
if(args.Length>1&&args[0]=="trust-fixture"){
    using var doc=JsonDocument.Parse(File.ReadAllText(args[1]));var c=doc.RootElement;string Read(string k)=>c.GetProperty(k).GetString()!;
    string directory=Read("directory");var store=new HostTrustStore(directory);var settings=new ConnectionSettings("127.0.0.1","fixture","fixture-only",c.GetProperty("port").GetInt32());
    store.Accept(new("other.example",22,"SHA256:other"));
    async Task<HostKeyInfo> NeedTrust(){using var s=new SshSession(directory);try{await s.ConnectAsync(settings,_=>false);throw new Exception("unconfirmed key accepted");}catch(HostTrustRequiredException e){return e.Key;}}
    async Task Approved(){using var s=new SshSession(directory);try{await s.ConnectAsync(settings,_=>throw new Exception("approved key prompted again"));throw new Exception("fixture did not reject Debian");}catch(UnsupportedOsException){}}
    var first=await NeedTrust();if(first.PreviousFingerprint!=null||first.Fingerprint!=Read("first"))throw new Exception("first key mismatch");store.Accept(first);await Approved();
    File.WriteAllText(Read("mode"),"second");var changed=await NeedTrust();if(changed.PreviousFingerprint!=first.Fingerprint||changed.Fingerprint!=Read("second"))throw new Exception("changed key not identified");
    if(store.Describe(settings.Host,settings.Port,first.Fingerprint).PreviousFingerprint!=first.Fingerprint)throw new Exception("decline changed stored trust");
    string host=$"[127.0.0.1]:{settings.Port}";File.WriteAllText(Path.Combine(directory,"known_hosts"),host+" ssh-rsa old-key\nother.example ssh-rsa keep-key\n");store.Accept(changed);await Approved();
    if(!File.ReadAllText(Path.Combine(directory,"known_hosts")).Contains("other.example")||File.ReadAllText(Path.Combine(directory,"known_hosts")).Contains("old-key"))throw new Exception("known_hosts isolation failed");
    bool stale=false;try{store.Accept(first);}catch(InvalidOperationException){stale=true;}if(!stale)throw new Exception("stale challenge accepted");
    if(store.Describe("other.example",22,"SHA256:other").PreviousFingerprint!="SHA256:other")throw new Exception("other host pin modified");
    Console.WriteLine("FIRST_TRUST_REINSTALL_CONFIRM_DECLINE_CAS_AND_ENDPOINT_ISOLATION_PASSED");return 0;
}
if(args.Length>0&&args[0]=="geo"){
    string fixture="""{"country":"Hong Kong","countryCode":"hk","trust_score":78,"cidr":"203.0.113.0/24","geo_sources":[{"country":"Hong Kong","country_code":"hk"},{"country":"Japan","country_code":"jp","city":"Tokyo"},{"country":"Japan","country_code":"jp","city":"Tokyo"},{"country":"Japan","country_code":"jp","city":"Shinagawa"}]}""";
    var chosen=IpIntelligenceService.ParseLookup("203.0.113.9",fixture);if(chosen.RegionCode!="jp"||chosen.City!="Tokyo"||!chosen.HasGeoConflict)throw new Exception("多源多數定位失敗。");
    if(args.Length>1){var live=IpIntelligenceService.ParseLookup("fixture",File.ReadAllText(args[1]));Console.WriteLine(JsonSerializer.Serialize(live,JsonDefaults.Options));if(live.RegionCode!="jp")throw new Exception("詳情頁綜合定位未選擇日本。");}
    Console.WriteLine("GEO_CONSENSUS_JAPAN_OVERRIDES_REGISTERED_LOCATION_OK");return 0;
}
if (args.Length>0 && args[0]=="webdav") { await TestWebDavAsync(); return 0; }
if (args.Length < 1) { Console.Error.WriteLine("用法：SafeCode.Verify 私密測試配置.json [inspect|deploy|check|cloud]"); return 2; }
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
        if(!server.Statuses.GetValueOrDefault("localization"))throw new Exception("本土化語言包未就緒。");Console.WriteLine("LOCALE_TIMEZONE_AND_LANGUAGE_PACKS_VERIFIED");
    }
    if (phase == "deploy")
    {
        foreach (var task in DeploymentCatalog.Steps)
        {
            server = await session.DeployAsync(task.Id, DeploymentCatalog.Locales[0]);
            if (!server.Statuses.GetValueOrDefault(task.Id)) throw new Exception(task.Title + " 驗證未通過。");
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
        if (count < 11 || response[0] != 3 || response[5] != 208) throw new Exception("RDP 協定協商驗證失敗。");
        Console.WriteLine("RDP_TUNNEL_NEGOTIATED " + count);
    }
    if(phase=="durable")
    {
        var job=await session.RequestAsync<RemoteJob>("claude");
        string active=await session.ExecuteTextAsync("systemctl is-active safe-code-job-"+job.JobId+".service");
        if(active.Trim()!="active")throw new Exception("任務未處於執行狀態，無法驗證斷線恢復。");
        Console.WriteLine("DURABLE_JOB_STARTED_ACTIVE "+job.JobId);session.Dispose();
        using var resumed=new SshSession(Path.Combine(Path.GetDirectoryName(Path.GetFullPath(args[0]))!,"verify-state"));resumed.Log+=line=>Console.WriteLine(line);
        await resumed.ConnectAsync(settings,key=>key.Fingerprint==Read("fingerprint"));
        server=await resumed.ResumeJobAsync(job.JobId);
        if(!server.Statuses.GetValueOrDefault("claude"))throw new Exception("斷線後任務未完成。");
        Console.WriteLine("REMOTE_JOB_SURVIVED_DISCONNECT_AND_RESUMED");
    }
    if (phase == "ip")
    {
        var policy=new IpPolicySettings(settings.Host,settings.Host,true,true,settings.Port);
        server=await session.ApplyIpPolicyAsync(policy);
        if(server.IpPolicy?.LockEgress!=true||server.IpPolicy.LockIngress!=true)throw new Exception("IP 策略提交狀態錯誤。");
        string table=await session.ExecuteTextAsync("nft list table inet safe_code_ip");
        if(!table.Contains("egress_guard")||!table.Contains(settings.Host))throw new Exception("實際防火牆規則缺失。");
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
        if(!after.Contains("ip saddr != "+settings.Host+" drop")||!after.Contains("ip daddr != "+settings.Host+" tcp dport "+settings.Port+" drop"))throw new Exception("自動回退未恢復入口 / 出口阻斷規則。");
        string pending=await session.ExecuteTextAsync("test ! -e /var/lib/safe-code/ip-pending-"+prepared.Token+".json && printf removed");
        if(pending!="removed")throw new Exception("自動回退記錄尚未結束。");
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
            if(read!="local-to-server")throw new Exception("本地到伺服器檔案讀取失敗。");
            read=await session.ExecuteTextAsync("cat "+SshSession.Quote(remoteRoot+"/two/second.txt"));
            if(read!="second-mapping")throw new Exception("第二組目錄映射失敗。");
            await session.ExecuteTextAsync("runuser -u "+SshSession.Quote(server.Username)+" -- sh -c "+SshSession.Quote("printf server-to-local > "+SshSession.Quote(remoteRoot+"/one/from-server.txt")));
            for(int i=0;i<30&&!File.Exists(Path.Combine(localRoot,"from-server.txt"));i++)await Task.Delay(500);
            if(!File.Exists(Path.Combine(localRoot,"from-server.txt"))||await File.ReadAllTextAsync(Path.Combine(localRoot,"from-server.txt"))!="server-to-local")throw new Exception("伺服器到本地檔案寫入失敗。");
            Console.WriteLine("MOUNT_TWO_WAY_AND_MULTI_OK "+mounted.Paths.Count);
            string cloudPid=(await session.ExecuteTextAsync("systemctl show safe-code-cloudcli -p MainPID --value")).Trim();
            if(int.TryParse(cloudPid,out int cp)&&cp>0){
                await session.ExecuteTextAsync("nsenter -t "+cp+" -m -- runuser -u "+SshSession.Quote(server.Username)+" -- sh -c "+SshSession.Quote("printf cloud-namespace-write > "+SshSession.Quote(remoteRoot+"/one/from-cloud.txt")));
                for(int i=0;i<30&&!File.Exists(Path.Combine(localRoot,"from-cloud.txt"));i++)await Task.Delay(500);
                if(!File.Exists(Path.Combine(localRoot,"from-cloud.txt"))||await File.ReadAllTextAsync(Path.Combine(localRoot,"from-cloud.txt"))!="cloud-namespace-write")throw new Exception("CloudCLI 命名空間無法寫入掛載目錄。");
                Console.WriteLine("CLOUD_NAMESPACE_MOUNT_WRITE_OK");
            }
        }
        finally { await session.UnmountAsync(); }
        string mounts=await session.ExecuteTextAsync("findmnt -rn -t fuse.davfs -o TARGET || true");
        if(mountClientId.Length>0&&mounts.Contains(mountClientId))throw new Exception("退出後仍殘留掛載。");
        Console.WriteLine("MOUNT_CLEANUP_OK");
    }
    if (phase == "mount-cloud")
    {
        if(!server.CloudReady||!server.MountReady)throw new Exception("聯動驗收需要已有 CloudCLI 和掛載組件。");
        string fixture=Path.Combine(Path.GetDirectoryName(Path.GetFullPath(args[0]))!,"cloud-link-"+Guid.NewGuid().ToString("N")[..10]);Directory.CreateDirectory(fixture);
        string one=Path.Combine(fixture,"one"),two=Path.Combine(fixture,"two");Directory.CreateDirectory(one);Directory.CreateDirectory(two);await File.WriteAllTextAsync(Path.Combine(one,"source.txt"),"cloud-link-local");
        string remote=server.HomeDirectory+"/cloud-link-verify-"+Guid.NewGuid().ToString("N")[..10];string clientId="";List<string> paths=[];bool unmounted=false;
        const string check="""
import base64,hmac,hashlib,json,pathlib,sqlite3,time,urllib.request,sys
home=pathlib.Path(sys.argv[1]);needle=sys.argv[2]
c=sqlite3.connect('file:'+str(home/'.local/state/safe-code-cloud/auth.db')+'?mode=ro',uri=True)
secret=c.execute("SELECT value FROM app_config WHERE key='jwt_secret'").fetchone()[0]
user=c.execute('SELECT id FROM users WHERE is_active=1 LIMIT 1').fetchone()[0]
def enc(value):return base64.urlsafe_b64encode(value).rstrip(b'=')
head=enc(b'{"alg":"HS256","typ":"JWT"}');payload=enc(json.dumps({'userId':user,'iat':int(time.time()),'exp':int(time.time())+120}).encode());message=head+b'.'+payload;token=message+b'.'+enc(hmac.new(secret.encode(),message,hashlib.sha256).digest())
request=urllib.request.Request('http://127.0.0.1:3001/api/projects',headers={'Authorization':'Bearer '+token.decode()})
projects=json.load(urllib.request.urlopen(request,timeout=30))
print(json.dumps({'projects':[{'id':p['projectId'],'name':p['displayName'],'path':p['fullPath']} for p in projects if needle in p['fullPath']],'stored':c.execute('SELECT project_id,project_path,isArchived FROM projects WHERE instr(project_path,?)>0 ORDER BY project_path',(needle,)).fetchall()}))
""";
        async Task<JsonElement> Check()=>JsonDocument.Parse(await session.ExecuteTextAsync("python3 -c "+SshSession.Quote(check)+" "+SshSession.Quote(server.HomeDirectory)+" "+SshSession.Quote(clientId))).RootElement.Clone();
        try
        {
            var first=await session.ApplyMountsAsync([new(one,remote+"/one"),new(two,remote+"/two")]);clientId=first.ClientId;paths=first.ManagedPaths??[];
            if(first.CloudWarning!=null||paths.Count!=2)throw new Exception(first.CloudWarning??"掛載路徑缺失。");
            if(await session.ExecuteTextAsync("cat "+SshSession.Quote(remote+"/one/source.txt"))!="cloud-link-local")throw new Exception("真實掛載讀取失敗。");
            var before=await Check();if(before.GetProperty("projects").GetArrayLength()!=2)throw new Exception("原生 CloudCLI API 未列出兩組目錄。");
            string ids=before.GetProperty("stored").GetRawText();
            var second=await session.ApplyMountsAsync([new(two,remote+"/two"),new(one,remote+"/one")]);
            if(second.CloudWarning!=null||!second.ManagedPaths!.SequenceEqual(paths.AsEnumerable().Reverse()))throw new Exception("改序改變了項目路徑。");
            var reordered=await Check();if(reordered.GetProperty("stored").GetRawText()!=ids)throw new Exception("重複掛載改變了項目 ID。");
            await session.RequestAsync<ServerInfo>("cloud_setup");if((await Check()).GetProperty("stored").GetRawText()!=ids)throw new Exception("補登記生成重複項目。");
            Console.WriteLine("CLOUD_NATIVE_API_TWO_PROJECTS_STABLE_IDS_REORDER_AND_OPEN_SYNC_OK");
            await session.UnmountAsync();unmounted=true;var after=await Check();
            if(after.GetProperty("projects").GetArrayLength()!=0||after.GetProperty("stored").EnumerateArray().Any(row=>row[2].GetInt32()!=1))throw new Exception("卸載後項目未歸檔。");
            var remounted=await session.ApplyMountsAsync([new(one,remote+"/one"),new(two,remote+"/two")]);unmounted=false;
            if(remounted.CloudWarning!=null||(await Check()).GetProperty("stored").GetRawText()!=ids)throw new Exception("重新掛載未恢復原項目。");
            await session.UnmountAsync();unmounted=true;Console.WriteLine("CLOUD_UNMOUNT_ARCHIVE_AND_REMOUNT_SAME_IDS_OK");
            uint refreshPort=await session.ForwardAsync(3001);using var http=new HttpClient();string html=await http.GetStringAsync("http://127.0.0.1:"+refreshPort);
            if(!html.Contains("data-safe-code-mounts")||!(await http.GetStringAsync("http://127.0.0.1:"+refreshPort+"/safe-code-mounts.js")).Contains("websocket_reconnected"))throw new Exception("CloudCLI 未提供自動重新整理組件。");
            Console.WriteLine("CLOUD_HTTP_REFRESH_ASSETS_OK");
        }
        finally
        {
            if(!unmounted)await session.UnmountAsync();
            if(paths.Count>0){
                string cleanup="import json,sqlite3,sys;c=sqlite3.connect(sys.argv[1]);paths=json.loads(sys.argv[2]);c.execute('BEGIN IMMEDIATE');[(c.execute('DELETE FROM safe_code_mount_projects WHERE project_path=?',(p,)),c.execute('DELETE FROM projects WHERE project_path=? AND NOT EXISTS(SELECT 1 FROM sessions WHERE project_path=?)',(p,p))) for p in paths];c.commit()";
                await session.ExecuteTextAsync("python3 -c "+SshSession.Quote(cleanup)+" "+SshSession.Quote(server.HomeDirectory+"/.local/state/safe-code-cloud/auth.db")+" "+SshSession.Quote(JsonSerializer.Serialize(paths)));
            }
            await session.ExecuteTextAsync("rmdir -- "+SshSession.Quote(remote+"/one")+" "+SshSession.Quote(remote+"/two")+" "+SshSession.Quote(remote));Directory.Delete(fixture,true);
        }
    }
    if (phase == "cloud")
    {
        server = await session.RequestAsync<ServerInfo>("cloud");
        uint local = await session.ForwardAsync(3001);
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(20) };
        string html = await http.GetStringAsync("http://127.0.0.1:" + local);
        if (!html.Contains("<html", StringComparison.OrdinalIgnoreCase)) throw new Exception("CloudCLI 未返回網頁。");
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
        if((int)(await http.GetAsync(url+"read.txt")).StatusCode!=401)throw new Exception("缺少認證未被攔截。");
        http.DefaultRequestHeaders.Authorization=new System.Net.Http.Headers.AuthenticationHeaderValue("Basic",Convert.ToBase64String(System.Text.Encoding.UTF8.GetBytes(server.Username+":"+server.Password)));
        if(await http.GetStringAsync(url+"read.txt")!="hello")throw new Exception("WebDAV 讀取錯誤。");
        var put=await http.PutAsync(url+"write.txt",new StringContent("world"));if(!put.IsSuccessStatusCode||await File.ReadAllTextAsync(Path.Combine(root,"write.txt"))!="world")throw new Exception("WebDAV 寫入錯誤。");
        if((int)(await http.DeleteAsync(url)).StatusCode!=403)throw new Exception("允許刪除掛載根目錄。");
        if((int)(await http.GetAsync(url+"..%2Foutside.txt")).StatusCode!=403)throw new Exception("路徑穿越未被攔截。");
        if(!OperatingSystem.IsWindows()){
            string outside=Path.Combine(Path.GetTempPath(),"safe-code-outside-"+Guid.NewGuid().ToString("N"));await File.WriteAllTextAsync(outside,"not-shared");File.CreateSymbolicLink(Path.Combine(root,"link"),outside);
            if((int)(await http.GetAsync(url+"link")).StatusCode!=403)throw new Exception("符號連結逃逸未被攔截。");File.Delete(outside);
        }
        Console.WriteLine("WEBDAV_AUTH_READ_WRITE_TRAVERSAL_ROOT_PROTECTION_OK");
    }
    finally{Directory.Delete(root,true);}
}
