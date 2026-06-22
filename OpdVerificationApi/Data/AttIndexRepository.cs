using Dapper;
using Microsoft.Data.SqlClient;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging;

namespace OpdVerificationApi.Data;

public class AttIndexRepository
{
    private readonly string _connectionString;
    private readonly ILogger<AttIndexRepository> _logger;

    public AttIndexRepository(IConfiguration config, ILogger<AttIndexRepository> logger)
    {
        _connectionString = config.GetConnectionString("OpdMedia")
            ?? throw new InvalidOperationException("ConnectionStrings:OpdMedia not configured.");
        _logger = logger;
    }

    private SqlConnection CreateConnection() => new(_connectionString);

    public async Task<bool> ExistsBySha256Async(string hash)
    {
        await using var conn = CreateConnection();
        var count = await conn.ExecuteScalarAsync<int>(
            "SELECT COUNT(1) FROM [opd_att_index] WHERE sha256_hash = @Hash",
            new { Hash = hash });
        return count > 0;
    }

    public async Task<List<(int AttId, long PHash)>> GetAllPhashesAsync()
    {
        await using var conn = CreateConnection();
        var rows = await conn.QueryAsync<(int att_id, long phash)>(
            "SELECT att_id, phash FROM [opd_att_index] WHERE phash IS NOT NULL");
        return rows.Select(r => (r.att_id, r.phash)).ToList();
    }

    public async Task<Dictionary<int, string>> GetOcrTextByIdsAsync(IEnumerable<int> ids)
    {
        var idList = ids.ToList();
        if (idList.Count == 0) return [];

        await using var conn = CreateConnection();
        var rows = await conn.QueryAsync<(int att_id, string ocr_text)>(
            "SELECT att_id, ocr_text FROM [opd_att_index] WHERE att_id IN @Ids AND ocr_text IS NOT NULL",
            new { Ids = idList });
        return rows.ToDictionary(r => r.att_id, r => r.ocr_text);
    }

    /// <summary>
    /// Inserts a new claim image into opd_attachments (stores bytes) and opd_att_index
    /// (stores SHA-256, pHash, OCR text). Returns the new att_id.
    /// Called only when ImageDup=false so we don't store duplicates.
    /// </summary>
    public async Task<int> InsertClaimAsync(
        byte[] imageBytes, string sha256, long phash, string? ocrText, string imageExt)
    {
        await using var conn = CreateConnection();
        await conn.OpenAsync();
        await using var tx = await conn.BeginTransactionAsync();
        try
        {
            // Derive a short title from the hash for traceability
            var title = $"api-{sha256[..16]}";

            var attId = await conn.ExecuteScalarAsync<int>(
                """
                INSERT INTO [dbo].[opd_attachments]
                    ([att_title],[att_content],[att_type],[att_auth_id])
                VALUES
                    (@Title, @Content, @Type, @AuthId);
                SELECT CAST(SCOPE_IDENTITY() AS INT);
                """,
                new { Title = title, Content = imageBytes, Type = imageExt[..Math.Min(5, imageExt.Length)], AuthId = 0 },
                tx);

            await conn.ExecuteAsync(
                """
                INSERT INTO [dbo].[opd_att_index]
                    ([att_id],[sha256_hash],[phash],[ocr_text])
                VALUES
                    (@AttId, @Sha256, @PHash, @OcrText)
                """,
                new { AttId = attId, Sha256 = sha256, PHash = phash, OcrText = ocrText },
                tx);

            await tx.CommitAsync();
            _logger.LogInformation("Stored new claim att_id={AttId} sha256={Hash}", attId, sha256[..8]);
            return attId;
        }
        catch
        {
            await tx.RollbackAsync();
            throw;
        }
    }
}
