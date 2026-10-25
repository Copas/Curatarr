using System.Security.Claims;
using Jellyfin.Plugin.Curatarr.Configuration;
using Jellyfin.Plugin.Curatarr.Deletes;
using Jellyfin.Plugin.Curatarr.Posters;
using MediaBrowser.Controller.Drawing;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.Querying;
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

public class PosterFilterTests
{
    private static readonly Guid Sam = Guid.NewGuid();
    private static readonly Guid Alex = Guid.NewGuid();
    private static readonly Guid Film = Guid.NewGuid();
    private static readonly Guid Show = Guid.NewGuid();
    private readonly PosterStore _store = new();

    public PosterFilterTests()
    {
        _store.Replace(
        [
            new AccountPoster(Film.ToString("N"), Sam.ToString("N"), "p1", "aaaa1111"),
            new AccountPoster(Show.ToString("N"), Sam.ToString("N"), "p2", "bbbb2222"),
        ]);
    }

    private PosterFilter Filter() => new(
        _store,
        new Mock<ILibraryManager>().Object,
        new Mock<IImageProcessor>().Object,
        NullLogger<PosterFilter>.Instance,
        () => new PluginConfiguration());

    private static async Task<object> SendAsync(PosterFilter filter, Guid user, object value)
    {
        var http = new DefaultHttpContext
        {
            User = new ClaimsPrincipal(new ClaimsIdentity([new Claim(DeleteFilter.UserIdClaim, user.ToString())])),
        };
        var descriptor = new ControllerActionDescriptor { ControllerName = "Items", ActionName = "GetItems" };
        var actionContext = new ActionContext(http, new RouteData(), descriptor);
        var executing = new ActionExecutingContext(actionContext, [], new Dictionary<string, object?>(), controller: new object());
        var executed = new ActionExecutedContext(actionContext, [], controller: new object()) { Result = new ObjectResult(value) };
        await filter.OnActionExecutionAsync(executing, () => Task.FromResult(executed));
        return value;
    }

    private static BaseItemDto Item(Guid id) => new()
    {
        Id = id,
        ImageTags = new Dictionary<ImageType, string> { [ImageType.Primary] = "original" },
    };

    [Fact]
    public async Task An_account_with_its_own_poster_gets_its_tag()
    {
        var page = new QueryResult<BaseItemDto>([Item(Film), Item(Guid.NewGuid())]);
        await SendAsync(Filter(), Sam, page);
        Assert.Equal("aaaa1111", page.Items[0].ImageTags![ImageType.Primary]);
        Assert.Equal("original", page.Items[1].ImageTags![ImageType.Primary]);
    }

    [Fact]
    public async Task Every_other_account_gets_the_usual_poster()
    {
        var film = Item(Film);
        await SendAsync(Filter(), Alex, film);
        Assert.Equal("original", film.ImageTags![ImageType.Primary]);
    }

    [Fact]
    public async Task An_episode_card_shows_the_accounts_poster_of_its_show()
    {
        var episode = new BaseItemDto { Id = Guid.NewGuid(), SeriesId = Show, SeriesPrimaryImageTag = "original" };
        await SendAsync(Filter(), Sam, new List<BaseItemDto> { episode });
        Assert.Equal("bbbb2222", episode.SeriesPrimaryImageTag);
    }

    [Fact]
    public void A_tag_finds_its_poster_and_nothing_else_does()
    {
        Assert.True(_store.TryGetByTag("aaaa1111", out var poster));
        Assert.Equal("p1", poster.Id);
        Assert.False(_store.TryGetByTag("original", out _));
        Assert.False(_store.TryGetByTag(null, out _));
    }

    [Theory]
    [InlineData(295, 440, false)] // the Roku app's library rows
    [InlineData(464, 261, false)] // its home rows
    [InlineData(300, 450, true)] // a title's own page
    [InlineData(null, 900, true)]
    public void A_poster_is_big_on_a_titles_page(int? maxWidth, int? maxHeight, bool large)
    {
        var args = new Dictionary<string, object?> { ["maxWidth"] = maxWidth, ["maxHeight"] = maxHeight };
        Assert.Equal(large, PosterFilter.IsLarge(args, PluginConfiguration.DefaultLargePosterWidth));
    }

    [Fact]
    public async Task A_big_poster_is_its_version_with_names()
    {
        var tag = "cccc" + Guid.NewGuid().ToString("N")[..12];
        _store.Replace([new AccountPoster(Film.ToString("N"), Sam.ToString("N"), "v.x", tag, Large: true)]);
        Directory.CreateDirectory(PosterSync.Folder);
        await File.WriteAllTextAsync(PosterSync.FileFor(tag), "small");
        await File.WriteAllTextAsync(PosterSync.LargeFileFor(tag), "large");
        try
        {
            var library = new Mock<ILibraryManager>();
            library.Setup(l => l.GetItemById(Film)).Returns(new MediaBrowser.Controller.Entities.Movies.Movie());
            var images = new Mock<IImageProcessor>();
            images.Setup(i => i.ProcessImage(It.IsAny<MediaBrowser.Controller.Drawing.ImageProcessingOptions>()))
                .ReturnsAsync((MediaBrowser.Controller.Drawing.ImageProcessingOptions o) => (o.Image.Path, "image/jpeg", DateTime.UtcNow));
            var filter = new PosterFilter(_store, library.Object, images.Object, NullLogger<PosterFilter>.Instance, () => new PluginConfiguration());

            async Task<string?> Served(int maxWidth, int maxHeight)
            {
                var descriptor = new ControllerActionDescriptor { ControllerName = "Image", ActionName = "GetItemImage" };
                var context = new ActionExecutingContext(
                    new ActionContext(new DefaultHttpContext(), new RouteData(), descriptor),
                    [],
                    new Dictionary<string, object?> { ["itemId"] = Film, ["tag"] = tag, ["maxWidth"] = maxWidth, ["maxHeight"] = maxHeight },
                    controller: new object());
                await filter.OnActionExecutionAsync(context, () => throw new InvalidOperationException("served by the plugin"));
                return (context.Result as PhysicalFileResult)?.FileName;
            }

            Assert.Equal(PosterSync.FileFor(tag), await Served(295, 440));
            Assert.Equal(PosterSync.LargeFileFor(tag), await Served(300, 450));
        }
        finally
        {
            File.Delete(PosterSync.FileFor(tag));
            File.Delete(PosterSync.LargeFileFor(tag));
        }
    }

    [Fact]
    public async Task Switched_off_changes_nothing()
    {
        var filter = new PosterFilter(
            _store,
            new Mock<ILibraryManager>().Object,
            new Mock<IImageProcessor>().Object,
            NullLogger<PosterFilter>.Instance,
            () => new PluginConfiguration { CustomPosters = false });
        var film = Item(Film);
        await SendAsync(filter, Sam, film);
        Assert.Equal("original", film.ImageTags![ImageType.Primary]);
    }
}
