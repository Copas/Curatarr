using Jellyfin.Plugin.Curatarr.Deletes;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;

namespace Jellyfin.Plugin.Curatarr.Api;

/// <summary>
/// The settings page's Test button.
/// </summary>
[ApiController]
[Route("Curatarr")]
[Authorize(Policy = "RequiresElevation")]
public class CuratarrController : ControllerBase
{
    private readonly ICuratarrClient _curatarr;

    /// <summary>
    /// Initializes a new instance of the <see cref="CuratarrController"/> class.
    /// </summary>
    /// <param name="curatarr">Asks Curatarr.</param>
    public CuratarrController(ICuratarrClient curatarr)
    {
        _curatarr = curatarr;
    }

    /// <summary>
    /// Checks an address and token before they're saved.
    /// </summary>
    /// <param name="request">The address and token.</param>
    /// <param name="cancellationToken">Cancels the check.</param>
    /// <returns>Whether it works, and if not why.</returns>
    [HttpPost("Test")]
    public async Task<ActionResult<TestResult>> Test([FromBody] TestRequest request, CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);
        if (string.IsNullOrWhiteSpace(request.Url))
        {
            return new TestResult(false, "Enter Curatarr's address.");
        }

        var problem = await _curatarr.CheckAsync(request.Url.Trim(), request.Token ?? string.Empty, cancellationToken).ConfigureAwait(false);
        return new TestResult(problem is null, problem ?? "Curatarr answered: deletes will go through it.");
    }
}

/// <summary>
/// What to test.
/// </summary>
/// <param name="Url">Curatarr's address.</param>
/// <param name="Token">The webhook token.</param>
public sealed record TestRequest(string? Url, string? Token);

/// <summary>
/// How the test went.
/// </summary>
/// <param name="Ok">Whether Curatarr answered.</param>
/// <param name="Message">What to show.</param>
public sealed record TestResult(bool Ok, string Message);
