using System.Buffers.Binary;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Media.Imaging;
using SafeCode.Core;

namespace SafeCode.Desktop;

// Read only standard image bytes, never deserialize application objects or follow file paths.
internal sealed record ImageSnapshot(List<DesktopImagePart> Originals, byte[]? Dib);

internal static class ClipboardImage
{
    internal const int Limit = DesktopImageWire.MaxBytes;
    internal const int PixelLimit = 32 * 1024 * 1024;
    private const uint CfBitmap = 2, CfDib = 8, CfDibV5 = 17;

    internal static string? Mime(string name) => name.ToLowerInvariant() switch {
        "png" or "image/png" => "image/png", "jfif" or "jpeg" or "image/jpeg" => "image/jpeg",
        "gif" or "image/gif" => "image/gif", "webp" or "image/webp" => "image/webp",
        "tiff" or "image/tiff" => "image/tiff", "bmp" or "image/bmp" => "image/bmp", _ => null
    };

    internal static bool NativePasteWins(uint id, string name) =>
        id is 1 or 7 or 13 or 15 or 0x80 || name.Equals("HTML Format", StringComparison.OrdinalIgnoreCase)
        || name.Equals("Rich Text Format", StringComparison.OrdinalIgnoreCase)
        || name.Equals("Link Source", StringComparison.OrdinalIgnoreCase)
        || name.Equals("Embed Source", StringComparison.OrdinalIgnoreCase)
        || name.StartsWith("text/", StringComparison.OrdinalIgnoreCase)
        || name.StartsWith("FileGroupDescriptor", StringComparison.OrdinalIgnoreCase);

    internal static ImageSnapshot? Read(IntPtr owner, bool explicitImage)
    {
        if (!OpenClipboard(owner)) throw new IOException("本機剪貼板正忙，請重新貼上。");
        try
        {
            List<(uint Id, string Name)> formats = [];
            uint id = 0;
            while ((id = EnumClipboardFormats(id)) != 0)
            {
                var name = new StringBuilder(128);
                if (id >= 0xc000) GetClipboardFormatName(id, name, name.Capacity);
                formats.Add((id, name.ToString()));
            }
            if (!explicitImage && formats.Any(f => NativePasteWins(f.Id, f.Name))) return null;
            if (!formats.Any(f => f.Id is CfDib or CfDibV5 or CfBitmap || Mime(f.Name) != null)) return null;
            List<DesktopImagePart> parts = [];
            int remaining = Limit;
            foreach (var f in formats)
            {
                var mime = Mime(f.Name);
                if (mime == null || parts.Any(p => p.Mime == mime)) continue;
                var bytes = ReadGlobal(f.Id, remaining);
                if (bytes is { Length: > 0 } && Signature(mime, bytes))
                {
                    bytes = Unpad(mime, bytes);
                    parts.Add(new(mime, bytes)); remaining -= bytes.Length;
                }
            }
            // If the source has not offered PNG, its DIB is also useful as a fallback on Windows
            // without a codec for the original (e.g. WebP).
            byte[]? dib = null;
            if (!parts.Any(p => p.Mime == "image/png"))
            {
                foreach (var fmt in new[] { CfDibV5, CfDib })
                {
                    if (!IsClipboardFormatAvailable(fmt)) continue;
                    try { dib = ReadGlobal(fmt, Limit); }
                    catch (InvalidDataException) when (parts.Count > 0) { break; }
                    if (dib is { Length: >= 40 }) break;
                }
            }
            if (parts.Count == 0 && dib == null) throw new InvalidDataException("未能讀取本機圖片，請重新截圖。");
            return new(parts, dib);
        }
        finally { CloseClipboard(); }
    }

