using System.Reflection;
using System.Runtime.Loader;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Curatarr;

/// <summary>
/// Adds a Leaving Soon item beside Delete in the web app's menus. Jellyfin's apps
/// build their menus themselves, so a plugin can only change the web app, by
/// adding a script to its page through the File Transformation plugin. Without
/// that plugin the web app's Delete works as in every other app.
/// </summary>
public sealed class WebMenu : IHostedService
{
    /// <summary>The name the web app gives itself when it signs in.</summary>
    internal const string Client = "Jellyfin Web";

    private const string TransformationId = "c3a7e9b2-5d41-4f86-9a0c-2e6b8d1f4a73";
    private const string Marker = "<!-- curatarr-menu -->";
    private static readonly Lazy<string> Script = new(ReadScript);
    private readonly ILogger<WebMenu> _logger;

    /// <summary>
    /// Initializes a new instance of the <see cref="WebMenu"/> class.
    /// </summary>
    /// <param name="logger">The log.</param>
    public WebMenu(ILogger<WebMenu> logger)
    {
        _logger = logger;
    }

    /// <summary>
    /// Gets or sets a value indicating whether the web app has the Leaving Soon item,
    /// so its Delete deletes straight away.
    /// </summary>
    internal static bool Active { get; set; }

    /// <summary>
    /// Called by the File Transformation plugin with the web app's index.html.
    /// </summary>
    /// <param name="payload">The page.</param>
    /// <returns>The page with the script added.</returns>
    public static string IndexHtml(PagePayload payload)
    {
        ArgumentNullException.ThrowIfNull(payload);
        return Inject(payload.Contents ?? string.Empty);
    }

    /// <summary>
    /// Adds the menu script before the page's closing body tag, once.
    /// </summary>
    /// <param name="page">The web app's index.html.</param>
    /// <returns>The page with the script.</returns>
    internal static string Inject(string page)
    {
        var end = page.LastIndexOf("</body>", StringComparison.OrdinalIgnoreCase);
        if (end < 0 || page.Contains(Marker, StringComparison.Ordinal))
        {
            return page;
        }

        return page[..end] + Marker + "<script>" + Script.Value + "</script>" + page[end..];
    }

    /// <inheritdoc />
    public Task StartAsync(CancellationToken cancellationToken)
    {
        if (Plugin.Instance?.Configuration.WebMenu == false)
        {
            return Task.CompletedTask;
        }

        try
        {
            var register = AssemblyLoadContext.All
                .SelectMany(context => context.Assemblies)
                .FirstOrDefault(assembly => assembly.FullName?.Contains(".FileTransformation", StringComparison.Ordinal) == true)
                ?.GetType("Jellyfin.Plugin.FileTransformation.PluginInterface")
                ?.GetMethod("RegisterTransformation");
            if (register is null)
            {
                _logger.LogInformation("The File Transformation plugin isn't installed, so the web app has no Leaving Soon item; Delete there works as in the other apps");
                return Task.CompletedTask;
            }

            var json = JsonSerializer.Serialize(new Dictionary<string, string?>
            {
                ["id"] = TransformationId,
                ["fileNamePattern"] = "index.html",
                ["callbackAssembly"] = typeof(WebMenu).Assembly.FullName,
                ["callbackClass"] = typeof(WebMenu).FullName,
                ["callbackMethod"] = nameof(IndexHtml),
            });

            // It takes a Newtonsoft JObject: made by its own Parse, so this plugin
            // needn't reference Newtonsoft.
            var type = register.GetParameters()[0].ParameterType;
            var payload = type == typeof(string)
                ? json
                : type.GetMethod("Parse", BindingFlags.Public | BindingFlags.Static, [typeof(string)])?.Invoke(null, [json]);
            register.Invoke(null, [payload]);
            Active = true;
            _logger.LogInformation("Added Leaving Soon to the web app's menus");
        }
        catch (Exception ex) when (ex is TargetInvocationException or ArgumentException or InvalidOperationException or NotSupportedException or MemberAccessException)
        {
            _logger.LogWarning(ex, "Couldn't add Leaving Soon to the web app's menus");
        }

        return Task.CompletedTask;
    }

    /// <inheritdoc />
    public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;

    private static string ReadScript()
    {
        using var stream = typeof(WebMenu).Assembly.GetManifestResourceStream($"{typeof(WebMenu).Namespace}.Web.menu.js")
            ?? throw new InvalidOperationException("menu.js is missing from the plugin");
        using var reader = new StreamReader(stream);
        return reader.ReadToEnd();
    }
}

/// <summary>
/// What the File Transformation plugin passes: the file's contents.
/// </summary>
public sealed class PagePayload
{
    /// <summary>
    /// Gets or sets the file's contents.
    /// </summary>
    [JsonPropertyName("contents")]
    public string? Contents { get; set; }
}
