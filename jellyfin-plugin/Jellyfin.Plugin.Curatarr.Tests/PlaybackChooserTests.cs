using Jellyfin.Data.Enums;
using Jellyfin.Plugin.Curatarr.Playback;
using MediaBrowser.Model.Dlna;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.MediaInfo;
using MediaBrowser.Model.Session;
using Microsoft.Extensions.Logging.Abstractions;
using Xunit;

namespace Jellyfin.Plugin.Curatarr.Tests;

public class PlaybackChooserTests
{
    private static readonly Guid Item = Guid.NewGuid();

    // Like a browser: H.264 with AAC plays as it is; anything else is converted.
    private static readonly DeviceProfile Browser = new()
    {
        DirectPlayProfiles =
        [
            new DirectPlayProfile { Type = DlnaProfileType.Video, Container = "mkv", VideoCodec = "h264", AudioCodec = "aac" },
        ],
        TranscodingProfiles =
        [
            new TranscodingProfile { Type = DlnaProfileType.Video, Container = "ts", VideoCodec = "h264", AudioCodec = "aac", Protocol = MediaStreamProtocol.hls },
        ],
    };

    private static MediaSourceInfo Source(string id, string video, params (int Index, string Codec, string Language, int Channels, string? Title)[] audio)
    {
        var streams = new List<MediaStream>
        {
            new() { Index = 0, Type = MediaStreamType.Video, Codec = video, Width = 1920, Height = 1080, BitRate = 5_000_000 },
        };
        streams.AddRange(audio.Select(a => new MediaStream
        {
            Index = a.Index,
            Type = MediaStreamType.Audio,
            Codec = a.Codec,
            Language = a.Language,
            Channels = a.Channels,
            Title = a.Title,
            BitRate = 192_000,
        }));
        return new MediaSourceInfo
        {
            Id = id,
            Path = $"/media/{id}.mkv",
            Protocol = MediaProtocol.File,
            Container = "mkv",
            Bitrate = 5_500_000,
            MediaStreams = streams,
            DefaultAudioStreamIndex = audio[0].Index,
            SupportsDirectPlay = true,
            SupportsDirectStream = true,
            SupportsTranscoding = true,
        };
    }

    private static PlaybackChoice? Choose(params MediaSourceInfo[] sources)
        => new PlaybackChooser(NullLogger.Instance).Choose(Item, sources, Browser, null, 20_000_000, null, "device");

    [Fact]
    public void A_browser_plays_the_AAC_track_instead_of_converting_AC3()
    {
        var choice = Choose(Source("a", "h264", (1, "ac3", "eng", 6, null), (2, "aac", "eng", 2, "AAC stereo")));
        Assert.NotNull(choice);
        Assert.Equal(2, choice.AudioStreamIndex);
        Assert.Equal(PlayMethod.DirectPlay, choice.PlayMethod);
    }

    [Fact]
    public void A_track_in_another_language_or_a_commentary_is_never_chosen()
    {
        Assert.Null(Choose(Source("a", "h264", (1, "ac3", "eng", 6, null), (2, "aac", "fre", 2, null), (3, "aac", "eng", 2, "Director's commentary"))));
    }

    [Fact]
    public void What_already_plays_as_it_is_is_left_alone()
    {
        Assert.Null(Choose(Source("a", "h264", (1, "aac", "eng", 2, null), (2, "ac3", "eng", 6, null))));
    }

    [Fact]
    public void Of_two_copies_the_one_the_device_plays_directly_comes_first()
    {
        var choice = Choose(Source("hevc", "hevc", (1, "aac", "eng", 2, null)), Source("avc", "h264", (1, "aac", "eng", 2, null)));
        Assert.NotNull(choice);
        Assert.Equal("avc", choice.MediaSourceId);
    }

    [Fact]
    public void The_chosen_copy_and_track_are_what_the_app_is_told()
    {
        var response = new PlaybackInfoResponse { MediaSources = [Source("hevc", "hevc", (1, "aac", "eng", 2, null)), Source("avc", "h264", (1, "ac3", "eng", 6, null), (2, "aac", "eng", 2, null))] };
        PlaybackFilter.Apply(response, new PlaybackChoice("avc", 2, PlayMethod.DirectPlay));
        Assert.Equal("avc", response.MediaSources[0].Id);
        Assert.Equal(2, response.MediaSources[0].DefaultAudioStreamIndex);
    }
}
