using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32;

namespace SafeCode.Desktop;

internal static class WindowsClaudeDesktop
{
    private const string PackageFamily = "Claude_pzs8sxrjxfjjc";
    private const string DownloadPage = "https://claude.com/download";

    // Enumerate current-user launch entries. A leftover package data directory is
    // not an installed app, and the installer file name doesn't identify its format.
    internal static string? RegisteredAppId()
    {
        var shellType = Type.GetTypeFromProgID("Shell.Application", throwOnError: true)!;
        dynamic shell = Activator.CreateInstance(shellType)!;
        dynamic folder = shell.NameSpace("shell:AppsFolder")
            ?? throw new IOException("无法读取 Windows 应用列表，请从开始菜单检查 Claude Desktop。");
        foreach (dynamic item in folder.Items())
        {
            string? id = item.Path as string;
            if (id?.StartsWith(PackageFamily + "!", StringComparison.OrdinalIgnoreCase) == true)
                return id;
        }
        return null;
    }

    private static IEnumerable<string> ExecutableCandidates()
    {
        foreach (var hive in new[] { Registry.CurrentUser, Registry.LocalMachine })
        {
            using var app = hive.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\App Paths\Claude.exe");
            if (app?.GetValue(null) is string registered) yield return registered.Trim('"');
            using var handler = hive.OpenSubKey(@"Software\Classes\claude\shell\open\command");
            if (handler?.GetValue(null) is string command)
            {
                // Only inspect the executable token. Never replay protocol-handler arguments.
                command = Environment.ExpandEnvironmentVariables(command.Trim());
                int boundary = command.StartsWith('"') ? command.IndexOf('"', 1) : command.IndexOf(".exe", StringComparison.OrdinalIgnoreCase);
                if (boundary > 0)
                    yield return command.StartsWith('"') ? command[1..boundary] : command[..(boundary + 4)];
            }
        }

        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var programFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
        foreach (var root in new[] { Path.Combine(local, "AnthropicClaude"), Path.Combine(local, "Programs", "Claude"),
                                     Path.Combine(local, "Programs", "AnthropicClaude"), Path.Combine(programFiles, "Claude"), Path.Combine(programFiles, "AnthropicClaude"),
                                     Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), "Claude") })
        {
            yield return Path.Combine(root, "Claude.exe");
            if (!Directory.Exists(root)) continue;
            var versions = Directory.EnumerateDirectories(root, "app-*")
                .Select(path => (path, version: Version.TryParse(Path.GetFileName(path)[4..], out var v) ? v : null))
                .Where(entry => entry.version != null).OrderByDescending(entry => entry.version);
            foreach (var entry in versions) yield return Path.Combine(entry.path, "Claude.exe");
        }
    }

    internal static bool IsDesktopExecutable(string path)
    {
        if (!Path.IsPathFullyQualified(path) || !Path.GetFileName(path).Equals("Claude.exe", StringComparison.OrdinalIgnoreCase)
            || path.Contains(@"\WindowsApps\", StringComparison.OrdinalIgnoreCase) || !File.Exists(path)) return false;
        var info = FileVersionInfo.GetVersionInfo(path);
        return info.CompanyName?.Contains("Anthropic", StringComparison.OrdinalIgnoreCase) == true
            && (string.Equals(info.ProductName, "Claude", StringComparison.OrdinalIgnoreCase)
                || string.Equals(info.ProductName, "Claude Desktop", StringComparison.OrdinalIgnoreCase));
    }

    internal static async Task VerifyPublisherAsync(string path, CancellationToken ct)
    {
        // The registered Win32 handler is per-user. Verify the signed file before
        // starting it, especially if SafeCode was elevated for gateway setup.
        var powershell = Path.Combine(Environment.SystemDirectory, @"WindowsPowerShell\v1.0\powershell.exe");
        var start = new ProcessStartInfo(powershell) { UseShellExecute = false, CreateNoWindow = true };
        foreach (var arg in new[] { "-NoProfile", "-NonInteractive", "-Command",
            "$s=Get-AuthenticodeSignature -LiteralPath $env:SAFECODE_DESKTOP_TARGET; " +
            "if($s.Status -ne 'Valid' -or $s.SignerCertificate.GetNameInfo('SimpleName',$false) -notmatch '^Anthropic(,? (PBC|Inc\\.?))?$'){exit 1}" })
            start.ArgumentList.Add(arg);
        start.Environment["SAFECODE_DESKTOP_TARGET"] = path;
        using var check = Process.Start(start) ?? throw new IOException("无法检查 Claude Desktop 的数字签名。");
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(TimeSpan.FromSeconds(30));
        try { await check.WaitForExitAsync(deadline.Token); }
        catch (OperationCanceledException)
        {
            if (!check.HasExited) check.Kill();
            ct.ThrowIfCancellationRequested();
            throw new IOException("检查 Claude Desktop 签名超时，请稍后重试。");
        }
        if (check.ExitCode != 0)
            throw new InvalidOperationException("此 Claude.exe 的 Anthropic 数字签名未获 Windows 确认。请检查系统时间及安装来源，或重新安装官方客户端。");
    }

    // Activation returns an HRESULT if Windows cannot open this registered entry.
    [ComImport, Guid("2e941141-7f97-4756-ba1d-9decde894a3d"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IApplicationActivationManager
    {
        void ActivateApplication([MarshalAs(UnmanagedType.LPWStr)] string appId,
            [MarshalAs(UnmanagedType.LPWStr)] string arguments, uint options, out uint processId);
    }

    private static bool IsDesktopProcess(Process process, int session)
    {
        try
        {
            if (process.HasExited || process.SessionId != session || process.MainModule?.FileName is not string path) return false;
            return path.Contains(@"\WindowsApps\Claude_", StringComparison.OrdinalIgnoreCase)
                || IsDesktopExecutable(path);
        }
        catch (InvalidOperationException) { return false; }
        catch (Win32Exception error) when (error.NativeErrorCode is 5 or 299) { return false; }
    }

    internal static async Task<object> OpenAsync(DesktopKeyAccess access, bool reopen, CancellationToken ct)
    {
        string? exe = ExecutableCandidates().FirstOrDefault(IsDesktopExecutable);
        string? appId;
        try { appId = RegisteredAppId(); }
        catch (COMException) when (exe != null) { appId = null; }
        if (appId == null && exe == null)
        {
            try { using var browser = Process.Start(new ProcessStartInfo(DownloadPage) { UseShellExecute = true }); }
            catch (Win32Exception) { throw new InvalidOperationException("未找到 Claude Desktop。请在浏览器打开 "+DownloadPage); }
            return new { installationRequired = true, message = "未找到 Claude Desktop，已打开官方下载页。安装后请回到 SafeCode 再次打开。" };
        }

        bool packaged = appId != null && (exe == null || (access.Configured && access.UsesPackagedProfile()));
        using var executableLock = !packaged && exe != null ? File.Open(exe, FileMode.Open, FileAccess.Read, FileShare.Read) : null;
        if (executableLock != null) await VerifyPublisherAsync(exe!, ct);
        long readySince = await access.PrepareLaunchAsync(ct);
        using var self = Process.GetCurrentProcess();
        var apps = Process.GetProcessesByName("Claude");
        try
        {
            var desktop = apps.Where(p => IsDesktopProcess(p, self.SessionId)).ToArray();
            if (reopen)
            {
                foreach (var app in desktop)
                    if (!app.HasExited && app.MainWindowHandle != IntPtr.Zero) app.CloseMainWindow();
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
                timeout.CancelAfter(TimeSpan.FromSeconds(15));
                try { await Task.WhenAll(desktop.Select(p => p.WaitForExitAsync(timeout.Token))); }
                catch (OperationCanceledException) when (!ct.IsCancellationRequested)
                { throw new InvalidOperationException("请从系统托盘完全退出 Claude Desktop，再点击「打开 Claude Desktop」。"); }
                await access.PrepareLaunchAsync(ct);
            }
            else if (desktop.Any(app => !app.HasExited && new DateTimeOffset(app.StartTime).ToUnixTimeMilliseconds() < readySince))
                throw new InvalidOperationException("Claude Desktop 在本次网关启用前就已运行。请从系统托盘完全退出，再点击打开以载入当前配置。");

            if (packaged)
            {
                var type = Type.GetTypeFromCLSID(new Guid("45BA127D-10A8-46EA-8AB7-56EA9078943C"), true)!;
                var manager = (IApplicationActivationManager)Activator.CreateInstance(type)!;
                try { manager.ActivateApplication(appId!, "", 0, out _); }
                catch (COMException)
                {
                    // Windows may deny direct activation from an elevated SafeCode process.
                    var shell = new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "explorer.exe"));
                    shell.ArgumentList.Add(@"shell:AppsFolder\" + appId);
                    using var request = Process.Start(shell);
                }
                finally { Marshal.ReleaseComObject(manager); }
            }
            else
            {
                using var child = Process.Start(new ProcessStartInfo(exe!) { UseShellExecute = false,
                    WorkingDirectory = Path.GetDirectoryName(exe!)! }) ?? throw new IOException("无法启动 Claude Desktop。");
            }
            for (int i = 0; i < 40; i++)
            {
                var started = Process.GetProcessesByName("Claude");
                try { if (started.Any(p => IsDesktopProcess(p, self.SessionId) && p.MainWindowHandle != IntPtr.Zero))
                    return "已打开 Claude Desktop。"; }
                finally { foreach (var p in started) p.Dispose(); }
                await Task.Delay(250, ct);
            }
            throw new InvalidOperationException("已找到 Claude Desktop，但尚未检测到桌面窗口。请检查首次启动提示或系统托盘。");
        }
        catch (Exception error) when (error is COMException or Win32Exception)
        { throw new InvalidOperationException("Windows 未能启动已安装的 Claude Desktop。请从开始菜单检查该应用是否能正常打开。", error); }
        finally { foreach (var p in apps) p.Dispose(); }
    }
}
