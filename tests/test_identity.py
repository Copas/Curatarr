from curatarr import identity

SEASON = {
    20: "Brake My Wife, Please. When Homer loses his driver's license, Marge has to "
    "drive everyone everywhere, which becomes so stressful that she runs him over.",
    21: "The Bart of War. Bart and Milhouse are forced into community service after "
    "destroying Flanders' Beatles memorial and join separate scout groups.",
    22: "Moe Baby Blues. Moe saves Maggie from a car crash and becomes her babysitter, "
    "which leads to trouble with the mob.",
    19: "Old Yeller-Belly. Homer is called a coward after failing to save Grampa from "
    "a fire, while the dog Santa's Little Helper becomes a hero.",
}
DRIVING = (
    "Public transportation is for jerks. I miss my car so much, everyone's driving "
    "but me. Marge, you have to drive me to work. My license! Marge, the license. "
    "Driving driving driving. Homer walks. Marge drives the car everywhere."
)


def test_a_misfiled_episode_fits_another_summary_by_a_wide_margin():
    other, margin, certain = identity.better_match(21, DRIVING, SEASON)
    assert other == 20 and margin >= identity.WIN_MARGIN and certain


def test_which_episode_is_uncertain_when_two_others_fit_alike():
    season = {
        **SEASON,
        23: "Road Rage. Homer loses his license and Marge drives.",
    }
    other, _margin, certain = identity.better_match(21, DRIVING, season)
    assert other in (20, 23) and not certain


def test_only_a_clear_own_match_confirms_a_file():
    assert identity.fits_own(20, DRIVING, SEASON)
    shifted = {
        **SEASON,
        23: "Road Rage. Homer loses his license and Marge drives.",
    }
    assert not identity.fits_own(20, DRIVING, shifted)  # a near-tie isn't confirmation


def test_a_correctly_filed_episode_is_not_flagged():
    assert identity.better_match(20, DRIVING, SEASON) is None


def test_vague_or_missing_summaries_flag_nothing():
    vague = {n: "The guys pull pranks." for n in range(1, 8)}
    assert identity.better_match(3, DRIVING, vague) is None
    assert identity.better_match(21, DRIVING, {20: SEASON[20], 21: SEASON[21]}) is None


def test_foreign_default_audio():
    streams = [
        {"codec_type": "audio", "language": "jpn", "default": True},
        {"codec_type": "audio", "language": "eng"},
    ]
    assert identity.foreign_audio("ja", streams) == ("ja", True)
    assert identity.foreign_audio("en", streams) is None
    assert identity.foreign_audio("ja", streams[:1]) == ("ja", False)


def test_set_default_audio_flags_only_the_english_track(monkeypatch):
    from curatarr import mediafile

    info = mediafile.MediaInfo(
        path="/tv/x.mkv",
        size=1,
        mtime=1,
        duration=60,
        format_name="matroska,webm",
        streams=[
            {"index": 0, "codec_type": "video"},
            {"index": 1, "codec_type": "audio", "language": "jpn", "default": True},
            {"index": 2, "codec_type": "audio", "language": "eng", "default": False},
        ],
    )
    sent = []
    monkeypatch.setattr(mediafile, "required_tool", lambda name: name)
    monkeypatch.setattr(mediafile, "_mkvpropedit", sent.append)
    assert mediafile.english_audio(info) == 2
    mediafile.set_default_audio("/tv/x.mkv", info, 2)
    assert sent == [
        [
            "mkvpropedit",
            "/tv/x.mkv",
            "--edit",
            "track:a1",
            "--set",
            "flag-default=0",
            "--edit",
            "track:a2",
            "--set",
            "flag-default=1",
        ]
    ]


def test_transcript_key_follows_which_track_is_transcribed():
    from curatarr.mediafile import MediaInfo
    from curatarr.transcribe import cache_key

    def info(default_index):
        return MediaInfo(
            path="/tv/x.mkv",
            size=1,
            mtime=1,
            duration=60,
            format_name="matroska",
            streams=[
                {
                    "index": 1,
                    "codec_type": "audio",
                    "language": "jpn",
                    "default": default_index == 1,
                },
                {
                    "index": 2,
                    "codec_type": "audio",
                    "language": "eng",
                    "default": default_index == 2,
                },
            ],
        )

    assert cache_key(info(1), "medium") != cache_key(info(2), "medium")
