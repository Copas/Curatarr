using Jellyfin.Plugin.Curatarr.Configuration;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.MediaSegments;
using MediaBrowser.Model;
using MediaBrowser.Model.MediaSegments;

namespace Jellyfin.Plugin.Curatarr.Segments;

/// <summary>
/// Gives Jellyfin the skip segments Curatarr keeps (intros, credits, recaps,
/// previews), in place of Intro Skipper's. Turned on per library in
/// Jellyfin's library settings, like any segment provider.
/// </summary>
public sealed class CuratarrSegmentProvider : IMediaSegmentProvider
{
    private readonly SegmentStore _store;
    private readonly Func<PluginConfiguration?> _config;

    /// <summary>
    /// Initializes a new instance of the <see cref="CuratarrSegmentProvider"/> class.
    /// </summary>
    /// <param name="store">The segments Curatarr keeps.</param>
    public CuratarrSegmentProvider(SegmentStore store)
        : this(store, () => Plugin.Instance?.Configuration)
    {
    }

    /// <summary>
    /// Initializes a new instance of the <see cref="CuratarrSegmentProvider"/> class.
    /// </summary>
    /// <param name="store">The segments Curatarr keeps.</param>
    /// <param name="config">The plugin's settings.</param>
    internal CuratarrSegmentProvider(SegmentStore store, Func<PluginConfiguration?> config)
    {
        _store = store;
        _config = config;
    }

    /// <inheritdoc />
    public string Name => "Curatarr";

    /// <inheritdoc />
    public ValueTask<bool> Supports(BaseItem item)
        => ValueTask.FromResult(item is Episode or Movie && _config()?.SkipSegments == true);

    /// <inheritdoc />
    public Task<IReadOnlyList<MediaSegmentDto>> GetMediaSegments(MediaSegmentGenerationRequest request, CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);
        IReadOnlyList<MediaSegmentDto> segments = _config()?.SkipSegments == true
            ? _store.For(request.ItemId)
                .Where(s => s.End > s.Start)
                .Select(s => new MediaSegmentDto
                {
                    ItemId = request.ItemId,
                    Type = s.Kind,
                    StartTicks = TimeSpan.FromSeconds(s.Start).Ticks,
                    EndTicks = TimeSpan.FromSeconds(s.End).Ticks,
                })
                .ToList()
            : [];
        return Task.FromResult(segments);
    }

    /// <inheritdoc />
    public Task CleanupExtractedData(Guid itemId, CancellationToken cancellationToken) => Task.CompletedTask;
}
