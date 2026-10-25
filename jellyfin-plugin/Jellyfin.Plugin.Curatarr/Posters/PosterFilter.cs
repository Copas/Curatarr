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
    /// Puts the account's picture tags in one item's details: its poster, wide
    /// tile (Thumb), background, logo and banner, and its show's or parent's
    /// where an app shows those instead (an episode's card, a season's page).
    /// An older Curatarr's poster with a landscape version stands in for the
    /// Thumb, which apps show in rows of wide tiles (in place of the title's own
    /// Thumb or, when it has none, its backdrop).
    /// </summary>
    internal void Swap(BaseItemDto dto, IReadOnlyDictionary<string, IReadOnlyDictionary<ImageType, string>> posters)
    {
        string? Tag(Guid? id, ImageType type)
            => id is Guid found
                && posters.TryGetValue(found.ToString("N"), out var kinds)
                && kinds.TryGetValue(type, out var tag)
                ? tag
                : null;

        string? Thumb(Guid? id)
            => Tag(id, ImageType.Thumb) ?? (Tag(id, ImageType.Primary) is { } poster && IsWide(poster) ? poster : null);

        void Own(ImageType type, string? tag)
        {
            if (tag is null)
            {
                return;
            }

            dto.ImageTags ??= [];
            dto.ImageTags[type] = tag;
            Blur(dto, tag, type);
        }

        Own(ImageType.Primary, Tag(dto.Id, ImageType.Primary));
        Own(ImageType.Thumb, Thumb(dto.Id));
        Own(ImageType.Logo, Tag(dto.Id, ImageType.Logo));
        Own(ImageType.Banner, Tag(dto.Id, ImageType.Banner));
        if (Tag(dto.Id, ImageType.Backdrop) is { } backdrop)
        {
            dto.BackdropImageTags = First(backdrop, dto.BackdropImageTags);
            Blur(dto, backdrop, ImageType.Backdrop);
        }

        if (Tag(dto.SeriesId, ImageType.Primary) is { } seriesTag)
        {
            dto.SeriesPrimaryImageTag = seriesTag;
        }

        if (Thumb(dto.SeriesId) is { } seriesThumb)
        {
            dto.SeriesThumbImageTag = seriesThumb;
        }

        if (Tag(dto.ParentPrimaryImageItemId, ImageType.Primary) is { } parentTag)
        {
            dto.ParentPrimaryImageTag = parentTag;
        }

        if (Thumb(dto.ParentThumbItemId) is { } thumbTag)
        {
            dto.ParentThumbImageTag = thumbTag;
        }

        if (Tag(dto.ParentBackdropItemId, ImageType.Backdrop) is { } parentBackdrop)
        {
            dto.ParentBackdropImageTags = First(parentBackdrop, dto.ParentBackdropImageTags);
        }

        if (Tag(dto.ParentLogoItemId, ImageType.Logo) is { } parentLogo)
        {
            dto.ParentLogoImageTag = parentLogo;
        }
    }

    /// <summary>
    /// A title's backgrounds with the account's first, in place of Jellyfin's
    /// first (apps show the first; some rotate through the rest).
    /// </summary>
    private static string[] First(string tag, string[]? tags) => [tag, .. (tags ?? []).Skip(1)];

    private bool IsWide(string tag) => _store.TryGetByTag(tag, out var poster) && poster.Wide;

    private void Blur(BaseItemDto dto, string tag, ImageType type)
    {
        if (dto.ImageBlurHashes is null || !dto.ImageBlurHashes.TryGetValue(type, out var hashes))
        {
            return; // the app didn't ask for placeholders
        }

        var file = type == ImageType.Thumb && IsWide(tag) ? PosterSync.WideFileFor(tag)
            : PosterSync.FileFor(tag, type);
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
            || !string.Equals(itemId.ToString("N"), poster.Item, StringComparison.OrdinalIgnoreCase))
        {
            return null; // not one of ours: Jellyfin serves it
        }

        var asked = args.TryGetValue("imageType", out var type) && type is ImageType kind ? kind : ImageType.Primary;
        string file;
        if (asked == poster.Type)
        {
            file = PosterSync.FileFor(poster.Tag, asked);
        }
        else if (asked == ImageType.Thumb && poster.Type == ImageType.Primary && poster.Wide)
        {
            file = PosterSync.WideFileFor(poster.Tag); // an older Curatarr's landscape version
        }
        else
        {
            return null;
        }

        if (asked == ImageType.Primary && poster.Large && IsLarge(args, _config()?.LargePosterWidth ?? PluginConfiguration.DefaultLargePosterWidth)
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

        var logo = asked == ImageType.Logo;
        var fallbackMime = logo ? "image/png" : "image/jpeg";

        try
        {
            var options = new ImageProcessingOptions
            {
                ItemId = itemId,
                Item = item,
                Image = new ItemImageInfo { Path = file, Type = asked, DateModified = File.GetLastWriteTimeUtc(file) },
                ImageIndex = 0,
                Width = Arg<int?>(args, "width"),
                Height = Arg<int?>(args, "height"),
                MaxWidth = Arg<int?>(args, "maxWidth"),
                MaxHeight = Arg<int?>(args, "maxHeight"),
                FillWidth = Arg<int?>(args, "fillWidth"),
                FillHeight = Arg<int?>(args, "fillHeight"),
                Quality = Arg<int?>(args, "quality") ?? 90,
                // A logo keeps its see-through background.
                SupportedOutputFormats = [logo ? ImageFormat.Png : ImageFormat.Jpg],
            };
            var (path, mime, _) = await _imageProcessor.ProcessImage(options).ConfigureAwait(false);
            return new PhysicalFileResult(path, mime ?? fallbackMime);
        }
        catch (Exception ex) when (ex is IOException or InvalidOperationException or ArgumentException or NotSupportedException)
        {
            // Resizing failed: the picture as it is still beats the wrong one.
            _logger.LogWarning(ex, "Couldn't resize the account picture {Tag}; sending it as it is", poster.Tag);
            return new PhysicalFileResult(file, fallbackMime);
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
