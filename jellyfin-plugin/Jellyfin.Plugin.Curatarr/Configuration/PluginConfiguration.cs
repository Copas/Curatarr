using MediaBrowser.Model.Plugins;

namespace Jellyfin.Plugin.Curatarr.Configuration;

/// <summary>
/// Where Curatarr is, and what a delete does.
/// </summary>
public class PluginConfiguration : BasePluginConfiguration
{
    /// <summary>
    /// Gets or sets Curatarr's address as Jellyfin's server reaches it, e.g. http://127.0.0.1:8787.
    /// </summary>
    public string CuratarrUrl { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets the webhook token from Curatarr's Settings → Integrations.
    /// </summary>
    public string Token { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets a value indicating whether deletes ask Curatarr first.
    /// </summary>
    public bool InterceptDeletes { get; set; } = true;

    /// <summary>
    /// Gets or sets a value indicating whether a delete fails while Curatarr can't be
    /// reached (rather than deleting the file as Jellyfin would without the plugin).
    /// </summary>
    public bool BlockWhenUnreachable { get; set; } = true;

    /// <summary>
    /// Gets or sets a value indicating whether the web app's menus get a Leaving Soon
    /// item (Delete there then deletes straight away). Needs the File Transformation
    /// plugin, and a restart of Jellyfin to take effect.
    /// </summary>
    public bool WebMenu { get; set; } = true;

    /// <summary>
    /// Gets or sets a value indicating whether each device is pointed at the copy and
    /// audio track it can play with the least work (an AAC track beside AC3 for a
    /// browser, the copy it can play directly), when the viewer didn't pick one.
    /// </summary>
    public bool ChoosePlayable { get; set; } = true;

    /// <summary>
    /// Gets or sets a value indicating whether an account sees the posters Curatarr
    /// has for it alone (custom posters for one account) instead of the usual ones.
    /// </summary>
    public bool CustomPosters { get; set; } = true;

    /// <summary>
    /// The default for <see cref="LargePosterWidth"/>.
    /// </summary>
    public const int DefaultLargePosterWidth = 300;

    /// <summary>
    /// Gets or sets the width, in pixels, from which an app is showing a poster big
    /// (a title's own page), so it gets a favourite ribbon's version with names.
    /// </summary>
    public int LargePosterWidth { get; set; } = DefaultLargePosterWidth;
}
