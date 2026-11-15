using System.Globalization;
using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Xml.Linq;

namespace SafeCode.Core;
public sealed record MountMapping(string Local, string Remote);

/// <summary>仅监听本机回环、带独立会话认证的 WebDAV 文件桥。远端经反向 SSH 隧道访问。</summary>
public sealed record FileTransferStats(long BytesToServer,long BytesToLocal,long CompletedFiles,int ActiveTransfers);

public sealed class WebDavServer : IDisposable
{
    private readonly TcpListener listener = new(IPAddress.Loopback, 0);
    private readonly CancellationTokenSource cancellation = new();
    private readonly Dictionary<string,string> roots;
    private readonly byte[] authorization;
    private readonly ConcurrentDictionary<string,SemaphoreSlim> mutations = new(OperatingSystem.IsWindows()?StringComparer.OrdinalIgnoreCase:StringComparer.Ordinal);
    private long bytesToServer,bytesToLocal,completedFiles;
    private int activeTransfers;
    public FileTransferStats TransferStats=>new(Interlocked.Read(ref bytesToServer),Interlocked.Read(ref bytesToLocal),Interlocked.Read(ref completedFiles),Volatile.Read(ref activeTransfers));
    public TimeSpan IdleTimeout {get;init;}=TimeSpan.FromMinutes(2);
    public string Username { get; } = "safecode";
    public string Password { get; }
    public int Port => ((IPEndPoint)listener.LocalEndpoint).Port;
    public WebDavServer(IReadOnlyList<MountMapping> mappings,string? sessionPassword=null)
    {
        if(sessionPassword!=null&&(sessionPassword.Length!=48||!sessionPassword.All(Uri.IsHexDigit)))throw new ArgumentException("文件通道会话凭据无效。");
        Password=sessionPassword??Convert.ToHexString(RandomNumberGenerator.GetBytes(24)).ToLowerInvariant();
        roots = new Dictionary<string,string>(StringComparer.Ordinal);
        for (int i=0;i<mappings.Count;i++)
        {
            string path=Path.GetFullPath(mappings[i].Local);
            if (!Directory.Exists(path)) throw new DirectoryNotFoundException("本地目录不存在："+path);
            if ((File.GetAttributes(path)&FileAttributes.ReparsePoint)!=0) throw new IOException("请直接选择实际目录，不使用符号链接。");
            roots["m"+i]=Path.TrimEndingDirectorySeparator(path);
        }
        authorization=Encoding.ASCII.GetBytes("Basic "+Convert.ToBase64String(Encoding.UTF8.GetBytes(Username+":"+Password)));
        listener.Start(); _=Task.Run(AcceptAsync);
    }
    private async Task AcceptAsync()
    {
        while(!cancellation.IsCancellationRequested)
        {
            try { var client=await listener.AcceptTcpClientAsync(cancellation.Token); _=HandleAsync(client); }
            catch(OperationCanceledException){break;}catch(ObjectDisposedException){break;}
        }
    }
    private async Task HandleAsync(TcpClient client)
    {
        using(client)
        {
            client.ReceiveTimeout=30000;client.SendTimeout=30000;
            using var stream=client.GetStream();
            using var idle=CancellationTokenSource.CreateLinkedTokenSource(cancellation.Token);idle.CancelAfter(IdleTimeout);
            var token=idle.Token;bool responseStarted=false;
            try
            {
                var headerBytes=new List<byte>();var one=new byte[1];
                while(headerBytes.Count<32768)
                {
                    int count=await stream.ReadAsync(one,token);if(count==0)return;headerBytes.Add(one[0]);idle.CancelAfter(IdleTimeout);
                    int n=headerBytes.Count;
                    if(n>=4&&headerBytes[n-4]==13&&headerBytes[n-3]==10&&headerBytes[n-2]==13&&headerBytes[n-1]==10)break;
                }
                if(headerBytes.Count>=32768){await Reply(stream,431,[]);return;}
                string[] lines=Encoding.ASCII.GetString(headerBytes.ToArray()).Split("\r\n");
                string[] request=lines[0].Split(' ');if(request.Length!=3){await Reply(stream,400,[]);return;}
                var headers=lines.Skip(1).Where(x=>x.Contains(':')).Select(x=>x.Split(':',2)).ToDictionary(x=>x[0],x=>x[1].Trim(),StringComparer.OrdinalIgnoreCase);
                var supplied=Encoding.ASCII.GetBytes(headers.GetValueOrDefault("Authorization", ""));
                if(supplied.Length!=authorization.Length||!CryptographicOperations.FixedTimeEquals(supplied,authorization))
                {await Reply(stream,401,[],extra:"WWW-Authenticate: Basic realm=\"SafeCode\"\r\n");return;}
                if(headers.ContainsKey("Transfer-Encoding")){await Reply(stream,501,[]);return;}
                long contentLength=0;if(headers.TryGetValue("Content-Length",out var declared)&&!long.TryParse(declared,out contentLength)){await Reply(stream,400,[]);return;}
                if(contentLength<0||contentLength>1024L*1024*1024*1024){await Reply(stream,413,[]);return;}
                string method=request[0];
                if(method=="PUT"&&!headers.ContainsKey("Content-Length")){await Reply(stream,411,[]);return;}
                string rawPath=request[1].Split('?',2)[0];
                var resolved=Resolve(rawPath);string path=resolved.Path;
                if(headers.GetValueOrDefault("Expect","").Equals("100-continue",StringComparison.OrdinalIgnoreCase))
                    await stream.WriteAsync(Encoding.ASCII.GetBytes("HTTP/1.1 100 Continue\r\n\r\n"),token);
                // 小型 XML 请求先读完，避免关闭时残留请求体导致连接重置。
                if(method!="PUT"&&contentLength>0)
                {
                    if(contentLength>1024*1024){await Reply(stream,413,[]);return;}
                    await CopyExact(stream,Stream.Null,contentLength,token,reset:()=>idle.CancelAfter(IdleTimeout));
                }
                switch(method)
                {
                    case "OPTIONS":await Reply(stream,200,[],extra:"DAV: 1, 2\r\nAllow: OPTIONS, PROPFIND, GET, HEAD, PUT, MKCOL, DELETE, MOVE, COPY, LOCK, UNLOCK, PROPPATCH\r\n");break;
                    case "PROPFIND":
                        if(!Exists(path)){await Reply(stream,404,[]);break;}
                        var properties=new List<XElement>{Properties(path,rawPath)};
                        if(Directory.Exists(path)&&headers.GetValueOrDefault("Depth","1")!="0")
                            foreach(string child in Directory.EnumerateFileSystemEntries(path))
                            {if((File.GetAttributes(child)&FileAttributes.ReparsePoint)!=0)continue;properties.Add(Properties(child,rawPath.TrimEnd('/')+"/"+Uri.EscapeDataString(Path.GetFileName(child))));}
                        XNamespace dav="DAV:";
                        string xml=new XDocument(new XDeclaration("1.0","utf-8",null),new XElement(dav+"multistatus",properties)).ToString();
                        await Reply(stream,207,Encoding.UTF8.GetBytes(xml),"application/xml; charset=utf-8");break;
                    case "GET":case "HEAD":
                        if(!File.Exists(path)){await Reply(stream,404,[]);break;}
                        using(var file=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete,65536,true))
                        {
                            long start=0,length=file.Length;int status=200;
                            string extra="Accept-Ranges: bytes\r\nETag: "+ETag(path)+"\r\nLast-Modified: "+File.GetLastWriteTimeUtc(path).ToString("R",CultureInfo.InvariantCulture)+"\r\n";
                            if(headers.TryGetValue("Range",out var range)&&(!headers.TryGetValue("If-Range",out var condition)||condition==ETag(path)||condition==File.GetLastWriteTimeUtc(path).ToString("R",CultureInfo.InvariantCulture)))
                            {
                                if(!TryRange(range,file.Length,out start,out length)){await Reply(stream,416,[],extra:"Content-Range: bytes */"+file.Length+"\r\n");break;}
                                status=206;extra+="Content-Range: bytes "+start+"-"+(start+length-1)+"/"+file.Length+"\r\n";
                            }
                            responseStarted=true;await WriteHeader(stream,status,length,"application/octet-stream",extra);
                            if(method=="GET")
                            {
                                Interlocked.Increment(ref activeTransfers);
                                try{file.Position=start;await CopyExact(file,stream,length,token,count=>Interlocked.Add(ref bytesToServer,count),()=>idle.CancelAfter(IdleTimeout));if(start==0&&length==file.Length)Interlocked.Increment(ref completedFiles);}
                                finally{Interlocked.Decrement(ref activeTransfers);}
                            }
                        }break;
                    case "PUT":
                        if(resolved.IsRoot||!Directory.Exists(Path.GetDirectoryName(path))){await Reply(stream,409,[]);break;}
                        var mutation=mutations.GetOrAdd(path,_=>new SemaphoreSlim(1,1));
                        await mutation.WaitAsync(token);
                        Interlocked.Increment(ref activeTransfers);
                        string temporary=path+".safe-code-"+Guid.NewGuid().ToString("N");
                        try
                        {
                            bool existed=File.Exists(path);
                            await using(var file=new FileStream(temporary,FileMode.CreateNew,FileAccess.Write,FileShare.None,65536,true))await CopyExact(stream,file,contentLength,token,count=>Interlocked.Add(ref bytesToLocal,count),()=>idle.CancelAfter(IdleTimeout));
                            File.Move(temporary,path,true);Interlocked.Increment(ref completedFiles);await Reply(stream,existed?204:201,[],extra:"ETag: "+ETag(path)+"\r\n");
                        }
                        finally{try{if(File.Exists(temporary))File.Delete(temporary);}finally{mutation.Release();Interlocked.Decrement(ref activeTransfers);}}break;
                    case "MKCOL":
                        if(resolved.IsRoot||Exists(path)){await Reply(stream,405,[]);break;}
                        if(!Directory.Exists(Path.GetDirectoryName(path))){await Reply(stream,409,[]);break;}
                        Directory.CreateDirectory(path);await Reply(stream,201,[]);break;
                    case "DELETE":
                        if(resolved.IsRoot){await Reply(stream,403,[]);break;}
                        if(File.Exists(path))File.Delete(path);else if(Directory.Exists(path))Directory.Delete(path,true);else{await Reply(stream,404,[]);break;}
                        await Reply(stream,204,[]);break;
                    case "MOVE":case "COPY":
                        if(resolved.IsRoot){await Reply(stream,403,[]);break;}
                        string destination=headers.GetValueOrDefault("Destination","");
                        if(Uri.TryCreate(destination,UriKind.Absolute,out var destinationUri))destination=destinationUri.AbsolutePath;
                        var target=Resolve(destination);
                        if(target.IsRoot){await Reply(stream,403,[]);break;}
                        if(method=="MOVE")
                        {if(File.Exists(path))File.Move(path,target.Path,true);else if(Directory.Exists(path))Directory.Move(path,target.Path);else{await Reply(stream,404,[]);break;}}
                        else if(File.Exists(path))File.Copy(path,target.Path,true);else{await Reply(stream,501,[]);break;}
                        await Reply(stream,201,[]);break;
                    case "LOCK":
                        string lockToken="opaquelocktoken:"+Guid.NewGuid();
                        string lockXml="<?xml version=\"1.0\"?><D:prop xmlns:D=\"DAV:\"><D:lockdiscovery><D:activelock><D:locktype><D:write/></D:locktype><D:lockscope><D:exclusive/></D:lockscope><D:depth>0</D:depth><D:timeout>Second-3600</D:timeout><D:locktoken><D:href>"+lockToken+"</D:href></D:locktoken></D:activelock></D:lockdiscovery></D:prop>";
                        await Reply(stream,200,Encoding.UTF8.GetBytes(lockXml),"application/xml", "Lock-Token: <"+lockToken+">\r\n");break;
                    case "UNLOCK":case "PROPPATCH":await Reply(stream,204,[]);break;
                    default:await Reply(stream,405,[]);break;
                }
            }
            catch(OperationCanceledException){}
            catch(UnauthorizedAccessException){try{if(!responseStarted)await Reply(stream,403,[]);}catch{}}
            catch(FileNotFoundException){try{if(!responseStarted)await Reply(stream,404,[]);}catch{}}
            catch(DirectoryNotFoundException){try{if(!responseStarted)await Reply(stream,404,[]);}catch{}}
            catch{try{if(!responseStarted)await Reply(stream,500,[]);}catch{}}
        }
    }
    private (string Path,bool IsRoot) Resolve(string path)
    {
        string decoded=Uri.UnescapeDataString(path);
        if(!decoded.StartsWith('/')||decoded.Contains('\\')||decoded.Contains('\0'))throw new UnauthorizedAccessException();
        string[] parts=decoded.Split('/',StringSplitOptions.RemoveEmptyEntries);
        if(parts.Length==0||!roots.TryGetValue(parts[0],out string? root)||parts.Any(x=>x is "." or ".."))throw new UnauthorizedAccessException();
        string full=Path.GetFullPath(Path.Combine([root,..parts.Skip(1)]));
        var comparison=OperatingSystem.IsWindows()?StringComparison.OrdinalIgnoreCase:StringComparison.Ordinal;
        if(!full.Equals(root,comparison)&&!full.StartsWith(root+Path.DirectorySeparatorChar,comparison))throw new UnauthorizedAccessException();
        string current=root;
        foreach(string part in parts.Skip(1))
        {
            current=Path.Combine(current,part);
            if(Exists(current)&&(File.GetAttributes(current)&FileAttributes.ReparsePoint)!=0)throw new UnauthorizedAccessException();
        }
        return(full,parts.Length==1);
    }
    private static bool TryRange(string value,long total,out long start,out long length)
    {
        start=0;length=0;if(total==0||!value.StartsWith("bytes=",StringComparison.OrdinalIgnoreCase)||value.Contains(','))return false;
        var parts=value[6..].Split('-',2);if(parts.Length!=2)return false;
        if(parts[0].Length==0)
        {if(!long.TryParse(parts[1],out long suffix)||suffix<=0)return false;length=Math.Min(suffix,total);start=total-length;return true;}
        if(!long.TryParse(parts[0],out start)||start<0||start>=total)return false;
        long end=total-1;if(parts[1].Length>0&&(!long.TryParse(parts[1],out end)||end<start))return false;
        length=Math.Min(end,total-1)-start+1;return true;
    }
    private static bool Exists(string path)=>File.Exists(path)||Directory.Exists(path);
    private static string ETag(string path)=>"\""+File.GetLastWriteTimeUtc(path).Ticks.ToString("x")+"-"+(File.Exists(path)?new FileInfo(path).Length:0).ToString("x")+"\"";
    private static XElement Properties(string path,string href)
    {
        XNamespace d="DAV:";bool directory=Directory.Exists(path);href=href.TrimEnd('/')+(directory?"/":"");
        return new XElement(d+"response",new XElement(d+"href",href),new XElement(d+"propstat",
            new XElement(d+"prop",new XElement(d+"displayname",Path.GetFileName(path)),new XElement(d+"resourcetype",directory?new XElement(d+"collection"):null),
            new XElement(d+"getcontentlength",directory?0:new FileInfo(path).Length),new XElement(d+"getlastmodified",File.GetLastWriteTimeUtc(path).ToString("R",CultureInfo.InvariantCulture)),
            new XElement(d+"creationdate",File.GetCreationTimeUtc(path).ToString("yyyy-MM-ddTHH:mm:ssZ")),new XElement(d+"getetag",ETag(path))),new XElement(d+"status","HTTP/1.1 200 OK")));
    }
    private static async Task CopyExact(Stream input,Stream output,long length,CancellationToken ct,Action<int>? progress=null,Action? reset=null)
    {
        byte[] buffer=new byte[65536];long remaining=length;
        while(remaining>0){int count=await input.ReadAsync(buffer.AsMemory(0,(int)Math.Min(buffer.Length,remaining)),ct);if(count==0)throw new EndOfStreamException();await output.WriteAsync(buffer.AsMemory(0,count),ct);remaining-=count;progress?.Invoke(count);reset?.Invoke();}
    }
    private static async Task Reply(Stream stream,int status,byte[] bytes,string contentType="text/plain",string extra="")
    {await WriteHeader(stream,status,bytes.LongLength,contentType,extra);if(bytes.Length>0)await stream.WriteAsync(bytes);}
    private static Task WriteHeader(Stream stream,int code,long length,string contentType,string extra)
    {
        string status=code switch {200=>"OK",206=>"Partial Content",411=>"Length Required",416=>"Range Not Satisfiable",201=>"Created",204=>"No Content",207=>"Multi-Status",400=>"Bad Request",401=>"Unauthorized",403=>"Forbidden",404=>"Not Found",405=>"Method Not Allowed",409=>"Conflict",413=>"Content Too Large",431=>"Request Header Fields Too Large",501=>"Not Implemented",_=>"Internal Server Error"};
        return stream.WriteAsync(Encoding.ASCII.GetBytes($"HTTP/1.1 {code} {status}\r\nContent-Length: {length}\r\nContent-Type: {contentType}\r\nConnection: close\r\nDAV: 1, 2\r\n{extra}\r\n")).AsTask();
    }
    public void Dispose(){cancellation.Cancel();listener.Stop();cancellation.Dispose();}
}
