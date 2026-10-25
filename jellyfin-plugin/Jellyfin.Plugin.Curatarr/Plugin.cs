using Jellyfin.Plugin.Curatarr.Configuration;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;

namespace Jellyfin.Plugin.Curatarr;

/// <summary>
/// Lets Curatarr decide what a delete in Jellyfin does.
/// </summary>
public class Plugin : BasePlugin<PluginConfiguration>, IHasWebPages
{
    /// <summary>
    /// Initializes a new instance of the <see cref="Plugin"/> class.
    /// </summary>
    /// <param name="applicationPaths">Jellyfin's paths.</param>
    /// <param name="xmlSerializer">Reads and writes the configuration.</param>
    public Plugin(IApplicationPaths applicationPaths, IXmlSerializer xmlSerializer)
        : base(applicationPaths, xmlSerializer)
    {
        Instance = this;
    }

    /// <summary>
    /// Gets the loaded plugin, once Jellyfin has created it.
    /// </summary>
    public static Plugin? Instance { get; private set; }

    /// <inheritdoc />
    public override string Name => "Curatarr";

    /// <inheritdoc />
    public override Guid Id => Guid.Parse("42d6dc0d-a758-4b43-ab9c-303c6727aada");

    /// <inheritdoc />
    public override string Description =>
        "Delete in any Jellyfin app marks a film or show Leaving Soon in Curatarr instead; each device plays what it can; each account can have posters of its own.";

    /// <inheritdoc />
    public IEnumerable<PluginPageInfo> GetPages()
    {
        return
        [
            new PluginPageInfo
            {
                Name = Name,
                EmbeddedResourcePath = $"{GetType().Namespace}.Configuration.configPage.html",
            },
        ];
    }
}