    private static byte[]? ReadGlobal(uint id, int budget)
    {
        var handle = GetClipboardData(id);
        if (handle == 0) return null;
        nuint size = GlobalSize(handle);
        if (size == 0) return null;
        if (size > (nuint)budget) throw new InvalidDataException("這張圖片超過 32 MiB 限制，請縮小截圖範圍。");
        IntPtr ptr = GlobalLock(handle);
        if (ptr == 0) return null;
        try { var bytes = new byte[(int)size]; Marshal.Copy(ptr, bytes, 0, bytes.Length); return bytes; }
        finally { GlobalUnlock(handle); }
    }

    internal static bool Signature(string mime, ReadOnlySpan<byte> b) => mime switch
    {
        "image/png" => b.StartsWith(new byte[] { 137,80,78,71,13,10,26,10 }),
        "image/jpeg" => b.StartsWith(new byte[] { 255,216,255 }),
        "image/gif" => b.StartsWith("GIF87a"u8) || b.StartsWith("GIF89a"u8),
        "image/webp" => b.Length >= 12 && b.StartsWith("RIFF"u8) && b[8..].StartsWith("WEBP"u8),
        "image/tiff" => b.StartsWith("II*\0"u8) || b.StartsWith("MM\0*"u8),
        "image/bmp" => b.StartsWith("BM"u8), _ => false
    };

    // GlobalSize can include allocator padding. Never re-encode a native format just to remove it.
    internal static byte[] Unpad(string mime, byte[] data)
    {
        ReadOnlySpan<byte> b = data;
        long end = b.Length;
        if (mime == "image/png")
        {
            for (long pos = 8; pos + 12 <= b.Length;)
            {
                uint len = BinaryPrimitives.ReadUInt32BigEndian(b[(int)pos..]);
                bool last = b.Slice((int)pos + 4, 4).SequenceEqual("IEND"u8);
                pos += len + 12L;
                if (last && len == 0 && pos <= b.Length) { end = pos; break; }
            }
        }
        else if (mime == "image/webp" && b.Length >= 12) end = BinaryPrimitives.ReadUInt32LittleEndian(b[4..]) + 8L;
        else if (mime == "image/bmp" && b.Length >= 14) end = BinaryPrimitives.ReadUInt32LittleEndian(b[2..]);
        else if (mime is "image/jpeg" or "image/gif")
        {
            int n = b.Length;
            while (n > 2 && b[n - 1] == 0) n--;
            if (mime == "image/jpeg" ? b[n-2] == 255 && b[n-1] == 217 : b[n-1] == 0x3b) end = n;
        }
        return end > 0 && end < b.Length && b[(int)end..].IndexOfAnyExcept((byte)0) < 0 ? data[..(int)end] : data;
    }

    internal static IReadOnlyList<DesktopImagePart> Prepare(ImageSnapshot snapshot)
    {
        var parts = new List<DesktopImagePart>(snapshot.Originals);
        if (parts.Any(p => p.Mime == "image/png")) return parts;
        var budget = Limit - parts.Sum(p => p.Bytes.Length);
        // Prefer the original to its often flattened DIB; PNG is only an additional representation.
        IEnumerable<byte[]> images = parts.Select(p => p.Bytes);
        if (snapshot.Dib != null) images = images.Append(snapshot.Dib);
        foreach (var bytes in images.ToArray())
        {
            try
            {
                var image = ReferenceEquals(bytes, snapshot.Dib) ? BmpFromDib(bytes) : bytes;
                using var input = new MemoryStream(image, false);
                var decoder = BitmapDecoder.Create(input, BitmapCreateOptions.PreservePixelFormat, BitmapCacheOption.OnDemand);
                var frame = decoder.Frames[0];
                if ((long)frame.PixelWidth * frame.PixelHeight > PixelLimit) throw new InvalidDataException("截圖像素過多。");
                using var output = new BoundedStream(budget);
                var encoder = new PngBitmapEncoder();
                encoder.Frames.Add(BitmapFrame.Create(frame));
                encoder.Save(output);
                var png = output.ToArray();
                parts.Add(new("image/png", png));
                // DIB and BMP share pixel bytes. Offer that lossless local representation too
                // when it is the sole original and fits, with PNG first for Linux applications.
                if (snapshot.Originals.Count == 0 && ReferenceEquals(bytes, snapshot.Dib) &&
                    image.Length <= budget - png.Length) parts.Add(new("image/bmp", image));
                break;
            }
            catch (Exception e) when (e is IOException or InvalidDataException or ArgumentException or InvalidOperationException or
                NotSupportedException or COMException or FormatException or OverflowException or System.IO.FileFormatException)
            { /* Try the next representation or return just the untouched originals. */ }
        }
        if (parts.Count == 0) throw new InvalidDataException("未能無損轉換這張位圖，或圖片超出大小限制。");
        return parts;
    }

