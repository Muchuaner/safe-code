namespace SafeCode.Core;

public sealed record TrayIntegrationSection(bool Visible,string State,string Duration,string Detail,string Tooltip);

/// <summary>托盘详情只做缓存展示；活动状态决定是否显示，时长由 start/stop 计算。</summary>
public static class IntegrationTrayPresenter
{
    public static TrayIntegrationSection Codex(bool running,bool connected,DateTimeOffset? started,DateTimeOffset? stopped,IntegrationUsage? usage,string source,DateTimeOffset now)
    {
        if(!running)return new(false,"","","","");
        string state=connected?"通道已连接":"重连中";
        string duration="时长 "+FormatDuration(started,stopped,now);
        string detail,tooltip;
        if(usage?.Available==true)
        {
            duration+=" · "+FormatTokens(usage.Total)+" tokens";
            detail="入 "+FormatTokens(usage.Input)+" · 出 "+FormatTokens(usage.Output)+" · 缓存 "+FormatTokens(usage.CacheRead);
            tooltip="本次接入累计。input 已包含 cached，不重复计入总量。完整值：输入 "+usage.Input.ToString("N0")+"，输出 "+usage.Output.ToString("N0")+"，缓存 "+usage.CacheRead.ToString("N0")+"。来源："+source;
        }
        else
        {
            duration+=" · Tokens 暂不可用";
            detail=usage?.Note??"暂不可用/待统计";
            tooltip=usage?.Note??"没有可读的真实用量来源，不估算。";
        }
        return new(true,state,duration,detail,tooltip);
    }
    public static TrayIntegrationSection Desktop(bool active,DateTimeOffset? started,DateTimeOffset? stopped,IntegrationUsage? usage,string source,DateTimeOffset now)
    {
        if(!active)return new(false,"","","","");
        string duration="时长 "+FormatDuration(started,stopped,now);string detail,tooltip;
        if(usage?.Available==true)
        {
            duration+=" · "+FormatTokens(usage.Total)+" tokens";
            detail="入 "+FormatTokens(usage.Input)+" · 出 "+FormatTokens(usage.Output)+" · 缓存读 "+FormatTokens(usage.CacheRead)+" / 写 "+FormatTokens(usage.CacheWrite);
            tooltip="本次接入累计。input + cache read + cache write + output。完整值：输入 "+usage.Input.ToString("N0")+"，输出 "+usage.Output.ToString("N0")+"，缓存读 "+usage.CacheRead.ToString("N0")+"，缓存写 "+usage.CacheWrite.ToString("N0")+"。来源："+source;
        }
        else
        {
            duration+=" · Tokens 暂不可用";
            detail=usage?.Note??"暂不可用/待统计";
            tooltip=usage?.Note??"官方客户端未留下可读 token 元数据；不估算。";
        }
        return new(true,"VPS 通道已连接",duration,detail,tooltip);
    }
    public static string FormatDuration(DateTimeOffset? started,DateTimeOffset? stopped,DateTimeOffset now)
    {
        if(started is null)return "--:--:--";
        var end=stopped??now;var value=end<started.Value?TimeSpan.Zero:end-started.Value;
        return ((int)value.TotalHours).ToString("00")+value.ToString(@"\:mm\:ss");
    }
    public static string FormatTokens(long value)=>value>=1_000_000?(value/1_000_000d).ToString("0.0")+"M":value>=10_000?(value/1000d).ToString("0.0")+"k":value.ToString("N0");
}
