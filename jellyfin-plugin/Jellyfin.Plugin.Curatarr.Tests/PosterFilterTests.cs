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
    public async Task Wide_tiles_get_the_landscape_version_when_there_is_one()
    {
        _store.Replace(
        [
            new AccountPoster(Film.ToString("N"), Sam.ToString("N"), "v.1", "dddd4444", Wide: true),
            new AccountPoster(Show.ToString("N"), Sam.ToString("N"), "p2", "bbbb2222"),
        ]);
        var film = Item(Film);
        var show = Item(Show);
        var episode = new BaseItemDto { Id = Guid.NewGuid(), ParentThumbItemId = Film, ParentThumbImageTag = "original" };
        await SendAsync(Filter(), Sam, new List<BaseItemDto> { film, show, episode });
        Assert.Equal("dddd4444", film.ImageTags![ImageType.Thumb]);
        Assert.False(show.ImageTags!.ContainsKey(ImageType.Thumb)); // no landscape version: its own
        Assert.Equal("dddd4444", episode.ParentThumbImageTag);
    }

    [Fact]
    public async Task A_thumb_is_served_from_the_landscape_file()
    {
        var tag = "eeee" + Guid.NewGuid().ToString("N")[..12];
        _store.Replace([new AccountPoster(Film.ToString("N"), Sam.ToString("N"), "v.x", tag, Wide: true)]);
        Directory.CreateDirectory(PosterSync.Folder);
        await File.WriteAllTextAsync(PosterSync.FileFor(tag), "poster");
        await File.WriteAllTextAsync(PosterSync.WideFileFor(tag), "wide");
        try
        {
            var library = new Mock<ILibraryManager>();
            library.Setup(l => l.GetItemById(Film)).Returns(new MediaBrowser.Controller.Entities.Movies.Movie());
            var images = new Mock<IImageProcessor>();
            images.Setup(i => i.ProcessImage(It.IsAny<MediaBrowser.Controller.Drawing.ImageProcessingOptions>()))
                .ReturnsAsync((MediaBrowser.Controller.Drawing.ImageProcessingOptions o) => (o.Image.Path, "image/jpeg", DateTime.UtcNow));
            var filter = new PosterFilter(_store, library.Object, images.Object, NullLogger<PosterFilter>.Instance, () => new PluginConfiguration());

            async Task<string?> Served(ImageType type)
            {
                var descriptor = new ControllerActionDescriptor { ControllerName = "Image", ActionName = "GetItemImage" };
                var context = new ActionExecutingContext(
                    new ActionContext(new DefaultHttpContext(), new RouteData(), descriptor),
                    [],
                    new Dictionary<string, object?> { ["itemId"] = Film, ["tag"] = tag, ["imageType"] = type, ["maxWidth"] = 464, ["maxHeight"] = 261 },
                    controller: new object());
                await filter.OnActionExecutionAsync(context, () => Task.FromResult<ActionExecutedContext>(null!));
                return (context.Result as PhysicalFileResult)?.FileName;
            }

            Assert.Equal(PosterSync.WideFileFor(tag), await Served(ImageType.Thumb));
            Assert.Equal(PosterSync.FileFor(tag), await Served(ImageType.Primary));
            Assert.Null(await Served(ImageType.Backdrop));
        }
        finally
        {
            File.Delete(PosterSync.FileFor(tag));
            File.Delete(PosterSync.WideFileFor(tag));
        }
    }

    [Fact]
    public async Task Each_type_of_picture_goes_where_apps_look_for_it()
    {
        _store.Replace(
        [
            new AccountPoster(Film.ToString("N"), Sam.ToString("N"), "p1", "poster11", Kind: "Primary"),
            new AccountPoster(Film.ToString("N"), Sam.ToString("N"), "thumb.x", "thumb111", Kind: "Thumb"),
            new AccountPoster(Film.ToString("N"), Sam.ToString("N"), "backdrop.x", "back1111", Kind: "Backdrop"),
            new AccountPoster(Film.ToString("N"), Sam.ToString("N"), "logo.x", "logo1111", Kind: "Logo"),
            new AccountPoster(Film.ToString("N"), Sam.ToString("N"), "banner.x", "bann1111", Kind: "Banner"),
            new AccountPoster(Show.ToString("N"), Sam.ToString("N"), "backdrop.s", "back2222", Kind: "Backdrop"),
            new AccountPoster(Show.ToString("N"), Sam.ToString("N"), "logo.s", "logo2222", Kind: "Logo"),
        ]);
        var film = Item(Film);
        film.BackdropImageTags = ["first", "second"];
        var episode = new BaseItemDto
        {
            Id = Guid.NewGuid(),
            SeriesId = Show,
            ParentBackdropItemId = Show,
            ParentBackdropImageTags = ["showback"],
            ParentLogoItemId = Show,
            ParentLogoImageTag = "showlogo",
        };
        await SendAsync(Filter(), Sam, new List<BaseItemDto> { film, episode });
        Assert.Equal("poster11", film.ImageTags![ImageType.Primary]);
        Assert.Equal("thumb111", film.ImageTags[ImageType.Thumb]);
        Assert.Equal("logo1111", film.ImageTags[ImageType.Logo]);
        Assert.Equal("bann1111", film.ImageTags[ImageType.Banner]);
        Assert.Equal(["back1111", "second"], film.BackdropImageTags);
        Assert.Equal(["back2222"], episode.ParentBackdropImageTags);
        Assert.Equal("logo2222", episode.ParentLogoImageTag);
        // Someone else: Jellyfin's own.
        var theirs = Item(Film);
        theirs.BackdropImageTags = ["first"];
        await SendAsync(Filter(), Alex, theirs);
        Assert.Equal(["first"], theirs.BackdropImageTags);
    }

    [Fact]
    public async Task A_picture_is_served_only_as_its_own_type()
    {
        var tag = "ffff" + Guid.NewGuid().ToString("N")[..12];
        _store.Replace([new AccountPoster(Film.ToString("N"), Sam.ToString("N"), "logo.x", tag, Kind: "Logo")]);
        Directory.CreateDirectory(PosterSync.Folder);
        await File.WriteAllTextAsync(PosterSync.FileFor(tag, ImageType.Logo), "logo");
        try
        {
            var library = new Mock<ILibraryManager>();
            library.Setup(l => l.GetItemById(Film)).Returns(new MediaBrowser.Controller.Entities.Movies.Movie());
            var images = new Mock<IImageProcessor>();
            images.Setup(i => i.ProcessImage(It.IsAny<MediaBrowser.Controller.Drawing.ImageProcessingOptions>()))
                .ReturnsAsync((MediaBrowser.Controller.Drawing.ImageProcessingOptions o) => (o.Image.Path, o.SupportedOutputFormats.Contains(MediaBrowser.Model.Drawing.ImageFormat.Png) ? "image/png" : "image/jpeg", DateTime.UtcNow));
            var filter = new PosterFilter(_store, library.Object, images.Object, NullLogger<PosterFilter>.Instance, () => new PluginConfiguration());

            async Task<PhysicalFileResult?> Served(ImageType type)
            {
                var descriptor = new ControllerActionDescriptor { ControllerName = "Image", ActionName = "GetItemImage" };
                var context = new ActionExecutingContext(
                    new ActionContext(new DefaultHttpContext(), new RouteData(), descriptor),
                    [],
                    new Dictionary<string, object?> { ["itemId"] = Film, ["tag"] = tag, ["imageType"] = type },
                    controller: new object());
                await filter.OnActionExecutionAsync(context, () => Task.FromResult<ActionExecutedContext>(null!));
                return context.Result as PhysicalFileResult;
            }

            var logo = await Served(ImageType.Logo);
            Assert.Equal(PosterSync.FileFor(tag, ImageType.Logo), logo?.FileName);
            Assert.Equal("image/png", logo?.ContentType);
            Assert.Null(await Served(ImageType.Primary));
        }
        finally
        {
            File.Delete(PosterSync.FileFor(tag, ImageType.Logo));
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
