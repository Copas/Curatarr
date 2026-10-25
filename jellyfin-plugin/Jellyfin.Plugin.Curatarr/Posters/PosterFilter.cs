using Jellyfin.Plugin.Curatarr.Configuration;
using Jellyfin.Plugin.Curatarr.Deletes;
using MediaBrowser.Controller.Drawing;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Drawing;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.Querying;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Curatarr.Posters;

/// <summary>
/// Shows an account its own posters (Curatarr's custom posters for one account).
///
/// Apps fetch a poster from <c>/Items/{id}/Images/Primary?tag=…</c> without saying
/// who's asking, but they take the tag from the item details they were sent, and
/// those requests are signed in. So: in what's sent to an account with posters of
/// its own, a title's poster tag is swapped for one standing for that account's
/// poster; and when an app asks for an image with one of those tags, this serves
/// the poster (resized and cached by Jellyfin, as its own posters are). Every
/// other account gets the usual tag and the usual poster.
/// </summary>
public sealed class PosterFilter : IAsyncActionFilter
{
    private static readonly HashSet<string> ImageActions = new(StringComparer.Ordinal)
    {
        "GetItemImage",
        "GetItemImage2",
        "GetItemImageByIndex",
        "HeadItemImage",
        "HeadItemImage2",
        "HeadItemImageByIndex",
    };

    private readonly PosterStore _store;
    private readonly ILibraryManager _libraryManager;
    private readonly IImageProcessor _imageProcessor;
    private readonly ILogger<PosterFilter> _logger;
    private readonly Func<PluginConfiguration?> _config;

    /// <summary>
    /// Initializes a new instance of the <see cref="PosterFilter"/> class.
    /// </summary>
    /// <param name="store">The accounts' posters.</param>
    /// <param name="libraryManager">Finds the title.</param>
    /// <param name="imageProcessor">Resizes pictures as Jellyfin does its own.</param>
    /// <param name="logger">The log.</param>
    public PosterFilter(PosterStore store, ILibraryManager libraryManager, IImageProcessor imageProcessor, ILogger<PosterFilter> logger)
        : this(store, libraryManager, imageProcessor, logger, () => Plugin.Instance?.Configuration)
    {
    }

    internal PosterFilter(PosterStore store, ILibraryManager libraryManager, IImageProcessor imageProcessor, ILogger<PosterFilter> logger, Func<PluginConfiguration?> config)
    {
        _store = store;
        _libraryManager = libraryManager;
        _imageProcessor = imageProcessor;
        _logger = logger;
        _config = config;
    }

    /// <inheritdoc />
    public async Task OnActionExecutionAsync(ActionExecutingContext context, ActionExecutionDelegate next)
    {
        ArgumentNullException.ThrowIfNull(context);
        ArgumentNullException.ThrowIfNull(next);
        if (_config()?.CustomPosters == false || !_store.Any)
        {
            await next().ConfigureAwait(false);
            return;
        }

        if (context.ActionDescriptor is ControllerActionDescriptor { ControllerName: "Image" } action
            && ImageActions.Contains(action.ActionName))
        {
            var served = await ServeAsync(context).ConfigureAwait(false);
            if (served is not null)
            {
                context.Result = served;
                return;
            }

            await next().ConfigureAwait(false);
            return;
        }

        var executed = await next().ConfigureAwait(false);
        var claim = context.HttpContext.User.FindFirst(DeleteFilter.UserIdClaim)?.Value;
        if (executed.Result is ObjectResult { Value: { } value }
            && Guid.TryParse(claim, out var userId)
            && _store.ForUser(userId) is { } posters)
        {
            foreach (var dto in Items(value))
            {
                Swap(dto, posters);
            }
        }
    }

    /// <summary>
    /// The item details in an answer: one, a list, or a page of them.
    /// </summary>
    internal static IEnumerable<BaseItemDto> Items(object value) => value switch
    {
        BaseItemDto one => [one],
        QueryResult<BaseItemDto> page => page.Items ?? [],
        IEnumerable<BaseItemDto> many => many,
        _ => [],
    };

    /// <summary>
    /// Puts the account's poster tags in one item's details: its own poster, and
    /// its show's or parent's where an app shows that instead (an episode's card).
    /// Where the poster has a landscape version, its tag goes in for the Thumb too,
    /// which apps show in rows of wide tiles (in place of the title's own Thumb or,
    /// when it has none, its backdrop).
    /// </summary>
    internal void Swap(BaseItemDto dto, IReadOnlyDictionary<string, string> posters)
    {
        if (posters.TryGetValue(dto.Id.ToString("N"), out var tag))
        {
            dto.ImageTags ??= [];
            dto.ImageTags[ImageType.Primary] = tag;
            Blur(dto, tag, ImageType.Primary);
            if (IsWide(tag))
            {
                dto.ImageTags[ImageType.Thumb] = tag;
                Blur(dto, tag, ImageType.Thumb);
            }
        }

        if (dto.SeriesId is Guid series && posters.TryGetValue(series.ToString("N"), out var seriesTag))
        {
            dto.SeriesPrimaryImageTag = seriesTag;
            if (IsWide(seriesTag))
            {
                dto.SeriesThumbImageTag = seriesTag;
            }
        }

        if (dto.ParentPrimaryImageItemId is Guid parentId
            && posters.TryGetValue(parentId.ToString("N"), out var parentTag))
        {
            dto.ParentPrimaryImageTag = parentTag;
        }

        if (dto.ParentThumbItemId is Guid thumbId
            && posters.TryGetValue(thumbId.ToString("N"), out var thumbTag)
            && IsWide(thumbTag))
        {
            dto.ParentThumbImageTag = thumbTag;
        }
    }

