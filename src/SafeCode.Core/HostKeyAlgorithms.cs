using Renci.SshNet;
using System.Text;

namespace SafeCode.Core;

public sealed partial class SshSession
{
    private void PinHostKeyAlgorithms(ConnectionInfo info)
    {
        if(trustedKey==null)throw new InvalidOperationException("尚未確認伺服器身份，不能建立任務通道。");
        using var bytes=new MemoryStream(trustedKey);using var reader=new BinaryReader(bytes);
        int length=System.Net.IPAddress.NetworkToHostOrder(reader.ReadInt32());
        if(length<=0||length>trustedKey.Length-4)throw new InvalidDataException("已信任的伺服器密鑰格式無效。");
        string keyType=Encoding.ASCII.GetString(reader.ReadBytes(length));
        // 同一伺服器可能同時提供多種密鑰。後續通道繼續協商已確認的密鑰類型，
        // 避免系統更新新增密鑰後預設改選另一把；握手仍逐位元組核對原密鑰。
        // RSA 的 SHA-2 簽名算法共用 ssh-rsa 公鑰，保留庫原有的優先順序。
        foreach(string algorithm in info.HostKeyAlgorithms.Keys.ToArray())
            if(algorithm!=keyType&&!(keyType=="ssh-rsa"&&algorithm is "rsa-sha2-256" or "rsa-sha2-512"))
                info.HostKeyAlgorithms.Remove(algorithm);
        if(info.HostKeyAlgorithms.Count==0)throw new InvalidOperationException("當前客戶端不支援已確認的伺服器密鑰類型，請更新軟件後重新連線。");
    }
}
