namespace SafeCode.Core;

public sealed record TrayIntegrationSection(bool Visible,string State,string Duration,string Detail,string Tooltip);

/// <summary>系統匣詳情只做快取展示；活動狀態決定是否顯示，時長由 start/stop 計算。</summary>
public static class IntegrationTrayPresenter
{
    public static TrayIntegrationSection Codex(bool running,bool connected,DateTimeOffset? started,DateTimeOffset? stopped,IntegrationUsage? usage,string source,DateTimeOffset now)
    {
        if(!running)return new(false,"","","","");
        string state=connected?"通道已連線":"重連中";
        string duration="時長 "+FormatDuration(started,stopped,now);
        string detail,tooltip;
        if(usage?.Available==true)
        {
            duration+=" · "+FormatTokens(usage.Total)+" tokens";
            detail="入 "+FormatTokens(usage.Input)+" · 出 "+FormatTokens(usage.Output)+" · 快取 "+FormatTokens(usage.CacheRead);
            tooltip="本次接入累計。input 已包含 cached，不重複計入總量。完整值：輸入 "+usage.Input.ToString("N0")+"，輸出 "+usage.Output.ToString("N0")+"，快取 "+usage.CacheRead.ToString("N0")+"。來源："+source;
        }
        else
        {
            duration+=" · Tokens 暫不可用";
            detail=usage?.Note??"暫不可用/待統計";
            tooltip=usage?.Note??"沒有可讀的真實用量來源，不估算。";
        }
        return new(true,state,duration,detail,tooltip);
    }
    public static TrayIntegrationSection Desktop(bool active,DateTimeOffset? started,DateTimeOffset? stopped,IntegrationUsage? usage,string source,DateTimeOffset now)
    {
        if(!active)return new(false,"","","","");
        string duration="時長 "+FormatDuration(started,stopped,now);string detail,tooltip;
        if(usage?.Available==true)
        {
            duration+=" · "+FormatTokens(usage.Total)+" tokens";
            detail="入 "+FormatTokens(usage.Input)+" · 出 "+FormatTokens(usage.Output)+" · 快取讀 "+FormatTokens(usage.CacheRead)+" / 寫 "+FormatTokens(usage.CacheWrite);
            tooltip="本次接入累計。input + cache read + cache write + output。完整值：輸入 "+usage.Input.ToString("N0")+"，輸出 "+usage.Output.ToString("N0")+"，快取讀 "+usage.CacheRead.ToString("N0")+"，快取寫 "+usage.CacheWrite.ToString("N0")+"。來源："+source;
        }
        else
        {
            duration+=" · Tokens 暫不可用";
            detail=usage?.Note??"暫不可用/待統計";
            tooltip=usage?.Note??"官方客戶端未留下可讀 token 元數據；不估算。";
        }
        return new(true,"VPS 通道已連線",duration,detail,tooltip);
    }
    public static string FormatDuration(DateTimeOffset? started,DateTimeOffset? stopped,DateTimeOffset now)
    {
        if(started is null)return "--:--:--";
        var end=stopped??now;var value=end<started.Value?TimeSpan.Zero:end-started.Value;
        return ((int)value.TotalHours).ToString("00")+value.ToString(@"\:mm\:ss");
    }
    public static string FormatTokens(long value)=>value>=1_000_000?(value/1_000_000d).ToString("0.0")+"M":value>=10_000?(value/1000d).ToString("0.0")+"k":value.ToString("N0");
}
