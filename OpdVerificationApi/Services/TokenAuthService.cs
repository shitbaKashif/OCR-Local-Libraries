namespace OpdVerificationApi.Services;

public class TokenAuthService
{
    public bool IsValid(string? token)
        => !string.IsNullOrWhiteSpace(token)
           && token == Environment.GetEnvironmentVariable("JAZZ_API_TOKEN");
}
