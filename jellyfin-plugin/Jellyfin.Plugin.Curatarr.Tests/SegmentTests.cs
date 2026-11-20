using Jellyfin.Database.Implementations.Enums;
using Jellyfin.Plugin.Curatarr.Configuration;
using Jellyfin.Plugin.Curatarr.Segments;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Model;
using Xunit;

namespace Jellyfin.Plugin.Curatarr.Tests;

public class SegmentTests
{
    private static readonly Guid Episode1 = new("00000000-0000-0000-0000-000000000001");

    private static ChangedItem Changed(params CuratarrSegment[] segments)
        => new(Episode1.ToString("N"), segments.ToList());

    private static MediaSegmentGenerationRequest Request()
        => new() { ItemId = Episode1, ExistingSegments = [] };

    [Fact]
    public void Changes_replace_an_items_segments_and_survive_a_restart()
    {
        var file = Path.Combine(Path.GetTempPath(), $"curatarr-segments-{Guid.NewGuid():N}.json");
        try
        {
            var store = new SegmentStore(file);
            store.Apply([Changed(new CuratarrSegment("Intro", 0, 30), new CuratarrSegment("Outro", 2500, 2600))], 7);
            store.Apply([Changed(new CuratarrSegment("Intro", 5, 35))], 9);
            Assert.Equal(9, store.Version);
            Assert.Equal([new CuratarrSegment("Intro", 5, 35)], store.For(Episode1));

            var again = new SegmentStore(file);
            Assert.Equal(9, again.Version);
            Assert.Equal(MediaSegmentType.Intro, Assert.Single(again.For(Episode1)).Kind);

            again.Apply([new ChangedItem(Episode1.ToString("N"), [])], 10);
            Assert.Empty(again.For(Episode1));
        }
        finally
        {
            File.Delete(file);
        }
    }

    [Fact]
    public async Task Jellyfin_gets_them_in_ticks_only_while_serving_them_is_on()
    {
        var store = new SegmentStore();
        store.Apply([Changed(new CuratarrSegment("Intro", 1.5, 30), new CuratarrSegment("Recap", 0, 0))], 3);
        var config = new PluginConfiguration { SkipSegments = true };
        var provider = new CuratarrSegmentProvider(store, () => config);
        Assert.Equal("Curatarr", provider.Name);
        Assert.True(await provider.Supports(new Episode()));
        Assert.True(await provider.Supports(new Movie()));
        Assert.False(await provider.Supports(new Series()));

        var given = await provider.GetMediaSegments(Request(), CancellationToken.None);
        var intro = Assert.Single(given); // the empty recap is left out
        Assert.Equal(MediaSegmentType.Intro, intro.Type);
        Assert.Equal(TimeSpan.FromSeconds(1.5).Ticks, intro.StartTicks);
        Assert.Equal(TimeSpan.FromSeconds(30).Ticks, intro.EndTicks);
        Assert.Equal(Episode1, intro.ItemId);

        config.SkipSegments = false;
        Assert.False(await provider.Supports(new Episode()));
        Assert.Empty(await provider.GetMediaSegments(Request(), CancellationToken.None));
    }
}
