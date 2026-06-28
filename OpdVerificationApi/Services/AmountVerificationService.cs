namespace OpdVerificationApi.Services;

public static class AmountVerificationService
{
    public static bool Verify(decimal claimedAmount, double? extractedAmount)
    {
        if (extractedAmount is null) return false;
        return Math.Abs(claimedAmount - (decimal)extractedAmount.Value) <= 1.00m;
    }
}
