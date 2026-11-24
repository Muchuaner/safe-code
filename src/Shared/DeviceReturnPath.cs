using System.IO;
using System.Net.Http;
using System.Net.NetworkInformation;
using System.Text.Json;

namespace SafeCode.Desktop;

// One monitor per authorized FRP tunnel. A new transport, a network change or a
// gap across sleep invalidates the previous SSH verification, even if TCP looks open.
internal sealed class DeviceReturnPath(Func<CancellationToken,Task<JsonElement>> renew,
    Func<CancellationToken,Task<string>> verify, Func<bool> transportReady, Func<bool> processExited,
    CancellationToken lifetime)
{
    private static readonly int[] RetryDelayMs = [4000,8000,16000,30000];
    private readonly SemaphoreSlim check = new(1);
    private int generation;
    private int verifiedGeneration = -1;
    private long lastLease, lastVerification;
    private volatile string? failure;
    internal string Command { get; private set; } = "";
    private static long Now => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
    private static bool Recent(long time,long limit) { long age=Now-time; return age>=0 && age<limit; }

    internal string? Problem
    {
        get
        {
            if(processExited())return "设备隧道进程已退出，请在 SafeCode 停用后重新启用设备接入。";
            if(failure is {} reason)return reason;
            if(!transportReady())return "设备隧道正在重连，请检查本机网络和网关连接。";
            return Volatile.Read(ref verifiedGeneration)==Volatile.Read(ref generation) && Recent(Interlocked.Read(ref lastLease),45000)
                ? null : "正在重新确认设备租约和网关到本机的 SSH 回连，请稍后重试。";
        }
    }

    internal int Generation=>Volatile.Read(ref generation);
    internal void Invalidate()=>Interlocked.Increment(ref generation);
    private void AddressChanged(object? sender,EventArgs args)=>Invalidate();
    internal async Task MonitorAsync()
    {
        NetworkChange.NetworkAddressChanged+=AddressChanged;
        try
        {
            using var timer=new PeriodicTimer(TimeSpan.FromSeconds(4));
            int failures=0;
            long lastAttempt=0;
            int attemptedGeneration=Generation;
            while(await timer.WaitForNextTickAsync(lifetime).ConfigureAwait(false))
            {
                if(Problem==null && Recent(Interlocked.Read(ref lastLease),20000))continue;
                if(failures>0 && Generation==attemptedGeneration && Recent(lastAttempt,RetryDelayMs[failures-1]))continue;
                lastAttempt=Now;
                try { await CheckAsync(false,lifetime).ConfigureAwait(false); failures=0; }
                catch(InvalidOperationException) { failures=Math.Min(failures+1,RetryDelayMs.Length); attemptedGeneration=Generation; }
            }
        }
        catch(OperationCanceledException) when(lifetime.IsCancellationRequested) {}
        finally { NetworkChange.NetworkAddressChanged-=AddressChanged; }
    }

    internal async Task EnsureAsync(bool force,CancellationToken ct)
    {
        using var linked=CancellationTokenSource.CreateLinkedTokenSource(ct,lifetime);
        linked.CancelAfter(TimeSpan.FromSeconds(12));
        try
        {
            // Allow FRP to establish a new transport before verifying the SSH login.
            for(int i=0;i<8 && !transportReady() && !processExited();i++)
                await Task.Delay(250,linked.Token).ConfigureAwait(false);
            if(force || Problem!=null)await CheckAsync(force,linked.Token).ConfigureAwait(false);
        }
        catch(OperationCanceledException) when(!ct.IsCancellationRequested && !lifetime.IsCancellationRequested)
        {
            failure="设备回连验证超时，请检查本机网络及网关。";
            Invalidate();
            throw new InvalidOperationException(failure);
        }
    }

    private async Task CheckAsync(bool force,CancellationToken ct)
    {
        await check.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            if(!force && Problem==null && Recent(Interlocked.Read(ref lastLease),20000))return;
            int epoch=Volatile.Read(ref generation);
            bool verifyAgain=force || Problem!=null || !Recent(Interlocked.Read(ref lastVerification),120000);
            // Renew even when FRP is reconnecting: the gateway requires a current lease to accept it.
            if(processExited())throw new InvalidOperationException("设备隧道进程已退出，请重新启用设备接入。");
            var lease=await renew(ct).ConfigureAwait(false);
            if(!transportReady())throw new InvalidOperationException("设备租约已续期，FRP 隧道尚未连回网关，请检查网络后稍候。");
            if(lease.ValueKind!=JsonValueKind.Object || !lease.TryGetProperty("ready",out var ready) ||
                ready.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
                throw new IOException("无效的设备续租响应。");
            if(ready.ValueKind==JsonValueKind.False)verifyAgain=true;
            if(verifyAgain)
            {
                Command=await verify(ct).ConfigureAwait(false);
                Interlocked.Exchange(ref lastVerification,Now);
            }
            if(!transportReady()||epoch!=Volatile.Read(ref generation))
                throw new InvalidOperationException("验证期间网络发生变化，正在重新确认设备回连。");
            Volatile.Write(ref verifiedGeneration,epoch);
            Interlocked.Exchange(ref lastLease,Now);
            failure=null;
        }
        catch(Exception error) when(error is HttpRequestException or IOException or JsonException or InvalidOperationException or OperationCanceledException)
        {
            ct.ThrowIfCancellationRequested();
            failure=error switch {
                InvalidOperationException => error.Message,
                OperationCanceledException => "设备回连验证超时，请检查本机网络及网关。",
                HttpRequestException {HttpRequestError:HttpRequestError.SecureConnectionError} => "与设备网关的 TLS 握手未完成。请先检查网络，若持续失败再核对网关证书。",
                HttpRequestException => "无法联系设备授权网关，请检查网络和网关证书。",
                _ => "设备网关没有返回有效的续租或 SSH 验证结果，请检查网关服务。"
            };
            Invalidate();
            throw new InvalidOperationException(failure,error);
        }
        finally {check.Release();}
    }
}
