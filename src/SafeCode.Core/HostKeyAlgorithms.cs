using Renci.SshNet;
using System.Text;

namespace SafeCode.Core;

public sealed partial class SshSession
{
    private void PinHostKeyAlgorithms(ConnectionInfo info)
    {
        if(trustedKey==null)throw new InvalidOperationException("尚未确认服务器身份，不能建立任务通道。");
        using var bytes=new MemoryStream(trustedKey);using var reader=new BinaryReader(bytes);
        int length=System.Net.IPAddress.NetworkToHostOrder(reader.ReadInt32());
        if(length<=0||length>trustedKey.Length-4)throw new InvalidDataException("已信任的服务器密钥格式无效。");
        string keyType=Encoding.ASCII.GetString(reader.ReadBytes(length));
        // 同一服务器可能同时提供多种密钥。后续通道继续协商已确认的密钥类型，
        // 避免系统更新新增密钥后默认改选另一把；握手仍逐字节核对原密钥。
        // RSA 的 SHA-2 签名算法共用 ssh-rsa 公钥，保留库原有的优先顺序。
        foreach(string algorithm in info.HostKeyAlgorithms.Keys.ToArray())
            if(algorithm!=keyType&&!(keyType=="ssh-rsa"&&algorithm is "rsa-sha2-256" or "rsa-sha2-512"))
                info.HostKeyAlgorithms.Remove(algorithm);
        if(info.HostKeyAlgorithms.Count==0)throw new InvalidOperationException("当前客户端不支持已确认的服务器密钥类型，请更新软件后重新连接。");
    }
}
