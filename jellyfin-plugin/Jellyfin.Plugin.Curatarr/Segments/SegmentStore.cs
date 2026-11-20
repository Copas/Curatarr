using System.Collections.Concurrent;
using System.Text.Json;
using Jellyfin.Database.Implementations.Enums;

namespace Jellyfin.Plugin.Curatarr.Segments;

/// <summary>
/// One skip segment as Curatarr lists it: Jellyfin's type name and seconds.
/// </summary>
/// <param name="Type">Intro, Outro, Recap, Preview or Commercial.</param>
/// <param name="Start">Where it starts, in seconds.</param>
/// <param name="End">Where it ends, in seconds.</param>
public sealed record CuratarrSegment(string Type, double Start, double End)
{
    /// <summary>Gets Jellyfin's type for it (Unknown when the name isn't one).</summary>
    public MediaSegmentType Kind => Enum.TryParse<MediaSegmentType>(Type, true, out var kind) ? kind : MediaSegmentType.Unknown;
}

/// <summary>
/// Every item's skip segments as Curatarr keeps them, by Jellyfin item id
/// without dashes, and how far through Curatarr's changes they are. Kept in a
/// file in the plugin's data folder, so a restart doesn't start from nothing.
/// </summary>
public sealed class SegmentStore
{
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);
    private readonly ConcurrentDictionary<string, IReadOnlyList<CuratarrSegment>> _items = new(StringComparer.OrdinalIgnoreCase);
    private readonly string? _file;

    /// <summary>
    /// Initializes a new instance of the <see cref="SegmentStore"/> class,
    /// reading what was kept.
    /// </summary>
    /// <param name="file">The file it's kept in; none keeps it in memory only.</param>
    public SegmentStore(string? file = null)
    {
        _file = file;
        if (file is null || !File.Exists(file))
        {
            return;
        }

        try
        {
            var kept = JsonSerializer.Deserialize<Kept>(File.ReadAllText(file), Json);
            if (kept?.Items is not null)
            {
                foreach (var (item, segments) in kept.Items)
                {
                    _items[item] = segments;
                }

                Version = kept.Version;
            }
        }
        catch (Exception ex) when (ex is JsonException or IOException)
        {
            // Unreadable: fetched again from the start.
        }
    }

    /// <summary>Gets the last of Curatarr's changes taken in.</summary>
    public long Version { get; private set; }

    /// <summary>Gets a value indicating whether any segments are known.</summary>
    public bool Any => !_items.IsEmpty;

    /// <summary>
    /// The segments of one item.
    /// </summary>
    /// <param name="itemId">Its Jellyfin id.</param>
    /// <returns>Its segments; none when Curatarr has none for it.</returns>
    public IReadOnlyList<CuratarrSegment> For(Guid itemId)
        => _items.TryGetValue(itemId.ToString("N"), out var segments) ? segments : [];

    /// <summary>
    /// Takes in a page of Curatarr's changes: each item listed has exactly the
    /// segments given (none removes them all).
    /// </summary>
    /// <param name="items">The items that changed.</param>
    /// <param name="version">How far through the changes this page reaches.</param>
    public void Apply(IEnumerable<ChangedItem> items, long version)
    {
        ArgumentNullException.ThrowIfNull(items);
        foreach (var changed in items)
        {
            var key = changed.Item.Replace("-", string.Empty, StringComparison.Ordinal);
            if (changed.Segments is { Count: > 0 })
            {
                _items[key] = changed.Segments;
            }
            else
            {
                _items.TryRemove(key, out _);
            }
        }

        Version = Math.Max(Version, version);
        Save();
    }

    private void Save()
    {
        if (_file is null)
        {
            return;
        }

        var temporary = _file + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(new Kept(Version, new Dictionary<string, IReadOnlyList<CuratarrSegment>>(_items)), Json));
        File.Move(temporary, _file, true);
    }

    private sealed record Kept(long Version, Dictionary<string, IReadOnlyList<CuratarrSegment>>? Items);
}

/// <summary>
/// An item whose segments changed, with all of them as they are now.
/// </summary>
/// <param name="Item">Its Jellyfin id, without dashes.</param>
/// <param name="Segments">Its segments.</param>
public sealed record ChangedItem(string Item, List<CuratarrSegment>? Segments);
