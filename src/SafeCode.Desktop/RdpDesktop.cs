using System.ComponentModel;
using System.Drawing;
using F = System.Windows.Forms;

namespace SafeCode.Desktop;

// Windows 自带的微软远程桌面组件。密码经 COM 内存接口传入，不使用文件或命令行。
internal sealed class RdpDesktop : F.Form
{
    private readonly RdpControl control=new();
    private dynamic client=null!;
    private DesktopResolution currentResolution=new();
    private bool adaptiveScaling, zoomSupported, connectionConfigured;
    internal bool CanEnableAdaptiveScaling=>!connectionConfigured||zoomSupported;
    public RdpDesktop(DesktopResolution? resolution=null):this(resolution,false){}
    public RdpDesktop(DesktopResolution? resolution,bool adaptiveScaling)
    {
        this.adaptiveScaling=adaptiveScaling;
        Text="Safe Code · 远程桌面";AutoScaleMode=F.AutoScaleMode.None;StartPosition=F.FormStartPosition.Manual;currentResolution=resolution??new();ResizeForResolution(currentResolution);Shown+=(_,_)=>ResizeForResolution(currentResolution);
        ((ISupportInitialize)control).BeginInit();Controls.Add(control);((ISupportInitialize)control).EndInit();
        ClientSizeChanged+=(_,_)=>LayoutViewport();
        DpiChanged+=(_,_)=>LayoutViewport();
        LayoutViewport();
        FormClosing+=(_,_)=>{try{client.Disconnect();}catch{}};
    }
    public void Connect(string host,uint port,string username,string password,DesktopResolution? resolution=null)
    {
        currentResolution=resolution??new();ResizeForResolution(currentResolution);
        client=control.Client;client.Server=host;client.UserName=username;client.DesktopWidth=(resolution??new()).Width;client.DesktopHeight=(resolution??new()).Height;
        zoomSupported=RdpDisplayScaling.TryEnableZoom((object)client);connectionConfigured=true;
        if(adaptiveScaling&&!zoomSupported)throw new InvalidOperationException("当前 Windows 远程桌面组件不支持放大缩放。请关闭“云桌面自适应缩放”后重试，或更新 Windows 后使用。");
        dynamic advanced=client.AdvancedSettings7;
        advanced.RDPPort=(int)port;advanced.EnableCredSspSupport=false;
        // 远端真实性由外层严格核对 SSH 主机密钥的隧道提供。
        advanced.AuthenticationLevel=0;advanced.ClearTextPassword=password;advanced.SmartSizing=true;
        advanced.RedirectClipboard=true;client.Connect();
    }
    internal void SetAdaptiveScaling(bool enabled)
    {
        if(enabled&&!CanEnableAdaptiveScaling)throw new InvalidOperationException("当前 Windows 远程桌面组件不支持放大缩放，请更新 Windows 后重试。");
        adaptiveScaling=enabled;LayoutViewport();
    }
    private void LayoutViewport()
    {
        if(WindowState==F.FormWindowState.Minimized||IsDisposed)return;
        var bounds=RdpDisplayScaling.Fit(ClientSize,currentResolution,adaptiveScaling);
        if(bounds.Width>0&&bounds.Height>0&&control.Bounds!=bounds)control.Bounds=bounds;
    }
    internal void ResizeForResolution(DesktopResolution resolution)
    {
        var area=F.Screen.FromPoint(Visible?new Point(Left+Width/2,Top+Height/2):F.Cursor.Position).WorkingArea;
        var frame=SizeFromClientSize(Size.Empty);int availableWidth=Math.Max(1,area.Width-frame.Width-24),availableHeight=Math.Max(1,area.Height-frame.Height-24);
        double scale=Math.Min(1,Math.Min((double)availableWidth/resolution.Width,(double)availableHeight/resolution.Height));
        if(WindowState!=F.FormWindowState.Normal)WindowState=F.FormWindowState.Normal;
        ClientSize=new Size(Math.Max(1,(int)Math.Floor(resolution.Width*scale)),Math.Max(1,(int)Math.Floor(resolution.Height*scale)));
        Location=new Point(area.Left+(area.Width-Width)/2,area.Top+(area.Height-Height)/2);
    }
    internal bool ApplyResolution(DesktopResolution resolution)
    {
        ResizeForResolution(resolution);if(currentResolution==resolution)return true;
        if(client is null)return false;dynamic active=client!;
        try{if(active.Connected==1){active.UpdateSessionDisplaySettings((uint)resolution.Width,(uint)resolution.Height,(uint)resolution.Width,(uint)resolution.Height,0u,100u,100u);currentResolution=resolution;LayoutViewport();return true;}}catch(System.Runtime.InteropServices.COMException){}catch(Microsoft.CSharp.RuntimeBinder.RuntimeBinderException){}
        return false;
    }
    private sealed class RdpControl : F.AxHost
    {
        public RdpControl():base("A0C63C30-F08D-4AB4-907C-34905D770C7D"){}
        public object Client=>GetOcx()??throw new InvalidOperationException("无法创建 Windows 远程桌面组件。");
    }
}
