using System.Numerics;
using System.Text.RegularExpressions;

namespace OpdVerificationApi.Services;

public static class DuplicationService
{
    public static List<int> FindPHashMatches(
        long incoming,
        List<(int AttId, long PHash)> all,
        int threshold)
    {
        // Cast both to ulong before XOR — preserves bit pattern of signed values
        ulong u = (ulong)incoming;
        return all
            .Where(r => BitOperations.PopCount(u ^ (ulong)r.PHash) <= threshold)
            .Select(r => r.AttId)
            .ToList();
    }

    public static bool HasTextDuplicate(
        string? incoming,
        Dictionary<int, string> candidates,
        double threshold)
    {
        if (string.IsNullOrWhiteSpace(incoming)) return false;
        var inTrigrams = Trigrams(Normalize(incoming));
        foreach (var (_, text) in candidates)
        {
            if (string.IsNullOrWhiteSpace(text)) continue;
            double sim = Jaccard(inTrigrams, Trigrams(Normalize(text)));
            if (sim >= threshold) return true;
        }
        return false;
    }

    private static HashSet<string> Trigrams(string t)
        => t.Length < 3
           ? []
           : Enumerable.Range(0, t.Length - 2).Select(i => t[i..(i + 3)]).ToHashSet();

    private static string Normalize(string t)
        => Regex.Replace(t.ToLowerInvariant(), @"[^a-z0-9؀-ۿ]", " ").Trim();

    private static double Jaccard(HashSet<string> a, HashSet<string> b)
    {
        int inter = a.Intersect(b).Count();
        int union = a.Count + b.Count - inter;
        return union == 0 ? 0.0 : (double)inter / union;
    }
}
