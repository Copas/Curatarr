using System.Reflection;
using Jellyfin.Plugin.Curatarr.Configuration;
using Jellyfin.Plugin.Curatarr.Deletes;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Dlna;
using MediaBrowser.Model.MediaInfo;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Curatarr.Playback;

/// <summary>
/// Runs before Jellyfin answers a player asking how to play a title (POST
/// /Items/{id}/PlaybackInfo, which every app sends with what it can play), and
/// points it at the copy and audio track it can play with the least work (see
/// <see cref="PlaybackChooser"/>). A track or copy the viewer picked is left alone.
/// </summary>
public sealed class PlaybackFilter : IAsyncActionFilter
{
    /// <summary>The claim with the device's id.</summary>
    internal const string DeviceIdClaim = "Jellyfin-DeviceId";

    private readonly ILibraryManager _libraryManager;
    private readonly IUserManager _userManager;
    private readonly IMediaSourceManager _mediaSources;
    private readonly ILogger<PlaybackFilter> _logger;
    private readonly PlaybackChooser _chooser;
    private readonly Func<PluginConfiguration?> _config;

    /// <summary>
    /// Initializes a new instance of the <see cref="PlaybackFilter"/> class.
    /// </summary>
    /// <param name="libraryManager">Finds the title.</param>
    /// <param name="userManager">Finds the viewer.</param>
    /// <param name="mediaSources">The title's copies and tracks.</param>
    /// <param name="logger">The log.</param>
    public PlaybackFilter(ILibraryManager libraryManager, IUserManager userManager, IMediaSourceManager mediaSources, ILogger<PlaybackFilter> logger)
        : this(libraryManager, userManager, mediaSources, logger, () => Plugin.Instance?.Configuration)
    {
    }

    internal PlaybackFilter(ILibraryManager libraryManager, IUserManager userManager, IMediaSourceManager mediaSources, ILogger<PlaybackFilter> logger, Func<PluginConfiguration?> config)
    {
        _libraryManager = libraryManager;
        _userManager = userManager;
        _mediaSources = mediaSources;
        _logger = logger;
        _chooser = new PlaybackChooser(logger);
        _config = config;
    }

    /// <inheritdoc />
    public async Task OnActionExecutionAsync(ActionExecutingContext context, ActionExecutionDelegate next)
    {
        ArgumentNullException.ThrowIfNull(context);
        ArgumentNullException.ThrowIfNull(next);
        PlaybackChoice? choice = null;
        if (_config()?.ChoosePlayable != false
            && context.ActionDescriptor is ControllerActionDescriptor action
            && action.ControllerName == "MediaInfo"
            && action.ActionName == "GetPostedPlaybackInfo")
        {
            try
            {
                choice = Choose(context);
            }
            catch (Exception ex) when (ex is ArgumentException or InvalidOperationException or NullReferenceException or TargetInvocationException)
            {
                // Never stand in the way of playback: Jellyfin answers as it would.
                _logger.LogWarning(ex, "Couldn't choose what to play; Jellyfin chooses as usual");
            }
        }

        var executed = await next().ConfigureAwait(false);
        if (choice is not null && executed.Result is ObjectResult { Value: PlaybackInfoResponse response })
        {
            Apply(response, choice);
        }
    }

    private PlaybackChoice? Choose(ActionExecutingContext context)
    {
        var args = context.ActionArguments;
        if (!args.TryGetValue("itemId", out var raw) || raw is not Guid itemId)
        {
            return null;
        }

        args.TryGetValue("playbackInfoDto", out var dto);
        var profile = Read<DeviceProfile>(dto, "DeviceProfile");
        if (profile is null)
        {
            return null; // the app didn't say what it can play
        }

        var item = _libraryManager.GetItemById(itemId);
        // The signed-in viewer, or (with an API key) the user the request names.
        var claim = context.HttpContext.User.FindFirst(DeleteFilter.UserIdClaim)?.Value;
        Guid? userId = Guid.TryParse(claim, out var signedIn) && signedIn != Guid.Empty
            ? signedIn
            : Arg<Guid?>(args, "userId") ?? Read<Guid?>(dto, "UserId");
        var user = userId is Guid id ? _userManager.GetUserById(id) : null;
        if (item is null || user is null)
        {
            return null;
        }

        var sources = _mediaSources.GetStaticMediaSources(item, true, user);
        var sourceId = Arg<string>(args, "mediaSourceId") ?? Read<string>(dto, "MediaSourceId");
        var audio = Arg<int?>(args, "audioStreamIndex") ?? Read<int?>(dto, "AudioStreamIndex");
        var source = string.IsNullOrEmpty(sourceId)
            ? (sources.Count > 0 ? sources[0] : null)
            : sources.FirstOrDefault(s => string.Equals(s.Id, sourceId, StringComparison.OrdinalIgnoreCase));
        if (source is null || (audio is not null && audio != source.DefaultAudioStreamIndex))
        {
            return null; // the viewer picked a track: theirs stands
        }

        var choice = _chooser.Choose(
            itemId,
            sources,
            profile,
            sourceId,
            Arg<int?>(args, "maxStreamingBitrate") ?? Read<int?>(dto, "MaxStreamingBitrate"),
            Arg<int?>(args, "maxAudioChannels") ?? Read<int?>(dto, "MaxAudioChannels"),
            context.HttpContext.User.FindFirst(DeviceIdClaim)?.Value);
        if (choice is null)
        {
            return null;
        }

        args["audioStreamIndex"] = choice.AudioStreamIndex;
        Write(dto, "AudioStreamIndex", choice.AudioStreamIndex);
        if (sources.Count > 1)
        {
            args["mediaSourceId"] = choice.MediaSourceId;
            Write(dto, "MediaSourceId", choice.MediaSourceId);
        }

        _logger.LogInformation("{Item} ({Name}): playing copy {Source}, audio track {Audio} ({Method}) instead of Jellyfin's first choice", itemId, item.Name, choice.MediaSourceId, choice.AudioStreamIndex, choice.PlayMethod);
        return choice;
    }

    /// <summary>
    /// The chosen copy first, with the chosen track as its default, so the app
    /// plays it and shows it selected.
    /// </summary>
    internal static void Apply(PlaybackInfoResponse response, PlaybackChoice choice)
    {
        var sources = response.MediaSources?.ToList() ?? [];
        var chosen = sources.FirstOrDefault(s => string.Equals(s.Id, choice.MediaSourceId, StringComparison.OrdinalIgnoreCase));
        if (chosen is null)
        {
            return;
        }

        chosen.DefaultAudioStreamIndex = choice.AudioStreamIndex;
        sources.Remove(chosen);
        sources.Insert(0, chosen);
        response.MediaSources = [.. sources];
    }

    private static T? Arg<T>(IDictionary<string, object?> args, string name)
        => args.TryGetValue(name, out var value) && value is T typed ? typed : default;

    private static T? Read<T>(object? target, string property)
        => target?.GetType().GetProperty(property)?.GetValue(target) is T typed ? typed : default;

    private static void Write(object? target, string property, object? value)
    {
        var info = target?.GetType().GetProperty(property);
        if (info is not null && info.CanWrite)
        {
            info.SetValue(target, value);
        }
    }
}
