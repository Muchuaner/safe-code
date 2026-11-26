using System.IO;
using System.Runtime.InteropServices;
using SafeCode.Core;
using F = System.Windows.Forms;

namespace SafeCode.Desktop;

internal sealed partial class RdpDesktop
{
    private const int ImageHotkey = 0x5344;
    private readonly F.ToolStripSplitButton imageButton = new("貼上本機圖片") { Enabled = false,
        ToolTipText = "將本機剪貼板的圖片貼上到當前雲桌面輸入框。右側箭頭可設定 Ctrl+V。" };
    private readonly F.ToolStripMenuItem imageShortcut = new("Ctrl+V 貼上本機圖片") { CheckOnClick = true };
    private Func<IReadOnlyList<DesktopImagePart>, CancellationToken, Task>? sendImage;
    private CancellationTokenSource? imageOperation;
    private bool imageHotkeyRegistered, imageClosing, hotkeyConflict;
    private uint imageFocusEpoch;
    private uint? lastImageSequence; // avoid resending an unchanged image to the same desktop

    private void ConfigureImagePaste(Func<IReadOnlyList<DesktopImagePart>, CancellationToken, Task>? sink)
    {
        sendImage = sink;
        toolbar.Items.Insert(toolbar.Items.IndexOf(resizeMenu) + 1, imageButton);
        imageButton.DropDownItems.Add(imageShortcut);
        imageButton.ButtonClick += (_, _) => { control.Focus(); _ = PasteImageAsync(true); };
        imageShortcut.CheckedChanged += (_, _) =>
        {
            hotkeyConflict = false;
            if (!imageShortcut.Checked) ReleaseImageHotkey();
            else BeginInvoke(() => { control.Focus(); UpdateImageHotkey(); });
        };
        control.GotFocus += (_, _) => UpdateImageHotkey();
        control.LostFocus += (_, _) => { if (!control.ContainsFocus) { imageFocusEpoch++; ReleaseImageHotkey(); } };
    }

    private void ClipboardConnectionChanged(bool live)
    {
        if (!live)
        {
            imageOperation?.Cancel();
            ReleaseImageHotkey();
            lastImageSequence = null;
        }
        imageButton.Enabled = live && sendImage != null && !imageClosing && imageOperation == null;
        // UpdateDisplay calls us just before replacing its connected flag.
        if (live && IsHandleCreated) BeginInvoke(UpdateImageHotkey);
    }

    private bool OurWindow() => !imageClosing && IsHandleCreated && !IsDisposed && GetForegroundWindow() == Handle;
    private bool PasteTarget() => OurWindow() && client is not null && (int)client!.Connected == 1 && control.ContainsFocus;

    private void UpdateImageHotkey()
    {
        if (imageHotkeyRegistered || hotkeyConflict || !imageShortcut.Checked || !connected || sendImage == null ||
            !OurWindow() || !control.ContainsFocus) return;
        // ActiveX owns the focused child HWND. Claim Ctrl+V only while that child is foreground.
        imageHotkeyRegistered = RegisterHotKey(Handle, ImageHotkey, 0x4002, 0x56);
        if (!imageHotkeyRegistered)
        {
            hotkeyConflict = true;
            imageShortcut.Checked = false;
            displayStatus.Text = "Ctrl+V 被佔用，請用圖片按鈕";
        }
    }

    private void ReleaseImageHotkey()
    {
        if (imageHotkeyRegistered && IsHandleCreated) UnregisterHotKey(Handle, ImageHotkey);
        imageHotkeyRegistered = false;
    }

    private void StopImagePaste()
    {
        imageClosing = true;
        ReleaseImageHotkey();
        imageOperation?.Cancel();
    }

