using System.Net.Http.Json;
using System.Text.Json;
using Jellyfin.Plugin.Curatarr.Configuration;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Curatarr.Posters;

/// <summary>
/// Every minute, fetches from Curatarr which accounts have posters of their own,
/// and downloads each picture once (kept in the plugin's data folder).
/// </summary>
public sealed class PosterSync : BackgroundService
{
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);
    private static readonly TimeSpan Every = TimeSpan.FromMinutes(1);
    private readonly IHttpClientFactory _httpClientFactory;
    private readonly PosterStore _store;
    private readonly ILogger<PosterSync> _logger;

    /// <summary>
    /// Initializes a new instance of the <see cref="PosterSync"/> class.
    /// </summary>
    /// <param name="httpClientFactory">Jellyfin's HTTP clients.</param>
    /// <param name="store">Where the list is kept.</param>
    /// <param name="logger">The log.</param>
    public PosterSync(IHttpClientFactory httpClientFactory, PosterStore store, ILogger<PosterSync> logger)
    {
        _httpClientFactory = httpClientFactory;
        _store = store;
        _logger = logger;
    }

    /// <summary>
    /// Gets the folder downloaded posters are kept in.
    /// </summary>
    internal static string Folder => Path.Combine(Plugin.Instance?.DataFolderPath ?? Path.GetTempPath(), "posters");

    /// <summary>
    /// The file a poster is kept in.
    /// </summary>
    /// <param name="tag">The poster's tag.</param>
    /// <returns>Its path.</returns>
    internal static string FileFor(string tag) => Path.Combine(Folder, tag + ".jpg");

    /// <summary>
    /// The file a poster's big version is kept in.
    /// </summary>
    /// <param name="tag">The poster's tag.</param>
    /// <returns>Its path.</returns>
    internal static string LargeFileFor(string tag) => Path.Combine(Folder, tag + ".large.jpg");

    /// <inheritdoc />
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        using var timer = new PeriodicTimer(Every);
        do
        {
            var config = Plugin.Instance?.Configuration;
            if (config is { CustomPosters: true } && !string.IsNullOrEmpty(config.CuratarrUrl) && !string.IsNullOrEmpty(config.Token))
            {
                await SyncAsync(config, stoppingToken).ConfigureAwait(false);
            }
            else
            {
                _store.Replace([]);
            }
        }
        while (await timer.WaitForNextTickAsync(stoppingToken).ConfigureAwait(false));
    }

    private async Task SyncAsync(PluginConfiguration config, CancellationToken cancellationToken)
    {
        try
        {
            var client = _httpClientFactory.CreateClient();
            client.Timeout = TimeSpan.FromSeconds(20);
            var listing = await GetAsync<PosterListing>(client, config, "/api/v1/jellyfin/posters", cancellationToken).ConfigureAwait(false);
            var ready = new List<AccountPoster>();
            Directory.CreateDirectory(Folder);
            foreach (var poster in listing?.Posters ?? [])
            {
                if (!await DownloadAsync(client, config, $"/api/v1/jellyfin/posters/{poster.Id}.jpg", FileFor(poster.Tag), cancellationToken).ConfigureAwait(false))
                {
                    continue;
                }

                if (poster.Large)
                {
                    // Without it, the usual one shows big too.
                    await DownloadAsync(client, config, $"/api/v1/jellyfin/posters/{poster.Id}.jpg?size=large", LargeFileFor(poster.Tag), cancellationToken).ConfigureAwait(false);
                }

                ready.Add(poster);
            }

            _store.Replace(ready);
            Tidy(ready);
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or JsonException or IOException or UriFormatException or InvalidOperationException)
        {
            // Keep what's known: a moment when Curatarr is restarting changes nothing.
            _logger.LogDebug("Couldn't fetch account posters from Curatarr: {Error}", ex.GetType().Name);
        }
    }

    private static async Task<bool> DownloadAsync(HttpClient client, PluginConfiguration config, string path, string file, CancellationToken cancellationToken)
    {
        if (File.Exists(file))
        {
            return true;
        }

        using var request = Request(config, path);
        using var response = await client.SendAsync(request, cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
        {
            return false;
        }

        var partial = file + ".partial";
        await File.WriteAllBytesAsync(partial, await response.Content.ReadAsByteArrayAsync(cancellationToken).ConfigureAwait(false), cancellationToken).ConfigureAwait(false);
        File.Move(partial, file, true);
        return true;
    }

    private static void Tidy(List<AccountPoster> ready)
    {
        var keep = ready.SelectMany(p => new[] { FileFor(p.Tag), LargeFileFor(p.Tag) }).ToHashSet(StringComparer.Ordinal);
        foreach (var file in Directory.EnumerateFiles(Folder, "*.jpg"))
        {
            if (!keep.Contains(file))
            {
                File.Delete(file);
            }
        }
    }

    private static HttpRequestMessage Request(PluginConfiguration config, string path)
    {
        var request = new HttpRequestMessage(HttpMethod.Get, new Uri(config.CuratarrUrl.TrimEnd('/') + path));
        request.Headers.Add("X-Curatarr-Token", config.Token);
        return request;
    }

    private static async Task<T?> GetAsync<T>(HttpClient client, PluginConfiguration config, string path, CancellationToken cancellationToken)
    {
        using var request = Request(config, path);
        using var response = await client.SendAsync(request, cancellationToken).ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        return await response.Content.ReadFromJsonAsync<T>(Json, cancellationToken).ConfigureAwait(false);
    }

    private sealed record PosterListing(List<AccountPoster>? Posters);
}
