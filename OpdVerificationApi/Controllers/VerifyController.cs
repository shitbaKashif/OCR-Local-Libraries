using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;
using OpdVerificationApi.Data;
using OpdVerificationApi.Helpers;
using OpdVerificationApi.Models;
using OpdVerificationApi.Services;
using OpdVerificationApi.Settings;

namespace OpdVerificationApi.Controllers;

[ApiController]
[Route("api/v1")]
public class VerifyController : ControllerBase
{
    private readonly TokenAuthService       _tokenAuth;
    private readonly ImageIntelClient       _imageIntelClient;
    private readonly AttIndexRepository     _attIndexRepo;
    private readonly DupDetectionSettings   _dupSettings;
    private readonly ILogger<VerifyController> _logger;

    public VerifyController(
        TokenAuthService          tokenAuth,
        ImageIntelClient          imageIntelClient,
        AttIndexRepository        attIndexRepo,
        IOptions<DupDetectionSettings> dupSettings,
        ILogger<VerifyController> logger)
    {
        _tokenAuth        = tokenAuth;
        _imageIntelClient = imageIntelClient;
        _attIndexRepo     = attIndexRepo;
        _dupSettings      = dupSettings.Value;
        _logger           = logger;
    }

    [HttpPost("verify")]
    public async Task<IActionResult> Verify([FromBody] VerifyRequest? req)
    {
        // Auth — always 401 for bad/missing token, checked before anything else
        if (req is null || !_tokenAuth.IsValid(req.Token))
            return Unauthorized(new { error = "Unauthorized" });

        if (req.AmountPkr is <= 0 or > 500_000)
            return BadRequest(new { error = "amount_pkr must be between 0.01 and 500000" });
        if (string.IsNullOrWhiteSpace(req.ImageBase64))
            return BadRequest(new { error = "image_base64 is required" });

        byte[] imageBytes;
        try { imageBytes = Convert.FromBase64String(req.ImageBase64); }
        catch { return BadRequest(new { error = "Invalid base64 encoding" }); }

        var formatError = ValidateImageFormat(imageBytes);
        if (formatError is not null)
            return BadRequest(new { error = formatError });

        string sha256   = HashHelper.ComputeSha256(imageBytes);
        string imageExt = DetectImageExtension(imageBytes);

        // ── L1: exact SHA-256 duplicate check ────────────────────────────────
        // Done BEFORE calling the Python sidecar to avoid slow OCR on re-submissions.
        bool sha256Match;
        try
        {
            sha256Match = await _attIndexRepo.ExistsBySha256Async(sha256);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "DB SHA-256 lookup failed");
            return StatusCode(503, new { error = "Database unavailable" });
        }

        if (sha256Match)
        {
            // Exact duplicate: skip the sidecar entirely.
            // ImageDup=true means the claim is fraudulent — AmountVerified and DateValid are irrelevant.
            _logger.LogInformation("verify: L1 dup sha256={Hash} — fast reject", sha256[..8]);
            return Ok(new VerifyResponse { AmountVerified = false, ImageDup = true, DateValid = false, ReceiptDate = null });
        }

        // ── Call Python sidecar (OCR + amount extraction + hashing) ──────────
        SidecarResponse intel;
        try
        {
            intel = await _imageIntelClient.ProcessAsync(req.ImageBase64);
        }
        catch (HttpRequestException ex)
        {
            _logger.LogError(ex, "Sidecar call failed");
            return StatusCode(503, new { error = "Image processing service unavailable" });
        }
        catch (InvalidOperationException ex) when (ex.Message.StartsWith("bad_image:"))
        {
            return BadRequest(new { error = "Image could not be decoded" });
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Sidecar call failed");
            return StatusCode(500, new { error = "Internal server error" });
        }

        bool amountVerified = AmountVerificationService.Verify(req.AmountPkr, intel.GrandTotal);
        bool dateValid      = DateCheckService.IsValid(intel.ReceiptDate);

        // ── L2 + L3: near-duplicate detection ────────────────────────────────
        bool imageDup = false;
        try
        {
            var allPhashes = await _attIndexRepo.GetAllPhashesAsync();
            var l2Candidates = DuplicationService.FindPHashMatches(
                intel.PHash, allPhashes, _dupSettings.PHashThreshold);

            if (l2Candidates.Count > 0)
            {
                var candidateTexts = await _attIndexRepo.GetOcrTextByIdsAsync(l2Candidates);
                imageDup = DuplicationService.HasTextDuplicate(
                    intel.OcrText, candidateTexts, _dupSettings.JaccardThreshold);
            }
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "L2/L3 dup check failed — defaulting to not-dup");
            // Non-fatal: if dup check fails, treat as non-dup and persist
        }

        // ── Persist new image for future duplicate detection ──────────────────
        if (!imageDup)
        {
            try
            {
                await _attIndexRepo.InsertClaimAsync(
                    imageBytes, sha256, intel.PHash, intel.OcrText, imageExt);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Failed to persist claim — verdict still returned");
            }
        }

        _logger.LogInformation(
            "verify: sha256={Hash} amountVerified={Av} imageDup={Dup} dateValid={Dv} date={Dt} source={Src}",
            sha256[..8], amountVerified, imageDup, dateValid, intel.ReceiptDate ?? "null", intel.AmountSource);

        return Ok(new VerifyResponse
        {
            AmountVerified = amountVerified,
            ImageDup       = imageDup,
            DateValid      = dateValid,
            ReceiptDate    = intel.ReceiptDate,
        });
    }

    private static string? ValidateImageFormat(byte[] bytes)
    {
        if (bytes.Length > 20 * 1024 * 1024) return "Image exceeds 20 MB limit";
        if (bytes.Length < 4)                return "Image data too short";

        bool isJpeg = bytes[0] == 0xFF && bytes[1] == 0xD8 && bytes[2] == 0xFF;
        bool isPng  = bytes[0] == 0x89 && bytes[1] == 0x50;
        bool isBmp  = bytes[0] == 0x42 && bytes[1] == 0x4D;
        bool isTiff = (bytes[0] == 0x49 && bytes[1] == 0x49)
                   || (bytes[0] == 0x4D && bytes[1] == 0x4D);
        bool isWebp = bytes.Length > 11 && bytes[8] == 0x57 && bytes[9] == 0x45;
        // PDF: %PDF
        bool isPdf  = bytes[0] == 0x25 && bytes[1] == 0x50 && bytes[2] == 0x44 && bytes[3] == 0x46;
        // SVG: starts with '<' (either '<svg' directly or '<?xml' wrapper)
        bool isSvg  = bytes[0] == 0x3C;

        if (!isJpeg && !isPng && !isBmp && !isTiff && !isWebp && !isPdf && !isSvg)
            return "Unsupported format. Accepted: JPEG, PNG, BMP, TIFF, WEBP, PDF, SVG";
        return null;
    }

    private static string DetectImageExtension(byte[] bytes)
    {
        if (bytes.Length > 2 && bytes[0] == 0xFF && bytes[1] == 0xD8) return ".jpg";
        if (bytes.Length > 1 && bytes[0] == 0x89 && bytes[1] == 0x50) return ".png";
        if (bytes.Length > 1 && bytes[0] == 0x42 && bytes[1] == 0x4D) return ".bmp";
        if (bytes.Length > 11 && bytes[8] == 0x57 && bytes[9] == 0x45) return ".webp";
        if (bytes.Length > 3 && bytes[0] == 0x25 && bytes[1] == 0x50) return ".pdf";
        if (bytes.Length > 0 && bytes[0] == 0x3C)                      return ".svg";
        return ".tif";
    }
}
