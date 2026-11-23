"""Playback trouble (playback_trouble.py): files a device keeps giving up on."""

from datetime import timedelta

from test_enrichment import _movie

from curatarr import db, playback_trouble
from curatarr.mediafile import MediaInfo, compatible_audio_source
from curatarr.models import PlaybackTrouble, PlaybackTry, utcnow

ROKU = "Living Room Roku"


def _session(position, *, session="s1", device=ROKU, audio="opus", method="DirectPlay"):
    return {
        "Id": session,
        "UserId": "U-1",
        "DeviceName": device,
        "Client": "Jellyfin Roku",
        "PlayState": {
            "PositionTicks": int(position * 10_000_000),
            "PlayMethod": method,
        },
        "NowPlayingItem": {
            "Id": "jf-film",
            "Type": "Movie",
            "RunTimeTicks": 2600 * 10_000_000,
            "MediaStreams": [
                {"Type": "Video", "Codec": "hevc"},
                {"Type": "Audio", "Codec": audio, "IsDefault": True},
            ],
        },
    }


def _try(start, seconds, **kwargs):
    """Play for ``seconds`` from ``start``, then stop."""
    playback_trouble.record([_session(0, **kwargs)], start)
    playback_trouble.record(
        [_session(seconds, **kwargs)], start + timedelta(seconds=seconds)
    )
    playback_trouble.record(
        [], start + timedelta(seconds=seconds) + timedelta(minutes=3)
    )


def test_a_try_is_recorded_with_how_it_played(app):
    _library, _media, part = _movie()
    now = utcnow()
    _try(now, 90)
    (try_,) = db.session.query(PlaybackTry).all()
    assert try_.media_part_id == part.id and try_.device == ROKU
    assert (try_.audio_codec, try_.video_codec, try_.play_method) == (
        "opus",
        "hevc",
        "DirectPlay",
    )
    assert try_.furthest == 90 and try_.ended


def test_two_early_tries_are_trouble_and_the_audio_gets_a_track(app, monkeypatch):
    _library, _media, part = _movie()
    monkeypatch.setattr("curatarr.compat_audio.tv_enabled", lambda: True)
    now = utcnow()
    _try(now, 120)
    assert playback_trouble.find(now + timedelta(minutes=10)) == []  # once is nothing
    _try(now + timedelta(hours=9), 80)
    later = now + timedelta(hours=10)
    (trouble,) = playback_trouble.find(later)
    assert trouble.details["suspect"] == "audio"
    assert "OPUS audio sent to the device as it is" in trouble.details["words"]
    assert playback_trouble.act([trouble]) == 1
    assert playback_trouble.queued_part_ids() == {part.id}
    assert playback_trouble.find(later) == []  # found once
    # TV-friendly audio takes any audio a TV gave up on, not only its list.
    info = MediaInfo(
        path="x",
        size=1,
        mtime=1,
        duration=10,
        streams=[
            {"index": 1, "codec_type": "audio", "codec_name": "vorbis", "default": True}
        ],
        chapters=[],
        format_name="matroska,webm",
    )
    assert compatible_audio_source(info) is None
    assert compatible_audio_source(info, any_not_easy=True) == 1
    playback_trouble.done(part.id, added=True)
    db.session.commit()
    assert db.session.get(PlaybackTrouble, trouble.id).state == "fixed"


def test_watching_properly_isnt_trouble(app):
    _movie()
    now = utcnow()
    _try(now, 100)
    _try(now + timedelta(hours=1), 1500)  # got going the second time
    assert playback_trouble.find(now + timedelta(hours=2)) == []


def test_tries_far_apart_or_on_other_devices_arent_trouble(app):
    _movie()
    now = utcnow()
    _try(now, 100)
    _try(now + timedelta(hours=1), 100, session="s2", device="Phone")
    _try(now + timedelta(days=2), 100)
    assert playback_trouble.find(now + timedelta(days=2, hours=1)) == []


def test_converted_audio_is_listed_not_acted_on(app, monkeypatch):
    _movie()
    monkeypatch.setattr("curatarr.compat_audio.tv_enabled", lambda: True)
    now = utcnow()
    _try(now, 60, audio="aac", method="Transcode")
    _try(now + timedelta(hours=1), 60, audio="aac", method="Transcode")
    (trouble,) = playback_trouble.find(now + timedelta(hours=2))
    assert trouble.details["suspect"] != "audio"
    assert playback_trouble.act([trouble]) == 0
    assert playback_trouble.count() == 1


def test_problems_lists_it_and_keep_takes_it_off(app, client):
    _movie()
    now = utcnow()
    _try(now, 60)
    _try(now + timedelta(hours=1), 60)
    (trouble,) = playback_trouble.find(now + timedelta(hours=2))
    page = client.get("/chapters/problems").text
    assert 'id="playback-trouble"' in page and ROKU in page
    assert "OPUS audio sent to the device as it is" in page
    client.post(f"/chapters/problems/playback/{trouble.id}/keep")
    assert playback_trouble.count() == 0
    assert ROKU not in client.get("/chapters/problems").text


def test_a_format_shared_by_several_troubled_files_is_called_out(app):
    for n in range(playback_trouble.PATTERN):
        db.session.add(
            PlaybackTrouble(
                item_id=f"item-{n}",
                device=ROKU,
                found_at=utcnow(),
                last_try_at=utcnow(),
                details={"client": "Jellyfin Roku", "audio": "opus", "video": f"v{n}"},
            )
        )
    db.session.commit()
    assert playback_trouble.patterns() == [
        {"what": "OPUS audio", "client": "Jellyfin Roku", "files": 3}
    ]


def test_the_audio_sweep_takes_a_queued_file_first_even_if_looked_at(app):
    from curatarr import compat_audio
    from curatarr.enrichment import state_for

    _library, _media, part = _movie()
    part.size_bytes = 100
    state = state_for(part)
    state.compat_audio_size = 100  # looked at: nothing needed by its list
    db.session.add(
        PlaybackTrouble(
            item_id="jf-film",
            device=ROKU,
            media_part_id=part.id,
            found_at=utcnow(),
            last_try_at=utcnow(),
            details={"suspect": "audio"},
            state="queued",
        )
    )
    db.session.commit()
    assert [p.id for p in compat_audio._due(set(), set())] == [part.id]
