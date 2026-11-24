using System.Buffers.Binary;
using System.IO;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using SafeCode.Core;
using SafeCode.Desktop;

internal static class Program
{
    private static int count;
    private static void Check(bool pass, string name)
    {
        if (!pass) throw new Exception(name);
        Console.WriteLine("PASS " + name); count++;
    }

    private static byte[] Encode(BitmapEncoder encoder)
    {
        var b = BitmapSource.Create(2, 1, 96, 96, PixelFormats.Bgra32, null,
            new byte[] { 14, 32, 77, 127, 250, 93, 35, 255 }, 8);
        encoder.Frames.Add(BitmapFrame.Create(b));
        using var output = new MemoryStream(); encoder.Save(output); return output.ToArray();
    }

    private static byte[] Pixels(byte[] png)
    {
        using var s = new MemoryStream(png);
        var image = new PngBitmapDecoder(s, BitmapCreateOptions.PreservePixelFormat, BitmapCacheOption.OnLoad).Frames[0];
        var rgb = new FormatConvertedBitmap(image, PixelFormats.Bgra32, null, 0);
        byte[] b = new byte[rgb.PixelWidth * rgb.PixelHeight * 4];
        rgb.CopyPixels(b, rgb.PixelWidth * 4, 0); return b;
    }

    [STAThread]
    public static void Main()
    {
        var png = Encode(new PngBitmapEncoder());
        var originals = ClipboardImage.Prepare(new([new("image/png", png)], null));
        Check(originals.Count == 1 && originals[0].Bytes.SequenceEqual(png) && Pixels(png)[3] == 127,
            "original PNG and alpha survive byte-for-byte");
        Check(ClipboardImage.Unpad("image/png", [..png, 0, 0, 0]).SequenceEqual(png), "HGLOBAL padding removed after PNG IEND");
        var jpeg = Encode(new JpegBitmapEncoder());
        var jpegFormats = ClipboardImage.Prepare(new([new("image/jpeg", jpeg)], null));
        Check(jpegFormats.Select(p => p.Mime).SequenceEqual(["image/jpeg", "image/png"]) &&
            jpegFormats[0].Bytes.SequenceEqual(jpeg), "JPEG is not recompressed; PNG is a second target");
        byte[] gif = Encode(new GifBitmapEncoder());
        var gifFormats = ClipboardImage.Prepare(new([new("image/gif", gif)], null));
        Check(gifFormats.Count == 2 && gifFormats[0].Bytes.SequenceEqual(gif), "GIF bytes preserved even with a still PNG alternative");
        byte[] dib = new byte[56]; // 40 header + 3 DWORD masks + 1 BGRA pixel
        void Put(int i, uint v) => BinaryPrimitives.WriteUInt32LittleEndian(dib.AsSpan(i), v);
        Put(0, 40); Put(4, 1); Put(8, 1); dib[12] = 1; dib[14] = 32; Put(16, 3);
        Put(40, 0xff0000); Put(44, 0xff00); Put(48, 0xff); Put(52, 0x007b402a);
        Check(BitConverter.ToUInt32(ClipboardImage.BmpFromDib(dib), 10) == 66, "mask-aware Windows BMP pixel offset");
        var onlyBitmap = ClipboardImage.Prepare(new([], dib));
        var pixels = Pixels(onlyBitmap[0].Bytes);
        Check(onlyBitmap[0].Mime == "image/png" && pixels.SequenceEqual(new byte[] {42,64,123,255}),
            "32-bit Windows screenshot with masks becomes the correct opaque RGBA pixel");
        Check(onlyBitmap[1].Mime == "image/bmp" && onlyBitmap[1].Bytes.AsSpan(14).SequenceEqual(dib),
            "original bitmap pixels, masks and dimensions remain available alongside PNG");
        var topDown = new byte[48]; Array.Copy(dib, topDown, 40);
        BitConverter.GetBytes(-2).CopyTo(topDown,8); topDown[14] = 24; topDown[16] = 0;
        topDown[40] = 255; topDown[46] = 255;
        Check(Pixels(ClipboardImage.Prepare(new([],topDown))[0].Bytes).SequenceEqual(new byte[]{255,0,0,255,0,0,255,255}),
            "top-down scanlines keep their orientation and colour");
        var palette = new byte[52]; Array.Copy(dib,palette,40); palette[14]=1; palette[16]=0;
        palette[46]=255; palette[48]=128;
        Check(Pixels(ClipboardImage.Prepare(new([],palette))[0].Bytes).SequenceEqual(new byte[]{0,0,255,255}),
            "palette offsets survive conversion");
        byte[] v5 = new byte[128];
        BitConverter.GetBytes(124).CopyTo(v5,0); v5[4]=1; v5[8]=1; v5[12]=1; v5[14]=32; v5[16]=3;
        BitConverter.GetBytes(0xff0000).CopyTo(v5,40); BitConverter.GetBytes(0xff00).CopyTo(v5,44);
        BitConverter.GetBytes(0xff).CopyTo(v5,48); BitConverter.GetBytes(0xff000000u).CopyTo(v5,52);
        BitConverter.GetBytes(0x73524742).CopyTo(v5,56);
        new byte[]{35,50,80,130}.CopyTo(v5,124);
        Check(Pixels(ClipboardImage.Prepare(new([],v5))[0].Bytes).SequenceEqual(new byte[]{35,50,80,130}),
            "DIBV5 explicit alpha survives in fallback PNG");
        bool invalid=false;
        try { ClipboardImage.Prepare(new([],dib[..^1])); } catch(InvalidDataException){invalid=true;}
        Check(invalid, "truncated DIB is refused rather than pasted blank");
        byte[] webpStub = "RIFF\x04\0\0\0WEBP"u8.ToArray();
        var web = ClipboardImage.Prepare(new([new("image/webp",webpStub)], dib));
        Check(web[0].Bytes.SequenceEqual(webpStub) && web[1].Mime == "image/png", "DIB previews an original with an unavailable decoder");
        Check(ClipboardImage.NativePasteWins(13, "") && ClipboardImage.NativePasteWins(15, "")
            && ClipboardImage.NativePasteWins(0xc002,"HTML Format") && !ClipboardImage.NativePasteWins(8,""),
            "ordinary paste retains text, files, HTML and detects standalone images");
        using var wire = new MemoryStream();
        DesktopImageWire.WriteAsync(wire, jpegFormats, default).GetAwaiter().GetResult();
        var frame = wire.ToArray(); var cut = Array.IndexOf(frame,(byte)10);
        Check(cut > 0 && frame.AsSpan(cut+1,jpeg.Length).SequenceEqual(jpeg)
            && frame.AsSpan(cut+1+jpeg.Length).SequenceEqual(jpegFormats[1].Bytes), "binary frame lengths and bytes match");
        bool duplicate = false;
        try {DesktopImageWire.WriteAsync(Stream.Null,[new("image/png",png),new("image/png",png)],default).GetAwaiter().GetResult();}
        catch(InvalidDataException){duplicate=true;}
        Check(duplicate,"duplicate wire formats refused");
        Console.WriteLine(count + " checks passed; no system clipboard, server or user files accessed.");
    }
}
