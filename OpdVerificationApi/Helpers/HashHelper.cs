using System.Security.Cryptography;

namespace OpdVerificationApi.Helpers;

public static class HashHelper
{
    public static string ComputeSha256(byte[] data)
    {
        var hash = SHA256.HashData(data);
        return Convert.ToHexStringLower(hash);
    }
}
