using SafeCode.Core;
using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;

string root=Path.Combine(Path.GetTempPath(),"safe-code-large-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(root);
try
{
    using var bridge=new WebDavServer([new(root,"/fixture")]);
    using var http=new HttpClient{Timeout=TimeSpan.FromMinutes(5)};
    string auth=Convert.ToBase64String(Encoding.UTF8.GetBytes(bridge.Username+":"+bridge.Password));
    http.DefaultRequestHeaders.Authorization=new AuthenticationHeaderValue("Basic",auth);
    string url="http://127.0.0.1:"+bridge.Port+"/m0/";
    long length=2L*1024*1024*1024+65537;
    byte[] pattern=Enumerable.Range(0,65536).Select(i=>(byte)(i*31+7)).ToArray();
    using var expected=IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
    for(long offset=0;offset<length;offset+=pattern.Length)expected.AppendData(pattern,0,(int)Math.Min(pattern.Length,length-offset));
    byte[] expectedHash=expected.GetHashAndReset();long before=GC.GetTotalMemory(true);
    using(var response=await http.PutAsync(url+"large.bin",new GeneratedContent(length,pattern)))
        if(response.StatusCode!=HttpStatusCode.Created)throw new Exception("超过 2 GB 的上传失败："+response.StatusCode);
    if(new FileInfo(Path.Combine(root,"large.bin")).Length!=length)throw new Exception("大文件被截断。");
    await using(var file=File.OpenRead(Path.Combine(root,"large.bin")))if(!SHA256.HashData(file).SequenceEqual(expectedHash))throw new Exception("上传内容校验失败。");
    using(var response=await http.GetAsync(url+"large.bin",HttpCompletionOption.ResponseHeadersRead))
    await using(var input=await response.Content.ReadAsStreamAsync())
        if(!SHA256.HashData(input).SequenceEqual(expectedHash))throw new Exception("大文件下载内容校验失败。");
    foreach(var range in new[]{"bytes=2147483648-2147483711","bytes=-128"})
    {
        using var request=new HttpRequestMessage(HttpMethod.Get,url+"large.bin");request.Headers.TryAddWithoutValidation("Range",range);
        using var response=await http.SendAsync(request);if(response.StatusCode!=HttpStatusCode.PartialContent)throw new Exception("分段读取未返回 206。");
        long start=range.StartsWith("bytes=-")?length-128:2147483648L;byte[] data=await response.Content.ReadAsByteArrayAsync();
        if(!data.Select((value,i)=>value==pattern[(int)((start+i)%pattern.Length)]).All(x=>x))throw new Exception("大文件范围读取内容错误。");
    }
    using(var request=new HttpRequestMessage(HttpMethod.Get,url+"large.bin"))
    {request.Headers.TryAddWithoutValidation("Range","bytes=999999999999-");using var response=await http.SendAsync(request);if((int)response.StatusCode!=416)throw new Exception("无效范围未拒绝。");}
    await File.WriteAllTextAsync(Path.Combine(root,"keep.txt"),"original");
    using(var stalled=new TcpClient())
    {
        await stalled.ConnectAsync("127.0.0.1",bridge.Port);var stream=stalled.GetStream();
        await stream.WriteAsync(Encoding.ASCII.GetBytes("PUT /m0/keep.txt HTTP/1.1\r\nAuthorization: Basic "+auth+"\r\nContent-Length: 4096\r\n\r\npartial"));
        // 一个文件暂停时，另一个文件仍可写入，不等待全局上传锁。
        using var other=await http.PutAsync(url+"parallel.txt",new StringContent("parallel"));if(!other.IsSuccessStatusCode)throw new Exception("并行文件写入被阻塞。");
    }
    for(int i=0;i<30&&Directory.EnumerateFiles(root,"*.safe-code-*").Any();i++)await Task.Delay(100);
    if(await File.ReadAllTextAsync(Path.Combine(root,"keep.txt"))!="original"||Directory.EnumerateFiles(root,"*.safe-code-*").Any())throw new Exception("中断上传损坏原文件或遗留临时文件。");
    using(var timed=new WebDavServer([new(root,"/fixture")]){IdleTimeout=TimeSpan.FromMilliseconds(500)})
    using(var stalled=new TcpClient())
    {
        await stalled.ConnectAsync("127.0.0.1",timed.Port);var stream=stalled.GetStream();string key=Convert.ToBase64String(Encoding.UTF8.GetBytes(timed.Username+":"+timed.Password));
        await stream.WriteAsync(Encoding.ASCII.GetBytes("PUT /m0/keep.txt HTTP/1.1\r\nAuthorization: Basic "+key+"\r\nContent-Length: 4096\r\n\r\npartial"));
        await Task.Delay(1200);using var timeout=new CancellationTokenSource(TimeSpan.FromSeconds(2));if(await stream.ReadAsync(new byte[1],timeout.Token)!=0)throw new Exception("空闲传输没有关闭。");
    }
    if(await File.ReadAllTextAsync(Path.Combine(root,"keep.txt"))!="original")throw new Exception("空闲超时损坏文件。");
    var stats=bridge.TransferStats;if(stats.BytesToServer<length||stats.BytesToLocal<length||stats.CompletedFiles!=3||stats.ActiveTransfers!=0)throw new Exception("文件传输统计不准确。");
    if(GC.GetTotalMemory(true)-before>256L*1024*1024)throw new Exception("大文件传输占用了过多托管内存。");
    Console.WriteLine("LARGE_2GB_PLUS_SHA256_BIDIRECTIONAL_RANGE_ATOMIC_ABORT_PARALLEL_IDLE_TIMEOUT_AND_STATS_PASSED");
}
finally{Directory.Delete(root,true);}
sealed class GeneratedContent(long length,byte[] pattern):HttpContent
{
    protected override bool TryComputeLength(out long value){value=length;return true;}
    protected override async Task SerializeToStreamAsync(Stream stream,TransportContext? context)
    {for(long offset=0;offset<length;offset+=pattern.Length)await stream.WriteAsync(pattern.AsMemory(0,(int)Math.Min(pattern.Length,length-offset)));}
}