    private async Task PasteImageAsync(bool fromButton)
    {
        if (imageOperation != null || sendImage == null || imageClosing) return;
        using var op = new CancellationTokenSource();
        imageOperation = op;
        uint focusEpoch = imageFocusEpoch;
        try
        {
            if (!PasteTarget()) return;
            uint seq = GetClipboardSequenceNumber();
            // ActiveX advertises remote X11 selections back to Windows using its own hidden HWND.
            // Pasting those should continue to use the live remote clipboard, never upload them again.
            if (!fromButton && OwnedByThisClient()) { await SendPasteWhenReleased(op.Token, seq); return; }
            // Only shortcuts can reuse the remote clipboard. An explicit click always republishes.
            if (!fromButton && seq != 0 && lastImageSequence == seq)
            { await SendPasteWhenReleased(op.Token, seq); return; }
            ImageSnapshot? snapshot = null;
            for (int attempt = 0; ; attempt++)
            {
                try { snapshot = ClipboardImage.Read(Handle, fromButton); break; }
                catch (IOException) when (attempt < 3) { await Task.Delay(25, op.Token); if (!PasteTarget()) return; }
            }
            if (snapshot == null)
            {
                if (fromButton) displayStatus.Text = "本機剪貼板中沒有可貼上的圖片";
                else await SendPasteWhenReleased(op.Token, seq); // native text, rich text, files, and remote clipboard
                return;
            }
            displayStatus.Text = "正在傳送圖片…";
            var parts = await Task.Run(() => ClipboardImage.Prepare(snapshot), op.Token);
            if (!PasteTarget() || focusEpoch != imageFocusEpoch || (seq != 0 && GetClipboardSequenceNumber() != seq))
            { displayStatus.Text = "焦點或剪貼板已變化，請重試"; return; }
            await sendImage(parts, op.Token);
            op.Token.ThrowIfCancellationRequested();
            lastImageSequence = seq == 0 ? null : seq;
            // X11 ownership is acknowledged immediately, but LibreOffice’s generic X11 backend
            // caches the previous target list for two seconds (SelectionManager::getPasteDataTypes).
            displayStatus.Text = "圖片已傳送，等待雲桌面更新剪貼板…";
            await Task.Delay(TimeSpan.FromSeconds(2), op.Token);
            // The native RDP channel may announce the newly installed X11 clipboard back to Windows.
            // A sequence change during the send must never cause a second upload or a wrong-window paste.
            if (!PasteTarget() || focusEpoch != imageFocusEpoch) { displayStatus.Text = "圖片已就緒，請回到輸入框貼上"; return; }
            if (!await SendPasteWhenReleased(op.Token, seq, acceptRdpEcho: true)) return;
            displayStatus.Text = "已傳送貼上按鍵 · " + string.Join(" / ", parts.Select(p => p.Mime[6..].ToUpperInvariant()));
        }
        catch (OperationCanceledException) { if (!imageClosing && !IsDisposed) displayStatus.Text = "圖片貼上已取消或超時"; }
        catch (Exception ex)
        {
            if (!imageClosing && !IsDisposed)
            {
                displayStatus.Text = "未完成圖片貼上";
                if (OurWindow()) F.MessageBox.Show(this, ex.Message, "貼上本機圖片");
            }
        }
        finally { imageOperation = null; if (!imageClosing && !IsDisposed) imageButton.Enabled = connected && sendImage != null; }
    }

    private async Task<bool> SendPasteWhenReleased(CancellationToken ct, uint expectedSequence, bool acceptRdpEcho=false)
    {
        uint epoch = imageFocusEpoch;
        // Don't paste while the user's original Ctrl is physically held, or into another application.
        for (int i = 0; i < 120 && (GetAsyncKeyState(0x11) < 0 || GetAsyncKeyState(0x56) < 0); i++)
            await Task.Delay(15, ct);
        ct.ThrowIfCancellationRequested();
        if (!PasteTarget() || epoch != imageFocusEpoch || GetAsyncKeyState(0x11) < 0 || GetAsyncKeyState(0x56) < 0)
        { if (!imageClosing) displayStatus.Text = "請鬆開按鍵，在目標輸入框重試"; return false; }
        if (expectedSequence != 0 && GetClipboardSequenceNumber() != expectedSequence && !(acceptRdpEcho && OwnedByThisClient()))
        { displayStatus.Text = "剪貼板已變化，未自動貼上"; return false; }
        // Send only to this COM session. No system-wide SendInput / simulated input in other apps.
        var input = (IRdpScanKeys)control.Client;
        int hr = input.SendKeys(4, [0, 0, -1, -1], [0x1d, 0x2f, 0x2f, 0x1d]);
        Marshal.ThrowExceptionForHR(hr);
        return true;
    }

    [ComImport, Guid("2F079C4C-87B2-4AFD-97AB-20CDB43038AE"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IRdpScanKeys
    {
        // ABI order from mstscax: ten IMsTscNonScriptable methods, then IMsRdpClientNonScriptable.
        [PreserveSig] int PutClearText([MarshalAs(UnmanagedType.BStr)] string value);
        [PreserveSig] int PutPortablePassword([MarshalAs(UnmanagedType.BStr)] string value);
        [PreserveSig] int GetPortablePassword([MarshalAs(UnmanagedType.BStr)] out string value);
        [PreserveSig] int PutPortableSalt([MarshalAs(UnmanagedType.BStr)] string value);
        [PreserveSig] int GetPortableSalt([MarshalAs(UnmanagedType.BStr)] out string value);
        [PreserveSig] int PutBinaryPassword([MarshalAs(UnmanagedType.BStr)] string value);
        [PreserveSig] int GetBinaryPassword([MarshalAs(UnmanagedType.BStr)] out string value);
        [PreserveSig] int PutBinarySalt([MarshalAs(UnmanagedType.BStr)] string value);
        [PreserveSig] int GetBinarySalt([MarshalAs(UnmanagedType.BStr)] out string value);
        [PreserveSig] int ResetPassword();
        [PreserveSig] int NotifyRedirectDeviceChange(nuint wp, nint lp);
        [PreserveSig] int SendKeys(int count,
            [In, MarshalAs(UnmanagedType.LPArray, SizeParamIndex=0)] short[] keyUp,
            [In, MarshalAs(UnmanagedType.LPArray, SizeParamIndex=0)] int[] scan);
    }
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern short GetAsyncKeyState(int key);
    [DllImport("user32.dll")] private static extern uint GetClipboardSequenceNumber();
    private static bool OwnedByThisClient()
    {
        var owner = GetClipboardOwner();
        return owner != IntPtr.Zero && GetWindowThreadProcessId(owner, out var pid) != 0 && pid == (uint)Environment.ProcessId;
    }
    [DllImport("user32.dll")] private static extern IntPtr GetClipboardOwner();
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
}
