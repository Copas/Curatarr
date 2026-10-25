using System.Collections.Concurrent;
using System.Collections.Frozen;

namespace Jellyfin.Plugin.Curatarr.Posters;

/// <summary>
/// One account's own poster for a title, as Curatarr lists it.
/// </summary>
/// <param name="Item">The title's Jellyfin id, without dashes.</param>
/// <param name="User">The account's Jellyfin id, without dashes.</param>
/// <param name="Id">Curatarr's id for the poster (its picture's address).</param>
/// <param name="Tag">The image tag that stands for this poster.</param>
/// <param name="Large">Whether there's a second picture for where it's shown big
/// (a favourite ribbon listing names, on a title's own page).</param>
public sealed record AccountPoster(string Item, string User, string Id, string Tag, bool Large = false);

/// <summary>
/// The posters Curatarr has for single accounts, looked up by account and title
/// (to put the tag in what Jellyfin sends that account) and by tag (to serve the
/// picture when an app asks for it).
/// </summary>
public sealed class PosterStore
{
    private FrozenDictionary<string, FrozenDictionary<string, string>> _byUser =
        FrozenDictionary<string, FrozenDictionary<string, string>>.Empty;

    private FrozenDictionary<string, AccountPoster> _byTag = FrozenDictionary<string, AccountPoster>.Empty;

    /// <summary>Gets the placeholder blur for a tag, once worked out.</summary>
    internal ConcurrentDictionary<string, string> BlurHashes { get; } = new(StringComparer.Ordinal);

    /// <summary>Gets a value indicating whether any account has a poster of its own.</summary>
    public bool Any => _byTag.Count > 0;

    /// <summary>
    /// Replaces what's known with Curatarr's latest list.
    /// </summary>
    /// <param name="posters">Every account's own posters.</param>
    public void Replace(IEnumerable<AccountPoster> posters)
    {
        ArgumentNullException.ThrowIfNull(posters);
        var list = posters.ToList();
        _byUser = list
            .GroupBy(p => p.User, StringComparer.OrdinalIgnoreCase)
            .ToFrozenDictionary(
                g => g.Key,
                g => g.GroupBy(p => p.Item, StringComparer.OrdinalIgnoreCase)
                    .ToFrozenDictionary(i => i.Key, i => i.Last().Tag, StringComparer.OrdinalIgnoreCase),
                StringComparer.OrdinalIgnoreCase);
        _byTag = list
            .GroupBy(p => p.Tag, StringComparer.OrdinalIgnoreCase)
            .ToFrozenDictionary(g => g.Key, g => g.Last(), StringComparer.OrdinalIgnoreCase);
    }

    /// <summary>
    /// The account's posters by title, or null when it has none.
    /// </summary>
    /// <param name="userId">The account.</param>
    /// <returns>Title id → tag.</returns>
    public IReadOnlyDictionary<string, string>? ForUser(Guid userId)
        => _byUser.TryGetValue(userId.ToString("N"), out var titles) ? titles : null;

    /// <summary>
    /// The poster a tag stands for, if it's one of Curatarr's.
    /// </summary>
    /// <param name="tag">The tag in the image request.</param>
    /// <param name="poster">The poster.</param>
    /// <returns>Whether it is.</returns>
    public bool TryGetByTag(string? tag, out AccountPoster poster)
    {
        if (!string.IsNullOrEmpty(tag) && _byTag.TryGetValue(tag, out var found))
        {
            poster = found;
            return true;
        }

        poster = null!;
        return false;
    }
}
