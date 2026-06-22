namespace OpdVerificationApi.Settings;

public class DupDetectionSettings
{
    public int    PHashThreshold   { get; set; } = 12;
    public double JaccardThreshold { get; set; } = 0.85;
}
