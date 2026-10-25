using System.Collections.Concurrent;
using System.Collections.Frozen;
using MediaBrowser.Model.Entities;

namespace Jellyfin.Plugin.Curatarr.Posters;

/// <summary>
/// One account's own picture for a title, as Curatarr lists it.
/// </summary>
/// <param name="Item">The title's Jellyfin id, without dashes.</param>
/// <param name="User">The account's Jellyfin id, without dashes.</param>
/// <param name="Id">Curatarr's id for the picture (its address).</param>
/// <param name="Tag">The image tag that stands for this picture.</param>
/// <param name="Large">Whether there's a second picture for where it's shown big
/// (a favourite ribbon listing names, on a title's own page).</param>
/// <param name="Wide">Whether there's a landscape version, for rows of wide
/// tiles (the title's Thumb). Only older Curatarrs say so; newer ones list
/// the wide tile as a picture of its own.</param>
/// <param name="Kind">Which of the title's pictures it is (Jellyfin's image
/// type: Primary, Thumb, Backdrop, Logo or Banner); none means the poster.</param>
public sealed record AccountPoster(string Item, string User, string Id, string Tag, bool Large = false, bool Wide = false, string? Kind = null)
{
    /// <summary>Gets the image type it stands for.</summary>
    public ImageType Type => Enum.TryParse<ImageType>(Kind, true, out var type) ? type : ImageType.Primary;
}

/// <summary>
/// The pictures Curatarr has for single accounts, looked up by account and title
/// (to put the tags in what Jellyfin sends that account) and by tag (to serve the
/// picture when an app asks for it).
/// </summary>
public sealed class PosterStore
{
    private FrozenDictionary<string, FrozenDictionary<string, IReadOnlyDictionary<ImageType, string>>> _byUser =
        FrozenDictionary<string, FrozenDictionary<string, IReadOnlyDictionary<ImageType, string>>>.Empty;

    private FrozenDictionary<string, AccountPoster> _byTag = FrozenDictionary<string, AccountPoster>.Empty;

    /// <summary>Gets the placeholder blur for a picture file, once worked out.</summary>
    internal ConcurrentDictionary<string, string> BlurHashes { get; } = new(StringComparer.Ordinal);

    /// <summary>Gets a value indicating whether any account has a picture of its own.</summary>
    public bool Any => _byTag.Count > 0;

    /// <summary>
    /// Replaces what's known with Curatarr's latest list.
    /// </summary>
    /// <param name="posters">Every account's own pictures.</param>
    public void Replace(IEnumerable<AccountPoster> posters)
    {
        ArgumentNullException.ThrowIfNull(posters);
        var list = posters.ToList();
        _byUser = list
            .GroupBy(p => p.User, StringComparer.OrdinalIgnoreCase)
            .ToFrozenDictionary(
                g => g.Key,
                g => g.GroupBy(p => p.Item, StringComparer.OrdinalIgnoreCase)
                    .ToFrozenDictionary(
                        i => i.Key,
                        i => (IReadOnlyDictionary<ImageType, string>)i
                            .GroupBy(p => p.Type)
                            .ToFrozenDictionary(k => k.Key, k => k.Last().Tag),
                        StringComparer.OrdinalIgnoreCase),
                StringComparer.OrdinalIgnoreCase);
        _byTag = list
            .GroupBy(p => p.Tag, StringComparer.OrdinalIgnoreCase)
            .ToFrozenDictionary(g => g.Key, g => g.Last(), StringComparer.OrdinalIgnoreCase);
    }

    /// <summary>
    /// The account's pictures by title, or null when it has none.
    /// </summary>
    /// <param name="userId">The account.</param>
    /// <returns>Title id → its pictures' tags by type.</returns>
    public IReadOnlyDictionary<string, IReadOnlyDictionary<ImageType, string>>? ForUser(Guid userId)
        => _byUser.TryGetValue(userId.ToString("N"), out var titles) ? titles : null;

    /// <summary>
    /// The picture a tag stands for, if it's one of Curatarr's.
    /// </summary>
    /// <param name="tag">The tag in the image request.</param>
    /// <param name="poster">The picture.</param>
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