    internal static byte[] BmpFromDib(byte[] d)
    {
        uint U32(int i) => BinaryPrimitives.ReadUInt32LittleEndian(d.AsSpan(i));
        ushort U16(int i) => BinaryPrimitives.ReadUInt16LittleEndian(d.AsSpan(i));
        if (d.Length < 40 || d.Length > Limit) throw new InvalidDataException("DIB 長度無效。");
        uint head = U32(0);
        if (head is not (40 or 52 or 56 or 108 or 124) || head > d.Length || U16(12) != 1) throw new InvalidDataException("DIB 頭無效。");
        int w = (int)U32(4), h = (int)U32(8);
        ushort bpp = U16(14);
        uint compression = U32(16), colors = U32(32);
        if (w <= 0 || h == 0 || (long)w * Math.Abs((long)h) > PixelLimit ||
            bpp is not (1 or 4 or 8 or 16 or 24 or 32) || compression is not (0 or 3 or 6))
            throw new InvalidDataException("不支援此位圖編碼或尺寸。");
        if (colors == 0 && bpp <= 8) colors = 1u << bpp;
        // BITMAPINFOHEADER stores bit masks AFTER the header. V4/V5 store them WITHIN it.
        long offset = head + (head == 40 ? (compression == 3 ? 12 : compression == 6 ? 16 : 0) : 0) + colors * 4L;
        long pixels = (((long)w * bpp + 31) / 32) * 4 * Math.Abs((long)h);
        if (head == 124 && U32(116) != 0 && U32(112) == offset) offset += U32(116);
        if (offset + pixels > d.Length) throw new InvalidDataException("位圖像素不完整。");
        byte[] bmp = new byte[checked(d.Length + 14)];
        bmp[0] = 66; bmp[1] = 77;
        BinaryPrimitives.WriteUInt32LittleEndian(bmp.AsSpan(2), (uint)bmp.Length);
        BinaryPrimitives.WriteUInt32LittleEndian(bmp.AsSpan(10), checked((uint)offset + 14));
        d.CopyTo(bmp, 14);
        return bmp;
    }

    private sealed class BoundedStream(int limit) : MemoryStream
    {
        private void Check(long end) { if (end > limit) throw new InvalidDataException("圖片太大。"); }
        public override void Write(byte[] b, int off, int count) { Check(Position + count); base.Write(b, off, count); }
        public override void Write(ReadOnlySpan<byte> b) { Check(Position + b.Length); base.Write(b); }
        public override void WriteByte(byte b) { Check(Position + 1); base.WriteByte(b); }
        public override void SetLength(long value) { Check(value); base.SetLength(value); }
    }

    [DllImport("user32.dll")] private static extern bool OpenClipboard(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern bool CloseClipboard();
    [DllImport("user32.dll")] private static extern uint EnumClipboardFormats(uint format);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetClipboardFormatName(uint format, StringBuilder buffer, int size);
    [DllImport("user32.dll")] private static extern bool IsClipboardFormatAvailable(uint format);
    [DllImport("user32.dll")] private static extern IntPtr GetClipboardData(uint format);
    [DllImport("kernel32.dll")] private static extern nuint GlobalSize(IntPtr hMem);
    [DllImport("kernel32.dll")] private static extern IntPtr GlobalLock(IntPtr hMem);
    [DllImport("kernel32.dll")] private static extern bool GlobalUnlock(IntPtr hMem);
}
