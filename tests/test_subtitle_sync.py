"""Whether subtitles beside a video line up with the speech: the measuring
(pure) and the subtitle job that re-times or sets them aside."""

import random
from pathlib import Path

import pytest
from test_enrichment import media_env, not_paused  # noqa: F401
from test_subtitles import _state, subtitle_env  # noqa: F401

from curatarr import db, enrichment, subtitle_sync
from curatarr.chapters import Segment
from curatarr.models import LifecycleAction
from curatarr.services import set_setting
from curatarr.subtitles import cues_from, generated_path, srt_text

DURATION = 3600.0
WORDS = [
    *("river", "lamp", "quiet", "thunder", "basket", "orange", "window", "seven"),
    *("garden", "pocket", "silver", "ladder", "morning", "velvet", "candle"),
    *("hammer", "north", "stable", "whistle", "copper", "meadow", "bottle"),
    *("shadow", "marble", "cousin", "island", "button", "feather", "rocket"),
]


def _speech(duration=DURATION, seed=1):
    """A line every 4 seconds through the stretches the check listens to."""
    rng = random.Random(seed)
    segments = []
    for start, end in subtitle_sync.windows(duration):
        t = start + 1.0
        while t < end - 4:
            words = " ".join(rng.choice(WORDS) for _ in range(6))
            segments.append(Segment(t, t + 3, words.capitalize() + "."))
            t += 4.0
    return segments


def _subtitles(segments, timing):
    """SRT text for those lines, each moved by timing(t) seconds."""
    moved = [Segment(timing(s.start), timing(s.end), s.text) for s in segments]
    return srt_text(cues_from(moved))


def _judge(text, segments, duration=DURATION):
    return subtitle_sync.judge(subtitle_sync.parse_srt(text), segments, duration)


def test_subtitles_that_line_up_are_in_sync():
    speech = _speech()
    result = _judge(_subtitles(speech, lambda t: t + 0.4), speech)
    assert result.verdict == "in_sync" and result.points > 50


def test_a_delay_and_a_frame_rate_drift_are_measured_and_undone():
    speech = _speech()
    rate = 24 / 23.976
    text = _subtitles(speech, lambda t: 11.0 + rate * t)
    result = _judge(text, speech)
    assert result.verdict == "shifted"
    assert result.rate == rate  # snapped to the known conversion
    assert result.delay == pytest.approx(11.0, abs=0.05)
    assert "11.0 s late at the start" in result.reason
    fixed = subtitle_sync.retime(text, result.delay, result.rate)
    assert _judge(fixed, speech).verdict == "in_sync"
    first = subtitle_sync.parse_srt(fixed)[0]
    assert first.start == pytest.approx(speech[0].start, abs=0.05)


def test_subtitles_cut_for_another_version_cant_be_shifted_into_line():
    speech = _speech()
    middle = DURATION / 2
    text = _subtitles(speech, lambda t: t if t < middle else t + 6.0)
    result = _judge(text, speech)
    assert result.verdict == "mismatch" and "different amounts" in result.reason


def test_subtitles_for_something_else_are_a_mismatch():
    speech = _speech()
    other = _speech(seed=2)
    assert _judge(_subtitles(other, lambda t: t), speech).verdict == "mismatch"


def test_subtitles_for_only_some_parts_are_left_undecided():
    speech = _speech()
    other = _speech(seed=2)[::5]  # signs, or the parts in another language
    result = _judge(_subtitles(other, lambda t: t), speech)
    assert result.verdict == "unknown" and "few lines" in result.reason


def test_too_little_speech_is_left_undecided():
    speech = _speech()[:5]
    result = _judge(_subtitles(speech, lambda t: t + 30), speech)
    assert result.verdict == "unknown"


def test_retiming_keeps_everything_but_the_times():
    text = (
        "1\r\n00:00:12,500 --> 00:00:14,000 X1:10\r\n<i>Hello</i>\r\n\r\n"
        "2\r\n01:00:00,000 --> 01:00:02,000\r\nBye\r\n"
    )
    fixed = subtitle_sync.retime(text, 10.0, 1.0)
    assert fixed.startswith("1\r\n00:00:02,500 --> 00:00:04,000 X1:10\r\n<i>Hello")
    assert "00:59:50,000 --> 00:59:52,000\r\nBye" in fixed
    assert subtitle_sync.retime(text, 20.0, 1.0).startswith(
        "1\r\n00:00:00,000 --> 00:00:00,000"
    )  # never before the start


