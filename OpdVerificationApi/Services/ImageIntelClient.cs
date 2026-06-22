using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;

namespace OpdVerificationApi.Services;

public class SidecarResponse
{
    [JsonPropertyName("sha256_hash")]  public string? Sha256Hash  { get; set; }
    [JsonPropertyName("phash")]        public long    PHash       { get; set; }
    [JsonPropertyName("grand_total")]  public double? GrandTotal  { get; set; }
    [JsonPropertyName("ocr_text")]     public string? OcrText     { get; set; }
    [JsonPropertyName("ocr_success")]  public bool    OcrSuccess  { get; set; }
    [JsonPropertyName("amount_source")]public string? AmountSource{ get; set; }
}

public class ImageIntelClient
{
    private readonly HttpClient _http;
    private readonly ILogger<ImageIntelClient> _logger;

    public ImageIntelClient(HttpClient http, ILogger<ImageIntelClient> logger)
    {
        _http = http;
        _logger = logger;
    }

    public async Task<SidecarResponse> ProcessAsync(string imageBase64, CancellationToken ct = default)
    {
        var payload = new { image_base64 = imageBase64 };
        HttpResponseMessage response;
        try
        {
            response = await _http.PostAsJsonAsync("/process", payload, ct);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Python sidecar unreachable");
            throw new HttpRequestException("Image processing service unavailable", ex);
        }

        if (!response.IsSuccessStatusCode)
        {
            var body = await response.Content.ReadAsStringAsync(ct);
            _logger.LogError("Sidecar returned {StatusCode}: {Body}", response.StatusCode, body);
            // 400/422 from sidecar = bad image data — propagate as client error, not 503
            if ((int)response.StatusCode is 400 or 422)
                throw new InvalidOperationException($"bad_image:{body}");
            throw new HttpRequestException($"Sidecar error {(int)response.StatusCode}: {body}");
        }

        var result = await response.Content.ReadFromJsonAsync<SidecarResponse>(ct);
        if (result is null)
            throw new InvalidOperationException("Sidecar returned null response");

        return result;
    }
}
