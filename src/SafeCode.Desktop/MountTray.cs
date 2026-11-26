using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;
using SafeCode.Core;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Input;
using System.Windows.Threading;
using Forms=System.Windows.Forms;

namespace SafeCode.Desktop;

internal sealed class MountTray : IDisposable
{
    private readonly Forms.NotifyIcon icon=new();
    private readonly Window panel;
    private readonly WebView2 view=new();
    private readonly Uri page=new(Path.Combine(AppContext.BaseDirectory,"Web","web","tray.html"));
    private readonly System.Drawing.Icon appIcon;
    private readonly DispatcherTimer timer=new(){Interval=TimeSpan.FromSeconds(1)};
    private readonly DispatcherTimer dismissTimer=new(){Interval=TimeSpan.FromMilliseconds(100)};
    private readonly Action<MountStatus> publish;
    private readonly Func<MountStatus> read;
    private readonly Func<object> state;
    private readonly Func<Task<object>> keys;
    private readonly Func<string,Task<object>> balance;
    private readonly Action show,exit;
    private readonly Func<Task> retry;
    private bool exiting,ready,disposed,released;

    public MountTray(Window owner,Func<MountStatus> read,Action show,Func<Task> retry,Action exit,
        Action<MountStatus> publish,Func<object> state,Func<Task<object>> keys,Func<string,Task<object>> balance,string dataDirectory)
    {
        this.read=read;this.publish=publish;this.state=state;this.show=show;this.retry=retry;this.exit=exit;
        this.keys=keys;this.balance=balance;
        string path=Path.Combine(AppContext.BaseDirectory,"Icons","safe-code-shortcut.ico");
        appIcon=File.Exists(path)?new System.Drawing.Icon(path):new System.Drawing.Icon(System.Drawing.SystemIcons.Shield,32,32);
        icon.Icon=appIcon;icon.Text="Safe Code";icon.Visible=true;
        view.CreationProperties=new CoreWebView2CreationProperties{UserDataFolder=Path.Combine(dataDirectory,"WebView")};
        view.DefaultBackgroundColor=System.Drawing.Color.FromArgb(250,251,249);
        view.CoreWebView2InitializationCompleted+=(_,e)=>
        {
            if(!e.IsSuccess)return;
            var core=view.CoreWebView2;
            core.Settings.IsStatusBarEnabled=false;core.Settings.AreDefaultContextMenusEnabled=false;
            core.Settings.AreDevToolsEnabled=false;core.Settings.IsZoomControlEnabled=false;
            core.NavigationStarting+=(_,n)=>n.Cancel=!Uri.TryCreate(n.Uri,UriKind.Absolute,out var uri)||uri!=page;
            core.NewWindowRequested+=(_,n)=>n.Handled=true;
            core.WebMessageReceived+=OnMessage;
        };
        view.Source=page;
        panel=new Window{Width=388,Height=220,WindowStyle=WindowStyle.None,ResizeMode=ResizeMode.NoResize,
            ShowInTaskbar=false,Topmost=true,Content=view,UseLayoutRounding=true};
        panel.Deactivated+=(_,_)=>{if(!disposed){released=false;dismissTimer.Start();}};
        dismissTimer.Tick+=(_,_)=>
        {
            if(panel.IsActive||!panel.IsVisible){dismissTimer.Stop();return;}
            if(Forms.Control.MouseButtons!=Forms.MouseButtons.None){released=false;return;}
            if(!released){released=true;return;}
            dismissTimer.Stop();panel.Hide();
        };
        panel.PreviewKeyDown+=(_,e)=>{if(e.Key==Key.Escape)panel.Hide();};
        icon.MouseUp+=(_,e)=>owner.Dispatcher.InvokeAsync(()=>{if(e.Button is Forms.MouseButtons.Left or Forms.MouseButtons.Right)Open();});
        icon.DoubleClick+=(_,_)=>owner.Dispatcher.InvokeAsync(show);
        timer.Tick+=(_,_)=>{var m=read();publish(m);if(panel.IsVisible)Render();};
        timer.Start();
    }

    private Task Script(string text)=>!disposed&&ready?view.ExecuteScriptAsync(text):Task.CompletedTask;
    private void Render(bool opened=false)=>_=Script("window.trayStatus("+JsonSerializer.Serialize(state(),JsonDefaults.Options)+","+(opened?"true":"false")+")");
    private async void OnMessage(object? sender,CoreWebView2WebMessageReceivedEventArgs e)
    {
        if(disposed||!Uri.TryCreate(e.Source,UriKind.Absolute,out var origin)||origin!=page)return;
        using var message=JsonDocument.Parse(e.WebMessageAsJson);
        var data=message.RootElement;
        switch(data.GetProperty("op").GetString())
        {
            case "ready":ready=true;Render(true);break;
            case "home":panel.Hide();show();break;
            case "close":panel.Hide();break;
            case "quit":if(!exiting)exit();break;
            case "reconnect":await retry();break;
            case "size":panel.Height=Math.Clamp(data.GetProperty("height").GetInt32(),120,SystemParameters.WorkArea.Height);Position();break;
            case "keys":
            case "balance":
                bool listing=data.GetProperty("op").GetString()=="keys";
                int generation=data.GetProperty("generation").GetInt32();
                object? result;
                try{result=listing?await keys():await balance(data.GetProperty("id").GetString()!);}
                catch(Exception){result=null;}
                string call=listing?$"trayKeys({generation},":$"trayBalance({generation},{data.GetProperty("index").GetInt32()},";
                if(!disposed)await Script(call+JsonSerializer.Serialize(result,JsonDefaults.Options)+")");
                break;
        }
    }
    private void Position()
    {
        var area=SystemParameters.WorkArea;
        panel.Left=Math.Max(area.Left,area.Right-panel.Width-12);
        panel.Top=Math.Max(area.Top,area.Bottom-panel.Height-12);
    }
    public void SetExiting(bool value){exiting=value;if(panel.IsVisible)_=Script("document.getElementById('quit').disabled="+(exiting?"true":"false"));}
    public void Open(){if(disposed)return;dismissTimer.Stop();Position();panel.Show();panel.Activate();Render(true);}
    public void NotifyHidden()=>icon.ShowBalloonTip(2000,"Safe Code","已轉入系統匣，背景任務繼續執行。",Forms.ToolTipIcon.Info);
    public void Dispose(){disposed=true;timer.Stop();dismissTimer.Stop();icon.Visible=false;icon.Dispose();appIcon.Dispose();panel.Close();view.Dispose();}
}
