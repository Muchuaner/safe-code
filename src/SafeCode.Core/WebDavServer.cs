using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Xml.Linq;

namespace SafeCode.Core;
public sealed record MountMapping(string Local, string Remote);

/// <summary>仅监听本机回环、带独立会话认证的 WebDAV 文件桥。远端经反向 SSH 隧道访问。</summary>
public sealed class WebDavServer : IDisposable
{
    private readonly TcpListener listener = new(IPAddress.Loopback, 0);
    private readonly CancellationTokenSource cancellation = new();
    private readonly Dictionary<string,string> roots;
    private readonly byte[] authorization;
    private readonly SemaphoreSlim mutations = new(1,1);
    public string Username { get; } = "safecode";
    public string Password { get; } = Convert.ToHexString(RandomNumberGenerator.GetBytes(24)).ToLowerInvariant();
    public int Port => ((IPEndPoint)listener.LocalEndpoint).Port;
    public WebDavServer(IReadOnlyList<MountMapping> mappings)
    {
        roots = new Dictionary<string,string>(StringComparer.Ordinal);
        for (int i=0;i<mappings.Count;i++)
        {
            string path=Path.GetFullPath(mappings[i].Local);
            if (!Directory.Exists(path)) throw new DirectoryNotFoundException("本地目录不存在："+path);
            if ((File.GetAttributes(path)&FileAttributes.ReparsePoint)!=0) throw new IOException("请直接选择实际目录，不使用符号链接。");
            roots["m"+i]=Path.TrimEndingDirectorySeparator(path);
        }
        authorization=Encoding.ASCII.GetBytes("Basic "+Convert.ToBase64String(Encoding.UTF8.GetBytes(Username+":"+Password)));
        listener.Start(); _=AcceptAsync();
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
            try
            {
                var headerBytes=new List<byte>();var one=new byte[1];
                while(headerBytes.Count<32768)
                {
                    int count=await stream.ReadAsync(one,cancellation.Token);if(count==0)return;headerBytes.Add(one[0]);
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
                long contentLength=long.TryParse(headers.GetValueOrDefault("Content-Length"),out var parsed)?parsed:0;
                if(contentLength<0||contentLength>2L*1024*1024*1024){await Reply(stream,413,[]);return;}
                string method=request[0];string rawPath=request[1].Split('?',2)[0];
                var resolved=Resolve(rawPath);string path=resolved.Path;
                if(headers.GetValueOrDefault("Expect","").Equals("100-continue",StringComparison.OrdinalIgnoreCase))
                    await stream.WriteAsync(Encoding.ASCII.GetBytes("HTTP/1.1 100 Continue\r\n\r\n"),cancellation.Token);
                // 小型 XML 请求先读完，避免关闭时残留请求体导致连接重置。
                if(method!="PUT"&&contentLength>0)
                {
                    if(contentLength>1024*1024){await Reply(stream,413,[]);return;}
                    await CopyExact(stream,Stream.Null,contentLength,cancellation.Token);
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
                            await WriteHeader(stream,200,file.Length,"application/octet-stream","ETag: "+ETag(path)+"\r\nLast-Modified: "+File.GetLastWriteTimeUtc(path).ToString("R",CultureInfo.InvariantCulture)+"\r\n");
                            if(method=="GET")await file.CopyToAsync(stream,cancellation.Token);
                        }break;
                    case "PUT":
                        if(resolved.IsRoot||!Directory.Exists(Path.GetDirectoryName(path))){await Reply(stream,409,[]);break;}
                        await mutations.WaitAsync(cancellation.Token);
                        string temporary=path+".safe-code-"+Guid.NewGuid().ToString("N");
                        try
                        {
                            bool existed=File.Exists(path);
                            await using(var file=new FileStream(temporary,FileMode.CreateNew,FileAccess.Write,FileShare.None,65536,true))await CopyExact(stream,file,contentLength,cancellation.Token);
                            File.Move(temporary,path,true);await Reply(stream,existed?204:201,[],extra:"ETag: "+ETag(path)+"\r\n");
                        }
                        finally{if(File.Exists(temporary))File.Delete(temporary);mutations.Release();}break;
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
                        string token="opaquelocktoken:"+Guid.NewGuid();
                        string lockXml="<?xml version=\"1.0\"?><D:prop xmlns:D=\"DAV:\"><D:lockdiscovery><D:activelock><D:locktype><D:write/></D:locktype><D:lockscope><D:exclusive/></D:lockscope><D:depth>0</D:depth><D:timeout>Second-3600</D:timeout><D:locktoken><D:href>"+token+"</D:href></D:locktoken></D:activelock></D:lockdiscovery></D:prop>";
                        await Reply(stream,200,Encoding.UTF8.GetBytes(lockXml),"application/xml", "Lock-Token: <"+token+">\r\n");break;
                    case "UNLOCK":case "PROPPATCH":await Reply(stream,204,[]);break;
                    default:await Reply(stream,405,[]);break;
                }
            }
            catch(UnauthorizedAccessException){try{await Reply(stream,403,[]);}catch{}}
            catch(FileNotFoundException){try{await Reply(stream,404,[]);}catch{}}
            catch(DirectoryNotFoundException){try{await Reply(stream,404,[]);}catch{}}
            catch{try{await Reply(stream,500,[]);}catch{}}
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
    private static async Task CopyExact(Stream input,Stream output,long length,CancellationToken ct)
    {
        byte[] buffer=new byte[65536];long remaining=length;
        while(remaining>0){int count=await input.ReadAsync(buffer.AsMemory(0,(int)Math.Min(buffer.Length,remaining)),ct);if(count==0)throw new EndOfStreamException();await output.WriteAsync(buffer.AsMemory(0,count),ct);remaining-=count;}
    }
    private static async Task Reply(Stream stream,int status,byte[] bytes,string contentType="text/plain",string extra="")
    {await WriteHeader(stream,status,bytes.LongLength,contentType,extra);if(bytes.Length>0)await stream.WriteAsync(bytes);}
    private static Task WriteHeader(Stream stream,int code,long length,string contentType,string extra)
    {
        string status=code switch {200=>"OK",201=>"Created",204=>"No Content",207=>"Multi-Status",400=>"Bad Request",401=>"Unauthorized",403=>"Forbidden",404=>"Not Found",405=>"Method Not Allowed",409=>"Conflict",413=>"Content Too Large",431=>"Request Header Fields Too Large",501=>"Not Implemented",_=>"Internal Server Error"};
        return stream.WriteAsync(Encoding.ASCII.GetBytes($"HTTP/1.1 {code} {status}\r\nContent-Length: {length}\r\nContent-Type: {contentType}\r\nConnection: close\r\nDAV: 1, 2\r\n{extra}\r\n")).AsTask();
    }
    public void Dispose(){cancellation.Cancel();listener.Stop();cancellation.Dispose();}
}
