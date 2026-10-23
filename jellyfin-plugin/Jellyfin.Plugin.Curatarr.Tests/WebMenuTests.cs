using Xunit;

namespace Jellyfin.Plugin.Curatarr.Tests;

public class WebMenuTests
{
    [Fact]
    public void The_script_goes_before_the_end_of_the_page_once()
    {
        var page = WebMenu.Inject("<html><body><div></div></body></html>");
        Assert.Contains("Leaving Soon", page, StringComparison.Ordinal);
        Assert.EndsWith("</script></body></html>", page, StringComparison.Ordinal);
        Assert.Equal(page, WebMenu.Inject(page));
    }

    [Fact]
    public void A_page_without_a_body_is_left_alone()
    {
        Assert.Equal("{}", WebMenu.IndexHtml(new PagePayload { Contents = "{}" }));
    }
}
