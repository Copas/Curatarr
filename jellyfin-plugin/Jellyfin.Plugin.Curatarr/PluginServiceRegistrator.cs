using Jellyfin.Plugin.Curatarr.Deletes;
using Jellyfin.Plugin.Curatarr.Playback;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Plugins;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.DependencyInjection;

namespace Jellyfin.Plugin.Curatarr;

/// <summary>
/// Adds the delete filter to Jellyfin's API, and Leaving Soon to the web app's menus.
/// </summary>
public class PluginServiceRegistrator : IPluginServiceRegistrator
{
    /// <inheritdoc />
    public void RegisterServices(IServiceCollection serviceCollection, IServerApplicationHost applicationHost)
    {
        serviceCollection.AddSingleton<ICuratarrClient, CuratarrClient>();
        serviceCollection.AddScoped<DeleteFilter>();
        serviceCollection.Configure<MvcOptions>(options => options.Filters.AddService<DeleteFilter>());
        serviceCollection.AddScoped<PlaybackFilter>();
        serviceCollection.Configure<MvcOptions>(options => options.Filters.AddService<PlaybackFilter>());
        serviceCollection.AddHostedService<WebMenu>();
    }
}
