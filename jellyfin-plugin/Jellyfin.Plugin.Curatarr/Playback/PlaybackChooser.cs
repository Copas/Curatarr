using MediaBrowser.Model.Dlna;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.Session;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Curatarr.Playback;

/// <summary>
/// What a device should play: a copy (when a title has several) and an audio track.
/// </summary>
/// <param name="MediaSourceId">The copy.</param>
/// <param name="AudioStreamIndex">The audio track in it.</param>
/// <param name="PlayMethod">How Jellyfin would then play it.</param>
public sealed record PlaybackChoice(string MediaSourceId, int? AudioStreamIndex, PlayMethod PlayMethod);

/// <summary>
/// Picks, from a title's copies and each one's audio tracks in the same language,
/// the one the device can play with the least work: played as it is, then copied
/// into another container, then converted. It asks Jellyfin's own stream builder,
/// with the device's own profile, so it judges exactly as Jellyfin would.
///
/// Jellyfin plays the default audio track, or the one in the viewer's language,
/// whatever the device can decode, so a browser converts an AC3 track while a
/// stereo AAC track beside it would play as it is. And with two copies of a film
/// it plays the first, though the other might play directly.
/// </summary>
public sealed class PlaybackChooser
{
    private readonly StreamBuilder _builder;

    /// <summary>
    /// Initializes a new instance of the <see cref="PlaybackChooser"/> class.
    /// </summary>
    /// <param name="logger">The stream builder's log.</param>
    public PlaybackChooser(ILogger logger)
    {
        _builder = new StreamBuilder(new AnyEncoder(), logger);
    }

    /// <summary>
    /// The best choice, or null when what Jellyfin would pick anyway is as good.
    /// </summary>
    /// <param name="itemId">The title.</param>
    /// <param name="sources">Its copies, with the viewer's default tracks set.</param>
    /// <param name="profile">What the device can play.</param>
    /// <param name="requestedSourceId">A copy the viewer picked, if any.</param>
    /// <param name="maxBitrate">The device's bitrate limit, if any.</param>
    /// <param name="maxAudioChannels">Its channel limit, if any.</param>
    /// <param name="deviceId">The device.</param>
    /// <returns>A better choice, or null.</returns>
    public PlaybackChoice? Choose(
        Guid itemId,
        IReadOnlyList<MediaSourceInfo> sources,
        DeviceProfile profile,
        string? requestedSourceId,
        int? maxBitrate,
        int? maxAudioChannels,
        string? deviceId)
    {
        ArgumentNullException.ThrowIfNull(sources);
        var allowed = string.IsNullOrEmpty(requestedSourceId)
            ? sources
            : sources.Where(s => string.Equals(s.Id, requestedSourceId, StringComparison.OrdinalIgnoreCase)).ToList();
        if (allowed.Count == 0)
        {
            return null;
        }

        PlaybackChoice? best = null;
        int? bestRank = null;
        PlaybackChoice? usual = null;
        int? usualRank = null;
        foreach (var source in allowed)
        {
            foreach (var audio in Candidates(source))
            {
                var info = _builder.GetOptimalVideoStream(new MediaOptions
                {
                    ItemId = itemId,
                    MediaSources = [source],
                    MediaSourceId = source.Id,
                    Profile = profile,
                    AudioStreamIndex = audio,
                    MaxBitrate = maxBitrate,
                    MaxAudioChannels = maxAudioChannels,
                    DeviceId = deviceId,
                });
                if (info is null)
                {
                    continue;
                }

                var rank = Rank(info);
                var choice = new PlaybackChoice(source.Id, audio, info.PlayMethod);
                if (usual is null && ReferenceEquals(source, allowed[0]) && audio == source.DefaultAudioStreamIndex)
                {
                    usual = choice;
                    usualRank = rank;
                }

                if (bestRank is null || rank < bestRank)
                {
                    best = choice;
                    bestRank = rank;
                }
            }
        }

        // Only a strict improvement on what Jellyfin plays anyway.
        return best is not null && usualRank is not null && bestRank < usualRank ? best : null;
    }

    /// <summary>
    /// The default audio track first, then the others in its language (never a
    /// commentary), most channels first.
    /// </summary>
    private static IEnumerable<int?> Candidates(MediaSourceInfo source)
    {
        var audio = (source.MediaStreams ?? []).Where(s => s.Type == MediaStreamType.Audio).ToList();
        var main = audio.FirstOrDefault(s => s.Index == source.DefaultAudioStreamIndex);
        if (main is null)
        {
            yield return source.DefaultAudioStreamIndex;
            yield break;
        }

        yield return main.Index;
        foreach (var other in audio
            .Where(s => s.Index != main.Index
                && string.Equals(s.Language ?? string.Empty, main.Language ?? string.Empty, StringComparison.OrdinalIgnoreCase)
                && !(s.Title ?? string.Empty).Contains("comment", StringComparison.OrdinalIgnoreCase))
            .OrderByDescending(s => s.Channels ?? 0))
        {
            yield return other.Index;
        }
    }

    /// <summary>
    /// Lower is better: played as it is, copied, converted; then fewer reasons.
    /// </summary>
    private static int Rank(StreamInfo info)
    {
        var method = info.PlayMethod switch
        {
            PlayMethod.DirectPlay => 0,
            PlayMethod.DirectStream => 1,
            _ => 2,
        };
        var reasons = System.Numerics.BitOperations.PopCount((ulong)info.TranscodeReasons);
        return (method * 100) + reasons;
    }

    /// <summary>
    /// Jellyfin's server can encode to anything; only the device's limits matter.
    /// </summary>
    private sealed class AnyEncoder : ITranscoderSupport
    {
        public bool CanEncodeToAudioCodec(string codec) => true;

        public bool CanEncodeToSubtitleCodec(string codec) => true;

        public bool CanExtractSubtitles(string codec) => true;
    }
}
