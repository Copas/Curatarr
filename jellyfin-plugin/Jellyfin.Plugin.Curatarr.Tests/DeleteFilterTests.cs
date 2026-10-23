using System.Security.Claims;
using Jellyfin.Database.Implementations.Entities;
using Jellyfin.Plugin.Curatarr.Configuration;
using Jellyfin.Plugin.Curatarr.Deletes;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Library;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Abstractions;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.AspNetCore.Routing;
using Microsoft.Extensions.Logging.Abstractions;
using Moq;
using Xunit;

namespace Jellyfin.Plugin.Curatarr.Tests;

public class DeleteFilterTests
{
    private static readonly Guid UserId = Guid.NewGuid();
    private readonly User _user = new("Sam", "Default", "Default");
    private readonly Dictionary<Guid, FakeMovie> _items = new();
    private readonly Dictionary<Guid, DeleteAnswer?> _answers = new();
    private readonly List<Guid> _asked = new();
    private readonly List<string> _modes = new();
    private readonly PluginConfiguration _config = new() { CuratarrUrl = "http://curatarr", Token = "t" };

    private DeleteFilter Filter()
    {
        var library = new Mock<ILibraryManager>();
        library.Setup(l => l.GetItemById(It.IsAny<Guid>())).Returns((Guid id) => _items.GetValueOrDefault(id));
        var users = new Mock<IUserManager>();
        users.Setup(u => u.GetUserById(UserId)).Returns(_user);
        var curatarr = new Mock<ICuratarrClient>();
        curatarr
            .Setup(c => c.AskAsync(It.IsAny<PluginConfiguration>(), It.IsAny<Guid>(), "Sam", It.IsAny<string>(), It.IsAny<CancellationToken>()))
            .ReturnsAsync((PluginConfiguration _, Guid id, string _, string mode, CancellationToken _) =>
            {
                _asked.Add(id);
                _modes.Add(mode);
                return _answers.GetValueOrDefault(id);
            });
        return new DeleteFilter(library.Object, users.Object, curatarr.Object, NullLogger<DeleteFilter>.Instance, () => _config);
    }

    private Guid Item(DeleteAnswer? answer, bool mayDelete = true)
    {
        var id = Guid.NewGuid();
        _items[id] = new FakeMovie { Allowed = mayDelete };
        _answers[id] = answer;
        return id;
    }

    private static ActionExecutingContext Context(string action, string argument, object ids, string app = "Jellyfin Roku")
    {
        var http = new DefaultHttpContext
        {
            User = new ClaimsPrincipal(new ClaimsIdentity(
                [new Claim(DeleteFilter.UserIdClaim, UserId.ToString()), new Claim(DeleteFilter.ClientClaim, app)], "test")),
        };
        var descriptor = new ControllerActionDescriptor { ControllerName = "Library", ActionName = action };
        var actionContext = new ActionContext(http, new RouteData(), descriptor);
        return new ActionExecutingContext(actionContext, [], new Dictionary<string, object?> { [argument] = ids }, controller: new object());
    }

    private static async Task<bool> Run(DeleteFilter filter, ActionExecutingContext context)
    {
        var ran = false;
        await filter.OnActionExecutionAsync(context, () =>
        {
            ran = true;
            return Task.FromResult(new ActionExecutedContext(context, [], new object()));
        });
        return ran;
    }

    private static readonly DeleteAnswer LeavingSoon = new(true, null, "leaving_soon", null);
    private static readonly DeleteAnswer NotMine = new(false, null, null, null);

    [Fact]
    public async Task A_title_Curatarr_takes_is_not_deleted()
    {
        var context = Context("DeleteItem", "itemId", Item(LeavingSoon));
        Assert.False(await Run(Filter(), context));
        Assert.IsType<NoContentResult>(context.Result);
    }

    [Fact]
    public async Task Anything_else_is_deleted_by_Jellyfin()
    {
        var context = Context("DeleteItem", "itemId", Item(NotMine));
        Assert.True(await Run(Filter(), context));
        Assert.Null(context.Result);
    }

    [Fact]
    public async Task A_title_Curatarr_keeps_fails_the_delete()
    {
        var context = Context("DeleteItem", "itemId", Item(new DeleteAnswer(true, "Never Purge is on for it.", null, null)));
        Assert.False(await Run(Filter(), context));
        var result = Assert.IsType<ObjectResult>(context.Result);
        Assert.Equal(409, result.StatusCode);
    }

    [Fact]
    public async Task Without_Curatarr_deletes_fail_unless_told_otherwise()
    {
        var id = Item(answer: null);
        var context = Context("DeleteItem", "itemId", id);
        Assert.False(await Run(Filter(), context));
        Assert.Equal(409, Assert.IsType<ObjectResult>(context.Result).StatusCode);

        _config.BlockWhenUnreachable = false;
        Assert.True(await Run(Filter(), Context("DeleteItem", "itemId", id)));
    }

    [Fact]
    public async Task Of_several_only_the_others_reach_Jellyfin()
    {
        var mine = Item(LeavingSoon);
        var theirs = Item(NotMine);
        var context = Context("DeleteItems", "ids", new[] { mine, theirs });
        Assert.True(await Run(Filter(), context));
        Assert.Equal(new[] { theirs }, context.ActionArguments["ids"]);
    }

    [Fact]
    public async Task Someone_who_may_not_delete_gets_Jellyfins_own_answer()
    {
        var id = Item(LeavingSoon, mayDelete: false);
        Assert.True(await Run(Filter(), Context("DeleteItem", "itemId", id)));
        Assert.Empty(_asked);
    }

    [Fact]
    public async Task Other_requests_and_a_switched_off_plugin_pass_straight_through()
    {
        var id = Item(LeavingSoon);
        Assert.True(await Run(Filter(), Context("GetItem", "itemId", id)));
        _config.InterceptDeletes = false;
        Assert.True(await Run(Filter(), Context("DeleteItem", "itemId", id)));
        Assert.Empty(_asked);
    }

    [Fact]
    public async Task The_web_app_deletes_now_once_it_has_Leaving_Soon_in_its_menu()
    {
        var id = Item(new DeleteAnswer(true, null, "deleting", null));
        Assert.False(await Run(Filter(), Context("DeleteItem", "itemId", id, app: "Jellyfin Web")));
        WebMenu.Active = true;
        try
        {
            Assert.False(await Run(Filter(), Context("DeleteItem", "itemId", id, app: "Jellyfin Web")));
            Assert.False(await Run(Filter(), Context("DeleteItem", "itemId", id)));
        }
        finally
        {
            WebMenu.Active = false;
        }

        Assert.Equal(new[] { Modes.Delete, Modes.Now, Modes.Delete }, _modes);
    }

    private sealed class FakeMovie : Movie
    {
        public bool Allowed { get; init; }

        public override bool CanDelete(User user) => Allowed;
    }
}
