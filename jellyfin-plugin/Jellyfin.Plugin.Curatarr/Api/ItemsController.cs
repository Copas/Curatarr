using Jellyfin.Plugin.Curatarr.Configuration;
using Jellyfin.Plugin.Curatarr.Deletes;
using MediaBrowser.Controller.Library;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;

namespace Jellyfin.Plugin.Curatarr.Api;

/// <summary>
/// The web app's Leaving Soon and Leave after I watch menu items, for anyone
/// Jellyfin lets delete the item.
/// </summary>
[ApiController]
[Route("Curatarr/Items")]
[Authorize]
public class ItemsController : ControllerBase
{
    private readonly ILibraryManager _libraryManager;
    private readonly IUserManager _userManager;
    private readonly ICuratarrClient _curatarr;
    private readonly Func<PluginConfiguration?> _config;

    /// <summary>
    /// Initializes a new instance of the <see cref="ItemsController"/> class.
    /// </summary>
    /// <param name="libraryManager">Finds the items.</param>
    /// <param name="userManager">Finds the user.</param>
    /// <param name="curatarr">Asks Curatarr.</param>
    public ItemsController(ILibraryManager libraryManager, IUserManager userManager, ICuratarrClient curatarr)
        : this(libraryManager, userManager, curatarr, () => Plugin.Instance?.Configuration)
    {
    }

    internal ItemsController(ILibraryManager libraryManager, IUserManager userManager, ICuratarrClient curatarr, Func<PluginConfiguration?> config)
    {
        _libraryManager = libraryManager;
        _userManager = userManager;
        _curatarr = curatarr;
        _config = config;
    }

    /// <summary>
    /// Whether the menu offers Leaving Soon for this item.
    /// </summary>
    /// <param name="itemId">The item.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>Whether it applies.</returns>
    [HttpGet("{itemId}")]
    public async Task<ActionResult<ItemStatus>> Status([FromRoute] Guid itemId, CancellationToken cancellationToken)
    {
        var (config, userName) = Allowed(itemId);
        if (config is null || userName is null)
        {
            return new ItemStatus(false, false);
        }

        var answer = await _curatarr.AskAsync(config, itemId, userName, Modes.Status, cancellationToken).ConfigureAwait(false);
        var handled = answer is { Handled: true, Deleting: false };
        return new ItemStatus(
            handled && !answer!.LeavingSoon,
            handled && answer!.LeavingSoon,
            answer?.CanLeaveAfterWatched ?? false,
            answer?.LeavesAfterWatched ?? false);
    }

    /// <summary>
    /// Leave after I watch: the item is marked Leaving Soon once this account has
    /// watched it (a film, show, season or episode). With <paramref name="undo"/>,
    /// it isn't any more.
    /// </summary>
    /// <param name="itemId">The item.</param>
    /// <param name="undo">Clears it instead.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>What happened, as a sentence to show.</returns>
    [HttpPost("{itemId}/LeaveAfterWatched")]
    public async Task<ActionResult<LeavingSoonResult>> LeaveAfterWatched([FromRoute] Guid itemId, [FromQuery] bool undo, CancellationToken cancellationToken)
    {
        var (config, userName) = Allowed(itemId);
        if (config is null || userName is null)
        {
            return Forbid();
        }

        var mode = undo ? Modes.KeepAfterWatched : Modes.LeaveAfterWatched;
        var answer = await _curatarr.AskAsync(config, itemId, userName, mode, cancellationToken).ConfigureAwait(false);
        return answer switch
        {
            null => new LeavingSoonResult(false, "Curatarr couldn't be reached."),
            { Handled: false } => new LeavingSoonResult(false, "Curatarr doesn't look after this."),
            { Refused: not null } => new LeavingSoonResult(false, answer.Refused),
            _ => new LeavingSoonResult(true, answer.Message ?? "Done."),
        };
    }

    /// <summary>
    /// Marks the item Leaving Soon.
    /// </summary>
    /// <param name="itemId">The item.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>What happened, as a sentence to show.</returns>
    [HttpPost("{itemId}/LeavingSoon")]
    public async Task<ActionResult<LeavingSoonResult>> LeavingSoon([FromRoute] Guid itemId, CancellationToken cancellationToken)
    {
        var (config, userName) = Allowed(itemId);
        if (config is null || userName is null)
        {
            return Forbid();
        }

        var answer = await _curatarr.AskAsync(config, itemId, userName, Modes.LeavingSoon, cancellationToken).ConfigureAwait(false);
        return answer switch
        {
            null => new LeavingSoonResult(false, "Curatarr couldn't be reached."),
            { Handled: false } => new LeavingSoonResult(false, "Curatarr doesn't look after this."),
            { Refused: not null } => new LeavingSoonResult(false, answer.Refused),
            _ => new LeavingSoonResult(answer.Result != "already", answer.Message ?? "Marked Leaving Soon."),
        };
    }

    private (PluginConfiguration? Config, string? UserName) Allowed(Guid itemId)
    {
        var config = _config();
        if (config is null || !config.InterceptDeletes || string.IsNullOrWhiteSpace(config.CuratarrUrl))
        {
            return (null, null);
        }

        var claim = User.FindFirst(DeleteFilter.UserIdClaim)?.Value;
        var user = Guid.TryParse(claim, out var userId) ? _userManager.GetUserById(userId) : null;
        var item = _libraryManager.GetItemById(itemId);
        if (user is null || item is null || !item.CanDelete(user))
        {
            return (config, null);
        }

        return (config, user.Username);
    }
}

/// <summary>
/// Whether the web app's menu offers Leaving Soon.
/// </summary>
/// <param name="CanLeave">Curatarr looks after it, it isn't Leaving Soon yet, and the user may delete it.</param>
/// <param name="LeavingSoon">It's Leaving Soon already.</param>
/// <param name="CanLeaveAfterWatched">Leave after I watch can be set on it.</param>
/// <param name="LeavesAfterWatched">It's set.</param>
public sealed record ItemStatus(bool CanLeave, bool LeavingSoon, bool CanLeaveAfterWatched = false, bool LeavesAfterWatched = false);

/// <summary>
/// The answer to Leaving Soon.
/// </summary>
/// <param name="Ok">Whether it's now Leaving Soon.</param>
/// <param name="Message">What to show.</param>
public sealed record LeavingSoonResult(bool Ok, string Message);
