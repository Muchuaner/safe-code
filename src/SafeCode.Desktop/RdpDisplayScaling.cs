using System.Drawing;
using System.Runtime.InteropServices;

namespace SafeCode.Desktop;

// Client-side presentation only: never negotiates a different remote resolution or DPI.
internal static class RdpDisplayScaling
{
    internal static Rectangle Fit(Size viewport, DesktopResolution resolution, bool allowUpscale)
    {
        if (viewport.Width <= 0 || viewport.Height <= 0) return Rectangle.Empty;
        if (resolution.Width <= 0 || resolution.Height <= 0)
            throw new ArgumentOutOfRangeException(nameof(resolution));
        double scale = Math.Min((double)viewport.Width / resolution.Width,
            (double)viewport.Height / resolution.Height);
        if (!allowUpscale) scale = Math.Min(1, scale);
        int width = Math.Max(1, (int)Math.Floor(resolution.Width * scale));
        int height = Math.Max(1, (int)Math.Floor(resolution.Height * scale));
        return new Rectangle((viewport.Width - width) / 2, (viewport.Height - height) / 2, width, height);
    }

    internal static bool TryEnableZoom(object client)
    {
        try
        {
            // EnableZoom is a pre-connect capability. Keeping it enabled permits live
            // on/off switching by changing only the local control's bounding rectangle.
            // ZoomLevel is NOT used: it is mutually exclusive with SmartSizing.
            object enabled = true;
            ((IExtendedSettings)client).SetProperty("EnableZoom", ref enabled);
            return true;
        }
        catch (COMException) { return false; }
        catch (InvalidCastException) { return false; }
    }

    // IMsRdpExtendedSettings is IUnknown, with put_Property preceding get_Property.
    // https://learn.microsoft.com/windows/win32/termserv/imsrdpextendedsettings-property
    [ComImport, Guid("302D8188-0052-4807-806A-362B628F9AC5"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IExtendedSettings
    {
        void SetProperty([MarshalAs(UnmanagedType.BStr)] string name,
            [In, MarshalAs(UnmanagedType.Struct)] ref object value);
        void GetProperty([MarshalAs(UnmanagedType.BStr)] string name,
            [Out, MarshalAs(UnmanagedType.Struct)] out object value);
    }
}
