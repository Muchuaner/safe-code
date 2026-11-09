using System.Text.RegularExpressions;
namespace SafeCode.Desktop;

internal sealed record ReleaseNumber(System.Version Base,bool Beta,int Revision):IComparable<ReleaseNumber>
{
    public static ReleaseNumber? Parse(string value)
    {
        var match=Regex.Match(value,@"^v?(?<beta>Beta-)?(?<major>\d{1,4})\.(?<minor>\d{1,4})\.(?<patch>\d{1,4})(?:\.(?<revision>\d{1,4}))?$");
        if(!match.Success)return null;bool beta=match.Groups["beta"].Success;
        if(!beta&&match.Groups["revision"].Success)return null;
        return new(new(int.Parse(match.Groups["major"].Value),int.Parse(match.Groups["minor"].Value),int.Parse(match.Groups["patch"].Value)),beta,match.Groups["revision"].Success?int.Parse(match.Groups["revision"].Value):0);
    }
    public int CompareTo(ReleaseNumber? other)
    {
        if(other==null)return 1;int core=Base.CompareTo(other.Base);if(core!=0)return core;
        if(Beta!=other.Beta)return Beta?-1:1;return Revision.CompareTo(other.Revision);
    }
}
