using System.Net.Http.Json;
using System.Text.Json;
using Jellyfin.Plugin.Curatarr.Configuration;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Curatarr.Deletes;

/// <summary>
/// Curatarr's answer to a delete.
/// </summary>
/// <param name="Handled">False leaves the delete to Jellyfin.</param>
/// <param name="Refused">Why Curatarr keeps the title (the delete fails).</param>
/// <param name="Result">"leaving_soon" or "already".</param>
/// <param name="Message">A sentence for the log.</param>
public sealed record DeleteAnswer(bool Handled, string? Refused, string? Result, string? Message);

/// <summary>
/// Asks Curatarr what a delete should do.
/// </summary>
public interface ICuratarrClient
{
    /// <summary>
    /// Asks about one item; null when Curatarr can't be reached or answers oddly.
    /// </summary>
    /// <param name="config">The plugin's settings.</param>
    /// <param name="itemId">The item being deleted.</param>
    /// <param name="userName">Who deleted it.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>Curatarr's answer, or null.</returns>
    Task<DeleteAnswer?> AskAsync(PluginConfiguration config, Guid itemId, string userName, CancellationToken cancellationToken);

    /// <summary>
    /// Whether Curatarr answers at this address with this token.
    /// </summary>
    /// <param name="url">Curatarr's address.</param>
    /// <param name="token">The webhook token.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>Null when it works, else what went wrong.</returns>
    Task<string?> CheckAsync(string url, string token, CancellationToken cancellationToken);
}

/// <summary>
/// Calls Curatarr's /api/v1/jellyfin/delete with the webhook token.
/// </summary>
public sealed class CuratarrClient : ICuratarrClient
{
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);
    private static readonly TimeSpan Timeout = TimeSpan.FromSeconds(10);
    private readonly IHttpClientFactory _httpClientFactory;
    private readonly ILogger<CuratarrClient> _logger;

    /// <summary>
    /// Initializes a new instance of the <see cref="CuratarrClient"/> class.
    /// </summary>
    /// <param name="httpClientFactory">Jellyfin's HTTP clients.</param>
    /// <param name="logger">The log.</param>
    public CuratarrClient(IHttpClientFactory httpClientFactory, ILogger<CuratarrClient> logger)
    {
        _httpClientFactory = httpClientFactory;
        _logger = logger;
    }

    /// <inheritdoc />
    public async Task<DeleteAnswer?> AskAsync(PluginConfiguration config, Guid itemId, string userName, CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(config);
        try
        {
            using var response = await PostAsync(
                config.CuratarrUrl,
                config.Token,
                new { itemId = itemId.ToString("N"), userName },
                cancellationToken).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
            {
                _logger.LogWarning("Curatarr answered {Status} about deleting {Item}", (int)response.StatusCode, itemId);
                return null;
            }

            return await response.Content.ReadFromJsonAsync<DeleteAnswer>(Json, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or JsonException or UriFormatException or InvalidOperationException)
        {
            _logger.LogWarning("Curatarr couldn't be asked about deleting {Item}: {Error}", itemId, ex.GetType().Name);
            return null;
        }
    }

    /// <inheritdoc />
    public async Task<string?> CheckAsync(string url, string token, CancellationToken cancellationToken)
    {
        try
        {
            using var response = await PostAsync(url, token, new { check = true }, cancellationToken).ConfigureAwait(false);
            return (int)response.StatusCode switch
            {
                200 => null,
                403 => "Curatarr refused the token. Copy the webhook token from Curatarr's Settings → Integrations.",
                404 => "That address answers, but not as a Curatarr new enough for this plugin.",
                var status => $"Curatarr answered {status}.",
            };
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or UriFormatException or InvalidOperationException)
        {
            return $"Curatarr couldn't be reached at that address ({ex.GetType().Name}).";
        }
    }

    private async Task<HttpResponseMessage> PostAsync(string url, string token, object body, CancellationToken cancellationToken)
    {
        var client = _httpClientFactory.CreateClient();
        client.Timeout = Timeout;
        using var request = new HttpRequestMessage(HttpMethod.Post, new Uri(url.TrimEnd('/') + "/api/v1/jellyfin/delete"))
        {
            Content = JsonContent.Create(body, options: Json),
        };
        request.Headers.Add("X-Curatarr-Token", token);
        return await client.SendAsync(request, cancellationToken).ConfigureAwait(false);
    }
}
