using System.Net;
using System.Net.Sockets;
using System.Text.Json;

namespace SafeCode.Core;
public static class IpIntelligenceService
{
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(35) };
    public static async Task<IpIntelligence> QueryAsync(string ip, CancellationToken ct = default)
    {
        if (!IPAddress.TryParse(ip, out var address) || address.AddressFamily != AddressFamily.InterNetwork) throw new ArgumentException("IP 情報暫僅支援 IPv4。");
        var b=address.GetAddressBytes();
        if(IPAddress.IsLoopback(address)||b[0] is 0 or 10||b[0]>=224||(b[0]==192&&b[1]==168)||(b[0]==172&&b[1]>=16&&b[1]<=31)||(b[0]==169&&b[1]==254))throw new ArgumentException("僅查詢公網 IP。");
        string json=await Http.GetStringAsync("https://ip.net.coffee/api/ip/lookup/"+Uri.EscapeDataString(ip),ct);
        return ParseLookup(ip,json);
    }
    /// <summary>與 Net.Coffee 多源彙總的原則一致：先取國家多數，再取該國的城市多數；註冊地不作為物理位置。</summary>
    public static IpIntelligence ParseLookup(string ip,string json)
    {
        using var document=JsonDocument.Parse(json);var data=document.RootElement;
        static string Get(JsonElement item,string key)=>item.TryGetProperty(key,out var value)&&value.ValueKind==JsonValueKind.String?value.GetString()??"":"";
        string country=Get(data,"country"),code=Get(data,"countryCode").ToLowerInvariant(),city=Get(data,"city");bool conflict=false;string source="簡表";
        if(data.TryGetProperty("geo_sources",out var sources)&&sources.ValueKind==JsonValueKind.Array)
        {
            var valid=sources.EnumerateArray().Where(x=>Get(x,"country_code").Length>0).ToList();
            var groups=valid.GroupBy(x=>Get(x,"country_code").ToLowerInvariant()).OrderByDescending(x=>x.Count()).ToList();
            conflict=groups.Count>1;
            if(groups.Count>0&&(groups.Count==1||groups[0].Count()>groups[1].Count()))
            {
                var chosen=groups[0].ToList();code=groups[0].Key;country=Get(chosen[0],"country");
                city=chosen.Select(x=>Get(x,"city")).Where(x=>x.Length>0).GroupBy(x=>x).OrderByDescending(x=>x.Count()).ThenBy(x=>x.Key.Length).FirstOrDefault()?.Key??Get(chosen[0],"region");
                source="多源綜合";
            }
            else if(conflict){source="來源有分歧";code="";city="";country="定位待確認";}
        }
        var countries=new Dictionary<string,string>{{"Japan","日本"},{"United States","美國"},{"Hong Kong","香港"},{"Singapore","新加坡"},{"Germany","德國"},{"United Kingdom","英國"},{"South Korea","韓國"}};
        var cities=new Dictionary<string,string>{{"Tokyo","東京"},{"New York","紐約"},{"Los Angeles","洛杉磯"},{"Singapore","新加坡"}};
        string location=string.Join(" · ",new[]{countries.GetValueOrDefault(country,country),cities.GetValueOrDefault(city,city)}.Where(x=>x.Length>0));
        int? score=data.TryGetProperty("trust_score",out var number)&&number.TryGetInt32(out int n)&&n is >=0 and <=100?n:null;
        bool subnet=Get(data,"cidr").Contains('/')&&!Get(data,"cidr").EndsWith("/32");
        return new(ip,location,score,code,city,subnet,source,conflict);
    }
}