@pytest.fixture
def sync_env(subtitle_env, monkeypatch):  # noqa: F811
    _files, arr, _library, _media, part, calls = subtitle_env
    speech = _speech()

    def fake_transcribe(info, model, device, cache, window=None):
        calls.append(info.path if window is None else window)
        found = (
            speech
            if window is None
            else [s for s in speech if window[0] <= s.start < window[1]]
        )
        return found, {"language": "en"}

    monkeypatch.setattr("curatarr.transcribe.transcribe", fake_transcribe)
    sidecar = Path(part.file_path).with_name("Film.en.srt")
    return part, sidecar, speech, calls, arr


def _run(part):
    enrichment.request(part, "subtitles")
    enrichment.dispatch(synchronous=True)
    return _state(part)


def test_out_of_sync_subtitles_are_retimed_and_the_original_kept(sync_env):
    part, sidecar, speech, calls, arr = sync_env
    original = _subtitles(speech, lambda t: 8.0 + t)
    sidecar.write_text(original, encoding="utf-8")
    state = _run(part)
    assert state.subtitle_status == "resynced"
    assert sidecar.with_name("Film.en.srt.unsynced").read_text() == original
    assert _judge(sidecar.read_text(), speech).verdict == "in_sync"
    assert not generated_path(part.file_path).exists()
    assert all(isinstance(call, tuple) for call in calls)  # only listened to
    entry = state.subtitle_sync["files"]["Film.en.srt"]
    assert entry["outcome"] == "resynced" and entry["result"]["verdict"] == "shifted"
    assert (
        db.session.query(LifecycleAction)
        .filter_by(action_type="subtitles_resynced")
        .count()
        == 1
    )
    assert "jf-film" in arr.refreshed
    # Checked as it is now: not listened to again.
    calls.clear()
    assert _run(part).subtitle_status == "resynced" and calls == []
    assert enrichment.describe(_state(part))["subtitles"].endswith("re-timed")


def test_subtitles_in_sync_are_left_alone(sync_env):
    part, sidecar, speech, _calls, _arr = sync_env
    text = _subtitles(speech, lambda t: t + 0.3)
    sidecar.write_text(text, encoding="utf-8")
    assert _run(part).subtitle_status == "existing"
    assert sidecar.read_text() == text
    assert not sidecar.with_name("Film.en.srt.unsynced").exists()


def test_unfixable_subtitles_are_set_aside_and_replaced_then_restored(sync_env):
    part, sidecar, speech, calls, _arr = sync_env
    text = _subtitles(speech, lambda t: t if t < DURATION / 2 else t + 6.0)
    sidecar.write_text(text, encoding="utf-8")
    state = _run(part)
    aside = sidecar.with_name("Film.en.srt.out-of-sync")
    assert state.subtitle_status == "generated"
    assert not sidecar.exists() and aside.read_text() == text
    assert generated_path(part.file_path).exists()
    assert part.file_path in calls  # transcribed in full for Curatarr's own
    enrichment.request(part, "remove_subtitles")
    enrichment.dispatch(synchronous=True)
    assert sidecar.read_text() == text and not aside.exists()
    assert _state(part).subtitle_status == "existing"


def test_the_check_can_be_switched_off(sync_env):
    part, sidecar, speech, calls, _arr = sync_env
    library_id = part.media.library_id
    set_setting(
        "chapter_library_settings",
        {
            library_id: {
                "enabled": True,
                "mode": "navigation",
                "subtitles": {"enabled": True, "check_sync": False},
            }
        },
    )
    sidecar.write_text(_subtitles(speech, lambda t: 8.0 + t), encoding="utf-8")
    assert _run(part).subtitle_status == "existing" and calls == []


def test_subtitles_the_library_already_had_are_checked_a_few_at_a_time(sync_env):
    from curatarr import subtitle_sync_sweep

    part, sidecar, speech, _calls, _arr = sync_env
    state = _run(part)  # no subtitles yet: Curatarr's own are made
    generated_path(part.file_path).unlink()
    state.subtitle_status, state.subtitle_fingerprint = "existing", None
    db.session.commit()
    assert subtitle_sync_sweep.sweep() == 0  # nothing beside it to check
    set_setting(subtitle_sync_sweep.KEY, {})
    sidecar.write_text(_subtitles(speech, lambda t: 8.0 + t), encoding="utf-8")
    assert subtitle_sync_sweep.sweep() == 1
    assert _state(part).requested_action == "subtitles"
    enrichment.dispatch(synchronous=True)
    assert _state(part).subtitle_status == "resynced"
    set_setting(subtitle_sync_sweep.KEY, {})
    assert subtitle_sync_sweep.sweep() == 0  # checked as it is now
