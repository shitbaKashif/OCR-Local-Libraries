using System.Text.Json.Serialization;

namespace OpdVerificationApi.Models;

public class VerifyResponse
{
    [JsonPropertyName("AmountVerified")] public bool    AmountVerified { get; set; }
    [JsonPropertyName("ImageDup")]       public bool    ImageDup       { get; set; }
    [JsonPropertyName("DateValid")]      public bool    DateValid      { get; set; }
    [JsonPropertyName("receipt_date")]   public string? ReceiptDate    { get; set; }
}
