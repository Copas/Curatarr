using Jellyfin.Plugin.Curatarr.Configuration;
using MediaBrowser.Controller.Library;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Curatarr.Deletes;

/// <summary>
/// Runs before Jellyfin's own delete (DELETE /Items/{id} and DELETE /Items?ids=),
/// which every Jellyfin app uses. Each item the user may delete is put to Curatarr:
/// a film or show it looks after becomes Leaving Soon and isn't deleted, or, if it
/// already is, is deleted through Radarr or Sonarr; anything else is deleted by
/// Jellyfin as usual. The web app, which has a Leaving Soon menu item of its own,
/// deletes straight away.
/// </summary>
public sealed class DeleteFilter : IAsyncActionFilter
{
    /// <summary>The claim Jellyfin's authentication puts the user's id in.</summary>
    internal const string UserIdClaim = "Jellyfin-UserId";

    /// <summary>The claim with the app's name, e.g. "Jellyfin Web".</summary>
    internal const string ClientClaim = "Jellyfin-Client";

    private readonly ILibraryManager _libraryManager;
    private readonly IUserManager _userManager;
    private readonly ICuratarrClient _curatarr;
    private readonly ILogger<DeleteFilter> _logger;
    private readonly Func<PluginConfiguration?> _config;

    /// <summary>
    /// Initializes a new instance of the <see cref="DeleteFilter"/> class.
    /// </summary>
    /// <param name="libraryManager">Finds the items.</param>
    /// <param name="userManager">Finds the user.</param>
    /// <param name="curatarr">Asks Curatarr.</param>
    /// <param name="logger">The log.</param>
    public DeleteFilter(ILibraryManager libraryManager, IUserManager userManager, ICuratarrClient curatarr, ILogger<DeleteFilter> logger)
        : this(libraryManager, userManager, curatarr, logger, () => Plugin.Instance?.Configuration)
    {
    }

    internal DeleteFilter(ILibraryManager libraryManager, IUserManager userManager, ICuratarrClient curatarr, ILogger<DeleteFilter> logger, Func<PluginConfiguration?> config)
    {
        _libraryManager = libraryManager;
        _userManager = userManager;
        _curatarr = curatarr;
        _logger = logger;
        _config = config;
    }

    /// <inheritdoc />
    public async Task OnActionExecutionAsync(ActionExecutingContext context, ActionExecutionDelegate next)
    {
        ArgumentNullException.ThrowIfNull(context);
        ArgumentNullException.ThrowIfNull(next);
        var config = _config();
        if (config is null
            || !config.InterceptDeletes
            || string.IsNullOrWhiteSpace(config.CuratarrUrl)
            || context.ActionDescriptor is not ControllerActionDescriptor action
            || action.ControllerName != "Library"
            || (action.ActionName != "DeleteItem" && action.ActionName != "DeleteItems"))
        {
            await next().ConfigureAwait(false);
            return;
        }

        var (argument, ids) = ItemIds(context);
        var user = UserId(context) is Guid userId ? _userManager.GetUserById(userId) : null;
        if (argument is null || user is null)
        {
            if (argument is null)
            {
                _logger.LogWarning("Jellyfin's {Action} has no item ids this plugin knows; deleting as usual", action.ActionName);
            }

            await next().ConfigureAwait(false);
            return;
        }

        var web = WebMenu.Active && context.HttpContext.User.FindFirst(ClientClaim)?.Value == WebMenu.Client;
        var mode = web ? Modes.Now : Modes.Delete;
        var toJellyfin = new List<Guid>();
        string? refused = null;
        foreach (var id in ids)
        {
            var item = _libraryManager.GetItemById(id);
            if (item is null || !item.CanDelete(user))
            {
                toJellyfin.Add(id); // Jellyfin answers these as it always does
                continue;
            }

            var answer = await _curatarr.AskAsync(config, id, user.Username, mode, context.HttpContext.RequestAborted).ConfigureAwait(false);
            if (answer is null)
            {
                if (config.BlockWhenUnreachable)
                {
                    refused ??= "Curatarr couldn't be reached, so nothing was deleted.";
                }
                else
                {
                    toJellyfin.Add(id);
                }
            }
            else if (!answer.Handled)
            {
                toJellyfin.Add(id);
            }
            else if (answer.Refused is not null)
            {
                refused ??= answer.Refused;
                _logger.LogInformation("Curatarr kept {Item} ({Name}) deleted by {User}: {Reason}", id, item.Name, user.Username, answer.Refused);
            }
            else if (answer.Result == "deleting")
            {
                _logger.LogInformation("{Item} ({Name}) deleted by {User} is being deleted by Curatarr", id, item.Name, user.Username);
            }
            else
            {
                _logger.LogInformation("{Item} ({Name}) deleted by {User} is Leaving Soon in Curatarr instead", id, item.Name, user.Username);
            }
        }

        if (refused is not null)
        {
            context.Result = new ObjectResult(new ProblemDetails { Title = "Kept by Curatarr", Detail = refused, Status = 409 })
            {
                StatusCode = 409,
            };
            return;
        }

        if (toJellyfin.Count == 0)
        {
            context.Result = new NoContentResult();
            return;
        }

        if (toJellyfin.Count < ids.Count)
        {
            context.ActionArguments[argument] = toJellyfin.ToArray();
        }

        await next().ConfigureAwait(false);
    }

    private static (string? Argument, IReadOnlyList<Guid> Ids) ItemIds(ActionExecutingContext context)
    {
        foreach (var (name, value) in context.ActionArguments)
        {
            switch (value)
            {
                case Guid id when name.Equals("itemId", StringComparison.OrdinalIgnoreCase):
                    return (name, [id]);
                case IEnumerable<Guid> many when name.Equals("ids", StringComparison.OrdinalIgnoreCase):
                    return (name, many.ToList());
            }
        }

        return (null, []);
    }

    private static Guid? UserId(ActionExecutingContext context)
    {
        var claim = context.HttpContext.User.FindFirst(UserIdClaim)?.Value;
        return Guid.TryParse(claim, out var id) ? id : null;
    }
}
