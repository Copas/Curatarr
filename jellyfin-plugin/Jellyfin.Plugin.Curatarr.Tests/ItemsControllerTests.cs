using System.Security.Claims;
using Jellyfin.Database.Implementations.Entities;
using Jellyfin.Plugin.Curatarr.Api;
using Jellyfin.Plugin.Curatarr.Configuration;
using Jellyfin.Plugin.Curatarr.Deletes;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.Library;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Moq;
using Xunit;

namespace Jellyfin.Plugin.Curatarr.Tests;

public class ItemsControllerTests
{
    private static readonly Guid UserId = Guid.NewGuid();
    private static readonly Guid EpisodeId = Guid.NewGuid();
    private readonly List<string> _modes = new();
    private DeleteAnswer? _answer;

    private ItemsController Controller(bool mayDelete = true)
    {
        var library = new Mock<ILibraryManager>();
        library.Setup(l => l.GetItemById(EpisodeId)).Returns(new FakeEpisode { Allowed = mayDelete });
        var users = new Mock<IUserManager>();
        users.Setup(u => u.GetUserById(UserId)).Returns(new User("Sam", "Default", "Default"));
        var curatarr = new Mock<ICuratarrClient>();
        curatarr
            .Setup(c => c.AskAsync(It.IsAny<PluginConfiguration>(), EpisodeId, "Sam", It.IsAny<string>(), It.IsAny<CancellationToken>()))
            .ReturnsAsync((PluginConfiguration _, Guid _, string _, string mode, CancellationToken _) =>
            {
                _modes.Add(mode);
                return _answer;
            });
        var config = new PluginConfiguration { CuratarrUrl = "http://curatarr", Token = "t" };
        return new ItemsController(library.Object, users.Object, curatarr.Object, () => config)
        {
            ControllerContext = new ControllerContext
            {
                HttpContext = new DefaultHttpContext
                {
                    User = new ClaimsPrincipal(new ClaimsIdentity([new Claim(DeleteFilter.UserIdClaim, UserId.ToString())], "test")),
                },
            },
        };
    }

    [Fact]
    public async Task An_episode_offers_leave_after_I_watch_but_not_leaving_soon()
    {
        _answer = new DeleteAnswer(false, null, null, null, CanLeaveAfterWatched: true);
        var status = (await Controller().Status(EpisodeId, CancellationToken.None)).Value!;
        Assert.False(status.CanLeave);
        Assert.True(status.CanLeaveAfterWatched);
        Assert.False(status.LeavesAfterWatched);
    }

    [Fact]
    public async Task Leave_after_I_watch_and_undoing_it_ask_curatarr()
    {
        _answer = new DeleteAnswer(true, null, "leave_after_watched", "A Show: Leaving Soon once Sam has watched it.");
        var result = (await Controller().LeaveAfterWatched(EpisodeId, false, CancellationToken.None)).Value!;
        Assert.True(result.Ok);
        Assert.Contains("once Sam has watched it", result.Message, StringComparison.Ordinal);
        _answer = new DeleteAnswer(true, "Never Purge is on for A Show.", null, null);
        result = (await Controller().LeaveAfterWatched(EpisodeId, true, CancellationToken.None)).Value!;
        Assert.False(result.Ok);
        Assert.Equal([Modes.LeaveAfterWatched, Modes.KeepAfterWatched], _modes);
    }

    [Fact]
    public async Task Only_for_people_who_may_delete_it()
    {
        var result = await Controller(mayDelete: false).LeaveAfterWatched(EpisodeId, false, CancellationToken.None);
        Assert.IsType<ForbidResult>(result.Result);
        Assert.Empty(_modes);
    }

    private sealed class FakeEpisode : Episode
    {
        public bool Allowed { get; init; }

        public override bool CanDelete(User user) => Allowed;
    }
}
