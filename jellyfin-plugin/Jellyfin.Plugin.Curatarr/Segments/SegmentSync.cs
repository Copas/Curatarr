using System.Net.Http.Json;
using System.Text.Json;
using Jellyfin.Plugin.Curatarr.Configuration;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.MediaSegments;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Curatarr.Segments;

/// <summary>
/// Every minute, while serving Curatarr's skip segments is on, fetches what
/// changed in Curatarr since last time and has Jellyfin take each changed
/// item's segments again (its segment providers are run for that item), so a
/// corrected intro reaches the apps straight away instead of at the next scan.
/// </summary>
public sealed class SegmentSync : BackgroundService
{
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);
    private static readonly TimeSpan Every = TimeSpan.FromMinutes(1);
    private readonly IHttpClientFactory _httpClientFactory;
    private readonly SegmentStore _store;
    private readonly ILibraryManager _libraryManager;
    private readonly IMediaSegmentManager _segmentManager;
    private readonly ILogger<SegmentSync> _logger;

    /// <summary>
    /// Initializes a new instance of the <see cref="SegmentSync"/> class.
    /// </summary>
    /// <param name="httpClientFactory">Jellyfin's HTTP clients.</param>
    /// <param name="store">Where the segments are kept.</param>
    /// <param name="libraryManager">Jellyfin's library.</param>
    /// <param name="segmentManager">Jellyfin's segment store.</param>
    /// <param name="logger">The log.</param>
    public SegmentSync(
        IHttpClientFactory httpClientFactory,
        SegmentStore store,
        ILibraryManager libraryManager,
        IMediaSegmentManager segmentManager,
        ILogger<SegmentSync> logger)
    {
        _httpClientFactory = httpClientFactory;
        _store = store;
        _libraryManager = libraryManager;
        _segmentManager = segmentManager;
        _logger = logger;
    }

    /// <summary>
    /// Gets the file the segments are kept in.
    /// </summary>
    internal static string File => Path.Combine(Plugin.Instance?.DataFolderPath ?? Path.GetTempPath(), "segments.json");

    /// <inheritdoc />
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        using var timer = new PeriodicTimer(Every);
        do
        {
            var config = Plugin.Instance?.Configuration;
            if (config is { SkipSegments: true } && !string.IsNullOrEmpty(config.CuratarrUrl) && !string.IsNullOrEmpty(config.Token))
            {
                await SyncAsync(config, stoppingToken).ConfigureAwait(false);
            }
        }
        while (await timer.WaitForNextTickAsync(stoppingToken).ConfigureAwait(false));
    }

    private async Task SyncAsync(PluginConfiguration config, CancellationToken cancellationToken)
    {
        try
        {
            var client = _httpClientFactory.CreateClient();
            client.Timeout = TimeSpan.FromSeconds(30);
            var more = true;
            while (more && !cancellationToken.IsCancellationRequested)
            {
                using var request = new HttpRequestMessage(
                    HttpMethod.Get,
                    new Uri(config.CuratarrUrl.TrimEnd('/') + "/api/v1/jellyfin/segments?after=" + _store.Version));
                request.Headers.Add("X-Curatarr-Token", config.Token);
                using var response = await client.SendAsync(request, cancellationToken).ConfigureAwait(false);
                response.EnsureSuccessStatusCode();
                var page = await response.Content.ReadFromJsonAsync<Changes>(Json, cancellationToken).ConfigureAwait(false);
                if (page?.Items is null || page.Items.Count == 0)
                {
                    break;
                }

                _store.Apply(page.Items, page.Version);
                foreach (var changed in page.Items)
                {
                    await RefreshAsync(changed.Item, cancellationToken).ConfigureAwait(false);
                }

                more = page.More;
            }
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or JsonException or IOException)
        {
            _logger.LogWarning("Couldn't fetch skip segments from Curatarr: {Message}", ex.Message);
        }
    }

    private async Task RefreshAsync(string itemId, CancellationToken cancellationToken)
    {
        if (!Guid.TryParse(itemId, out var id))
        {
            return;
        }

        var item = _libraryManager.GetItemById(id);
        if (item is null)
        {
            return; // gone from Jellyfin, or not scanned in yet: its next scan asks
        }

        try
        {
            await _segmentManager.RunSegmentPluginProviders(item, _libraryManager.GetLibraryOptions(item), true, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            _logger.LogWarning("Couldn't refresh the skip segments of {Item}: {Message}", itemId, ex.Message);
        }
    }

    private sealed record Changes(long Version, List<ChangedItem>? Items, bool More);
}