    private bool IsWide(string tag) => _store.TryGetByTag(tag, out var poster) && poster.Wide;

    private void Blur(BaseItemDto dto, string tag, ImageType type)
    {
        if (dto.ImageBlurHashes is null || !dto.ImageBlurHashes.TryGetValue(type, out var hashes))
        {
            return; // the app didn't ask for placeholders
        }

        var file = type == ImageType.Thumb ? PosterSync.WideFileFor(tag) : PosterSync.FileFor(tag);
        var hash = _store.BlurHashes.GetOrAdd(file, f =>
        {
            try
            {
                return _imageProcessor.GetImageBlurHash(f);
            }
            catch (Exception ex) when (ex is IOException or InvalidOperationException or ArgumentException)
            {
                return string.Empty;
            }
        });
        if (!string.IsNullOrEmpty(hash))
        {
            hashes[tag] = hash;
        }
    }

    private async Task<IActionResult?> ServeAsync(ActionExecutingContext context)
    {
        var args = context.ActionArguments;
        var tag = args.TryGetValue("tag", out var raw) ? raw as string : null;
        if (!_store.TryGetByTag(tag, out var poster)
            || !args.TryGetValue("itemId", out var rawId) || rawId is not Guid itemId
            || !string.Equals(itemId.ToString("N"), poster.Item, StringComparison.OrdinalIgnoreCase)
            || (args.TryGetValue("imageType", out var type) && type is ImageType kind && kind is not (ImageType.Primary or ImageType.Thumb)))
        {
            return null; // not one of ours: Jellyfin serves it
        }

        var wide = type is ImageType.Thumb;
        if (wide && !poster.Wide)
        {
            return null;
        }

        var file = wide ? PosterSync.WideFileFor(poster.Tag) : PosterSync.FileFor(poster.Tag);
        if (!wide && poster.Large && IsLarge(args, _config()?.LargePosterWidth ?? PluginConfiguration.DefaultLargePosterWidth)
            && File.Exists(PosterSync.LargeFileFor(poster.Tag)))
        {
            file = PosterSync.LargeFileFor(poster.Tag);
        }

        if (!File.Exists(file))
        {
            return null;
        }

        var item = _libraryManager.GetItemById(itemId);
        if (item is null)
        {
            return null;
        }

        try
        {
            var options = new ImageProcessingOptions
            {
                ItemId = itemId,
                Item = item,
                Image = new ItemImageInfo { Path = file, Type = wide ? ImageType.Thumb : ImageType.Primary, DateModified = File.GetLastWriteTimeUtc(file) },
                ImageIndex = 0,
                Width = Arg<int?>(args, "width"),
                Height = Arg<int?>(args, "height"),
                MaxWidth = Arg<int?>(args, "maxWidth"),
                MaxHeight = Arg<int?>(args, "maxHeight"),
                FillWidth = Arg<int?>(args, "fillWidth"),
                FillHeight = Arg<int?>(args, "fillHeight"),
                Quality = Arg<int?>(args, "quality") ?? 90,
                SupportedOutputFormats = [ImageFormat.Jpg],
            };
            var (path, mime, _) = await _imageProcessor.ProcessImage(options).ConfigureAwait(false);
            return new PhysicalFileResult(path, mime ?? "image/jpeg");
        }
        catch (Exception ex) when (ex is IOException or InvalidOperationException or ArgumentException or NotSupportedException)
        {
            // Resizing failed: the picture as it is still beats the wrong one.
            _logger.LogWarning(ex, "Couldn't resize the account poster {Tag}; sending it as it is", poster.Tag);
            return new PhysicalFileResult(file, "image/jpeg");
        }
    }

    /// <summary>
    /// Whether an app wants a poster big: on a title's own page rather than in a
    /// row of posters. Apps ask for a size in pixels; this is the width the poster
    /// would be drawn at (a height counts as the width of a poster that tall).
    /// The Roku app asks for at most 295 wide in lists and 300 on a title's page.
    /// </summary>
    /// <param name="args">The image request's arguments.</param>
    /// <param name="from">The width from which a poster is big.</param>
    /// <returns>Whether it's big.</returns>
    internal static bool IsLarge(IDictionary<string, object?> args, int from)
    {
        static int Width(int? width, int? height) => (width, height) switch
        {
            (int w, int h) => Math.Min(w, h * 2 / 3),
            (int w, null) => w,
            (null, int h) => h * 2 / 3,
            _ => 0,
        };

        var size = new[]
        {
            Width(Arg<int?>(args, "width"), Arg<int?>(args, "height")),
            Width(Arg<int?>(args, "fillWidth"), Arg<int?>(args, "fillHeight")),
            Width(Arg<int?>(args, "maxWidth"), Arg<int?>(args, "maxHeight")),
        }.Max();
        return size >= from;
    }

    private static T? Arg<T>(IDictionary<string, object?> args, string name)
        => args.TryGetValue(name, out var value) && value is T typed ? typed : default;
}
