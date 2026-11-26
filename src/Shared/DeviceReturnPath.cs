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
            if(processExited())return "裝置隧道進程已退出，請在 SafeCode 停用後重新啟用裝置接入。";
            if(failure is {} reason)return reason;
            if(!transportReady())return "裝置隧道正在重連，請檢查本機網絡和網關連線。";
            return Volatile.Read(ref verifiedGeneration)==Volatile.Read(ref generation) && Recent(Interlocked.Read(ref lastLease),45000)
                ? null : "正在重新確認裝置租約和網關到本機的 SSH 回連，請稍後重試。";
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
            failure="裝置回連驗證超時，請檢查本機網絡及網關。";
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
            if(processExited())throw new InvalidOperationException("裝置隧道進程已退出，請重新啟用裝置接入。");
            var lease=await renew(ct).ConfigureAwait(false);
            if(!transportReady())throw new InvalidOperationException("裝置租約已續期，FRP 隧道尚未連回網關，請檢查網絡後稍候。");
            if(lease.ValueKind!=JsonValueKind.Object || !lease.TryGetProperty("ready",out var ready) ||
                ready.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
                throw new IOException("無效的裝置續租響應。");
            if(ready.ValueKind==JsonValueKind.False)verifyAgain=true;
            if(verifyAgain)
            {
                Command=await verify(ct).ConfigureAwait(false);
                Interlocked.Exchange(ref lastVerification,Now);
            }
            if(!transportReady()||epoch!=Volatile.Read(ref generation))
                throw new InvalidOperationException("驗證期間網絡發生變化，正在重新確認裝置回連。");
            Volatile.Write(ref verifiedGeneration,epoch);
            Interlocked.Exchange(ref lastLease,Now);
            failure=null;
        }
        catch(Exception error) when(error is HttpRequestException or IOException or JsonException or InvalidOperationException or OperationCanceledException)
        {
            ct.ThrowIfCancellationRequested();
            failure=error switch {
                InvalidOperationException => error.Message,
                OperationCanceledException => "裝置回連驗證超時，請檢查本機網絡及網關。",
                HttpRequestException {HttpRequestError:HttpRequestError.SecureConnectionError} => "與裝置網關的 TLS 握手未完成。請先檢查網絡，若持續失敗再核對網關證書。",
                HttpRequestException => "無法聯繫裝置授權網關，請檢查網絡和網關證書。",
                _ => "裝置網關沒有返回有效的續租或 SSH 驗證結果，請檢查網關服務。"
            };
            Invalidate();
            throw new InvalidOperationException(failure,error);
        }
        finally {check.Release();}
    }
}
