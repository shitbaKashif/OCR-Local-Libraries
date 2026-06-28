namespace OpdVerificationApi.Services;

public static class DateCheckService
{
    /// <summary>
    /// Receipt date must fall within the last 3 calendar months (inclusive).
    /// A small 7-day forward buffer is allowed to handle timezone edge cases.
    /// Returns false when the date is absent or cannot be parsed.
    /// </summary>
    public static bool IsValid(string? receiptDateIso)
    {
        if (string.IsNullOrWhiteSpace(receiptDateIso))
            return false;

        if (!DateOnly.TryParseExact(receiptDateIso, "yyyy-MM-dd",
                System.Globalization.CultureInfo.InvariantCulture,
                System.Globalization.DateTimeStyles.None, out var receiptDate))
            return false;

        var today   = DateOnly.FromDateTime(DateTime.Today);
        var cutoff  = today.AddMonths(-3);

        return receiptDate >= cutoff && receiptDate <= today.AddDays(7);
    }
}
