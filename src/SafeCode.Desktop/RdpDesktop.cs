using System.ComponentModel;
using F = System.Windows.Forms;

namespace SafeCode.Desktop;

// Windows 自带的微软远程桌面组件。密码经 COM 内存接口传入，不使用文件或命令行。
internal sealed class RdpDesktop : F.Form
{
    private readonly RdpControl control=new(){Dock=F.DockStyle.Fill};
    private dynamic client=null!;
    public RdpDesktop()
    {
        Text="Safe Code · 远程桌面";Width=1280;Height=840;StartPosition=F.FormStartPosition.CenterScreen;
        ((ISupportInitialize)control).BeginInit();Controls.Add(control);((ISupportInitialize)control).EndInit();
        FormClosing+=(_,_)=>{try{client.Disconnect();}catch{}};
    }
    public void Connect(string host,uint port,string username,string password)
    {
        client=control.Client;client.Server=host;client.UserName=username;client.DesktopWidth=1280;client.DesktopHeight=800;
        dynamic advanced=client.AdvancedSettings7;
        advanced.RDPPort=(int)port;advanced.EnableCredSspSupport=false;
        // 远端真实性由外层严格核对 SSH 主机密钥的隧道提供。
        advanced.AuthenticationLevel=0;advanced.ClearTextPassword=password;advanced.SmartSizing=true;
        advanced.RedirectClipboard=true;client.Connect();
    }
    private sealed class RdpControl : F.AxHost
    {
        public RdpControl():base("A0C63C30-F08D-4AB4-907C-34905D770C7D"){}
        public object Client=>GetOcx()??throw new InvalidOperationException("无法创建 Windows 远程桌面组件。");
    }
}
