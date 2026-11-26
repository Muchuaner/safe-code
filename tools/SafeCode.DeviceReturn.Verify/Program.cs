using SafeCode.Desktop;
using System.Text.Json;

bool transport=true, revoked=false, ssh=true, exited=false;
int renewals=0, logins=0;
Action? duringVerification=null;
var path=new DeviceReturnPath(Renew,Verify,()=>transport,()=>exited,CancellationToken.None);
Task<JsonElement> Renew(CancellationToken ct)
{
    renewals++;
    if(revoked)throw new InvalidOperationException("裝置授權已撤銷");
    return Task.FromResult(JsonSerializer.SerializeToElement(new {ready=true}));
}
Task<string> Verify(CancellationToken ct)
{
    logins++;
    if(!ssh)throw new InvalidOperationException("SSH 未能回連");
    duringVerification?.Invoke();
    return Task.FromResult("fixture");
}
void Require(bool ok,string name)
{
    if(!ok)throw new Exception(name);
    Console.WriteLine("PASS "+name);
}
async Task Rejected(string reason)
{
    try {await path.EnsureAsync(false,CancellationToken.None);}
    catch(InvalidOperationException error) when(error.Message.Contains(reason) && path.Problem?.Contains(reason)==true)
    {Console.WriteLine("PASS "+reason);return;}
    throw new Exception("Unexpected return-path state");
}
await path.EnsureAsync(true,CancellationToken.None);
Require(path.Problem==null && renewals==1 && logins==1,"initial lease and SSH verified");
await path.EnsureAsync(false,CancellationToken.None);
Require(logins==1 && renewals==1,"reuse fresh evidence");
path.Invalidate();
Require(path.Problem!=null,"wake invalidates previous verification immediately");
revoked=true;
await Rejected("授權已撤銷");
revoked=false;transport=false;
await Rejected("FRP");
transport=true;ssh=false;
await Rejected("SSH");
ssh=true;duringVerification=path.Invalidate;
await Rejected("網絡發生變化");
duringVerification=null;
await path.EnsureAsync(false,CancellationToken.None);
Require(path.Problem==null,"same pairing recovers");
exited=true;
await Rejected("進程已退出");
