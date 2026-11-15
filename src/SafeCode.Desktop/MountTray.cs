using SafeCode.Core;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using Forms=System.Windows.Forms;

namespace SafeCode.Desktop;

internal sealed class MountTray : IDisposable
{
    private readonly Forms.NotifyIcon icon=new();
    private readonly Window panel;
    private readonly TextBlock status=new(),summary=new(),duration=new(),sent=new(),received=new(),files=new();
    private readonly DispatcherTimer timer=new(){Interval=TimeSpan.FromSeconds(1)};
    private readonly Func<MountStatus> read;
    private readonly Action<MountStatus> publish;
    private readonly Func<HostControlStatus?>? readHost;
    private readonly Func<bool>? readBridge;
    private readonly System.Drawing.Icon appIcon;
    public MountTray(Window owner,Func<MountStatus> read,Action show,Func<Task> retry,Action exit,Action<MountStatus> publish,Func<HostControlStatus?>? readHost=null,Func<bool>? readBridge=null)
    {
        this.read=read;this.publish=publish;this.readHost=readHost;this.readBridge=readBridge;
        string path=Path.Combine(AppContext.BaseDirectory,"Icons","safe-code-shortcut.ico");
        appIcon=File.Exists(path)?new System.Drawing.Icon(path):new System.Drawing.Icon(System.Drawing.SystemIcons.Shield,32,32);
        icon.Icon=appIcon;icon.Text="Safe Code · 挂载守护";icon.Visible=true;
        var content=new StackPanel{Margin=new Thickness(14,12,14,13)};
        var header=new Grid{Margin=new Thickness(0,0,0,12)};
        header.ColumnDefinitions.Add(new ColumnDefinition());header.ColumnDefinitions.Add(new ColumnDefinition{Width=GridLength.Auto});
        header.Children.Add(new TextBlock{Text="Safe Code",FontSize=14,FontWeight=FontWeights.SemiBold,Foreground=Brush("#2f7158"),VerticalAlignment=VerticalAlignment.Center});
        status.FontSize=10;status.VerticalAlignment=VerticalAlignment.Center;status.HorizontalAlignment=HorizontalAlignment.Right;Grid.SetColumn(status,1);header.Children.Add(status);content.Children.Add(header);
        var overview=new Grid{Margin=new Thickness(0,0,0,11)};
        overview.ColumnDefinitions.Add(new ColumnDefinition());overview.ColumnDefinitions.Add(new ColumnDefinition{Width=GridLength.Auto});
        summary.FontSize=11;summary.Foreground=Brush("#536359");overview.Children.Add(summary);
        duration.FontSize=11;duration.Foreground=Brush("#536359");duration.HorizontalAlignment=HorizontalAlignment.Right;Grid.SetColumn(duration,1);overview.Children.Add(duration);content.Children.Add(overview);
        var metrics=new Grid{Margin=new Thickness(0)};
        for(int i=0;i<3;i++)metrics.ColumnDefinitions.Add(new ColumnDefinition());
        var labels=new[]{"发送","接收","读写"};var values=new[]{sent,received,files};
        for(int i=0;i<3;i++)
        {
            var cell=new StackPanel{Margin=new Thickness(0,9,0,9)};
            cell.Children.Add(new TextBlock{Text=labels[i],FontSize=10,Foreground=Brush("#7b887f"),HorizontalAlignment=HorizontalAlignment.Center,Margin=new Thickness(0,0,0,4)});
            values[i].FontSize=13;values[i].FontWeight=FontWeights.SemiBold;values[i].Foreground=Brush("#354c3e");values[i].HorizontalAlignment=HorizontalAlignment.Center;
            values[i].TextTrimming=TextTrimming.CharacterEllipsis;values[i].MaxWidth=68;cell.Children.Add(values[i]);
            var border=new Border{Child=cell,BorderBrush=Brush("#e3eae3"),BorderThickness=new Thickness(i==0?0:1,0,0,0)};Grid.SetColumn(border,i);metrics.Children.Add(border);
        }
        content.Children.Add(new Border{Child=metrics,Background=Brush("#f5f8f4"),CornerRadius=new CornerRadius(5)});
        var actions=new Grid{Margin=new Thickness(0,11,0,0)};
        for(int i=0;i<3;i++)actions.ColumnDefinitions.Add(new ColumnDefinition());
        AddButton(actions,0,"主界面","显示主界面",()=>{panel!.Hide();show();},true);
        AddButton(actions,1,"重连","立即尝试恢复挂载连接",async()=>{await retry();Update();},false);
        AddButton(actions,2,"安全退出","同步文件并卸载目录后退出",()=>{panel!.Hide();exit();},false);
        content.Children.Add(actions);
        panel=new Window{Width=252,SizeToContent=SizeToContent.Height,WindowStyle=WindowStyle.None,ResizeMode=ResizeMode.NoResize,ShowInTaskbar=false,Topmost=true,AllowsTransparency=true,Background=Brushes.Transparent,Content=new Border{Background=Brushes.White,BorderBrush=Brush("#dfe7df"),BorderThickness=new Thickness(1),CornerRadius=new CornerRadius(8),Child=content},FontFamily=new FontFamily("Segoe UI")};
        panel.Deactivated+=(_,_)=>panel.Hide();
        panel.PreviewKeyDown+=(_,e)=>{if(e.Key==System.Windows.Input.Key.Escape){panel.Hide();e.Handled=true;}};
        icon.MouseUp+=(_,e)=>owner.Dispatcher.InvokeAsync(()=>{if(e.Button is Forms.MouseButtons.Left or Forms.MouseButtons.Right)Open();});
        icon.DoubleClick+=(_,_)=>owner.Dispatcher.InvokeAsync(show);
        timer.Tick+=(_,_)=>Update();timer.Start();Update();
    }
    private static SolidColorBrush Brush(string color)=>new((Color)ColorConverter.ConvertFromString(color));
    private static void AddButton(Grid panel,int column,string text,string hint,Action clicked,bool primary)
    {
        var button=new Button{Content=text,ToolTip=hint,Height=28,Margin=new Thickness(column==0?0:5,0,0,0),Padding=new Thickness(2,0,2,0),Background=Brush(primary?"#347457":"#f0f4ef"),Foreground=primary?Brushes.White:Brush("#46634f"),BorderThickness=new Thickness(0),FontSize=11,Cursor=System.Windows.Input.Cursors.Hand};
        var border=new FrameworkElementFactory(typeof(Border));border.SetValue(Border.BackgroundProperty,new TemplateBindingExtension(Control.BackgroundProperty));border.SetValue(Border.CornerRadiusProperty,new CornerRadius(4));border.SetValue(Border.BorderBrushProperty,Brush("#347457"));border.SetValue(Border.BorderThicknessProperty,new Thickness(0));border.Name="surface";
        var presenter=new FrameworkElementFactory(typeof(ContentPresenter));presenter.SetValue(FrameworkElement.HorizontalAlignmentProperty,HorizontalAlignment.Center);presenter.SetValue(FrameworkElement.VerticalAlignmentProperty,VerticalAlignment.Center);border.AppendChild(presenter);
        var template=new ControlTemplate(typeof(Button)){VisualTree=border};
        var hover=new Trigger{Property=UIElement.IsMouseOverProperty,Value=true};hover.Setters.Add(new Setter(Control.BackgroundProperty,Brush(primary?"#285d46":"#e3ebe1")));template.Triggers.Add(hover);
        var focus=new Trigger{Property=UIElement.IsKeyboardFocusedProperty,Value=true};focus.Setters.Add(new Setter(Border.BorderThicknessProperty,new Thickness(1),"surface"));template.Triggers.Add(focus);
        var pressed=new Trigger{Property=Button.IsPressedProperty,Value=true};pressed.Setters.Add(new Setter(UIElement.OpacityProperty,0.8));template.Triggers.Add(pressed);button.Template=template;
        button.Click+=(_,_)=>clicked();Grid.SetColumn(button,column);panel.Children.Add(button);
    }
    public void Open()
    {
        Update();panel.Show();var area=SystemParameters.WorkArea;panel.Left=Math.Max(area.Left,area.Right-panel.ActualWidth-12);panel.Top=Math.Max(area.Top,area.Bottom-panel.ActualHeight-12);panel.Activate();
    }
    public void NotifyHidden()=>icon.ShowBalloonTip(2000,"Safe Code","已转入托盘，后台任务继续运行。",Forms.ToolTipIcon.Info);
    private void Update()
    {
        var value=read();publish(value);
        status.Text=value.State=="connected"?"守护中":value.State=="reconnecting"?"重连中":value.State=="attention"?"需处理":"未挂载";
        status.ToolTip=value.Message;status.Foreground=Brush(value.State=="connected"?"#2f7158":value.State=="idle"?"#718075":"#a57430");
        summary.Text="目录  "+value.Count;var elapsed=TimeSpan.FromSeconds(value.DurationSeconds);duration.Text="时长  "+(int)elapsed.TotalHours+elapsed.ToString(@"\:mm\:ss");
        sent.Text=FormatBytes(value.BytesToServer);received.Text=FormatBytes(value.BytesToLocal);files.Text=value.CompletedFiles.ToString("N0")+" 次";
        sent.ToolTip="发送到服务器："+value.BytesToServer.ToString("N0")+" 字节";received.ToolTip="接收到本机："+value.BytesToLocal.ToString("N0")+" 字节";files.ToolTip="完成文件读写："+value.CompletedFiles.ToString("N0")+" 次"+(value.ActiveTransfers>0?"；正在传输 "+value.ActiveTransfers+" 个":"");
        var host=readHost?.Invoke();
        if(host?.Enabled==true){status.Text=host.State=="connected"?"本机控制开":host.State=="attention"?"本机需处理":"本机重连中";status.ToolTip=value.Message+"\n"+host.Message;status.Foreground=Brush(host.State=="connected"?"#2f7158":"#a57430");}
        bool bridgeRunning=readBridge?.Invoke()==true;
        if(bridgeRunning&&host?.Enabled!=true){status.Text="模型接入开";status.ToolTip=value.Message+"\nCodex 接入 Claude 适配器运行中";status.Foreground=Brush("#2f7158");}
        icon.Text="Safe Code · "+value.Count+" 个目录 · "+(value.State=="connected"?"守护中":value.State=="reconnecting"?"重连中":value.State=="attention"?"需要处理":"未挂载")+(host?.Enabled==true?" · 本机控制已启用":"")+(bridgeRunning?" · Claude适配器运行中":"");
    }
    private static string FormatBytes(long bytes)=>bytes>=1024L*1024*1024?(bytes/(1024d*1024*1024)).ToString("0.00")+" GB":bytes>=1024*1024?(bytes/(1024d*1024)).ToString("0.0")+" MB":bytes>=1024?(bytes/1024d).ToString("0.0")+" KB":bytes+" B";
    public void Dispose(){timer.Stop();icon.Visible=false;icon.Dispose();appIcon.Dispose();panel.Close();}
}
