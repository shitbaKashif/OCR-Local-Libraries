using System.Text.Json.Serialization;

namespace OpdVerificationApi.Models;

public class VerifyResponse
{
    [JsonPropertyName("AmountVerified")]
    public bool AmountVerified { get; set; }

    [JsonPropertyName("ImageDup")]
    public bool ImageDup { get; set; }
}
