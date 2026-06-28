using System.ComponentModel.DataAnnotations;
using System.Text.Json.Serialization;

namespace OpdVerificationApi.Models;

public class VerifyRequest
{
    // Token validated manually in controller — no [Required] so bad token → 401 not 400
    [JsonPropertyName("token")]
    public string Token { get; set; } = string.Empty;

    [Required]
    [Range(0.01, 500_000.0, ErrorMessage = "amount_pkr must be between 0.01 and 500000")]
    [JsonPropertyName("amount_pkr")]
    public decimal AmountPkr { get; set; }

    [Required]
    [JsonPropertyName("image_base64")]
    public string ImageBase64 { get; set; } = string.Empty;
}
