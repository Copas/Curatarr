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


def test_a_few_stray_matches_in_a_quiet_stretch_dont_condemn_the_file():
    speech = _speech()
    first = subtitle_sync.windows(DURATION)[0]
    quiet = [s for s in speech if not first[0] <= s.start < first[1]]
    quiet += [s for s in speech if first[0] <= s.start < first[0] + 16][:4]
    quiet.sort(key=lambda s: s.start)
    second = subtitle_sync.windows(DURATION)[1]
    # A second off in one stretch: uneven, but close enough.
    lines = _subtitles(
        speech, lambda t: t + (2.4 if second[0] <= t < second[1] else 0.4)
    )
    # Two of the quiet stretch's four lines are matched far away.
    texts = subtitle_sync.parse_srt(lines)
    early = [s for s in quiet if first[0] <= s.start < first[1]]
    for segment, offset in zip(early[:2], (28.0, 44.0), strict=True):
        moved = Segment(segment.start + offset, segment.end + offset, segment.text)
        texts = [line for line in texts if line.text != segment.text]
        texts.append(subtitle_sync.Line(moved.start, moved.end, moved.text))
    result = subtitle_sync.judge(texts, quiet, DURATION)
    assert result.verdict == "in_sync" and "close enough" in result.reason


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


def test_times_are_read_exactly():
    lines = subtitle_sync.parse_srt(
        "1\n00:00:46,083 --> 00:00:47,5\nA\n\n2\n01:02:03.004 --> 01:02:04,040\nB\n"
    )
    assert [(line.start, line.end) for line in lines] == [
        (46.083, 47.5),
        (3723.004, 3724.04),
    ]
    fixed = subtitle_sync.retime("1\n00:00:46,083 --> 00:00:48,009\nA\n", 1.0, 1.0)
    assert "00:00:45,083 --> 00:00:47,009" in fixed


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
    from curatarr import outside_subtitles

    part, sidecar, speech, _calls, _arr = sync_env
    state = _run(part)  # no subtitles yet: Curatarr's own are made
    generated_path(part.file_path).unlink()
    state.subtitle_status, state.subtitle_fingerprint = "existing", None
    db.session.commit()
    assert outside_subtitles.sweep() == 0  # nothing beside it to check
    set_setting(outside_subtitles.KEY, {})
    sidecar.write_text(_subtitles(speech, lambda t: 8.0 + t), encoding="utf-8")
    assert outside_subtitles.sweep() == 1
    assert _state(part).requested_action == "subtitles"
    enrichment.dispatch(synchronous=True)
    assert _state(part).subtitle_status == "resynced"
    set_setting(outside_subtitles.KEY, {})
    assert outside_subtitles.sweep() == 0  # checked as it is now


ASYNC = {"X-Curatarr-Async": "1"}


def test_the_subtitles_page_shows_what_happened_and_undoes_it(app, client, sync_env):
    part, sidecar, speech, _calls, _arr = sync_env
    original = _subtitles(speech, lambda t: 8.0 + t)
    sidecar.write_text(original, encoding="utf-8")
    state = _run(part)
    page = client.get("/chapters/subtitles").get_data(as_text=True)
    assert "Re-timed" in page and "8.0 s late at the start" in page
    assert ">Subtitles<" in page  # the Enhance tab
    assert "Make Curatarr&#39;s own for every file" in page
    card = client.get(f"/chapters/subtitles/{state.id}/card").get_data(as_text=True)
    assert 'data-live="card"' in card and "Undo" in card
    assert 'data-live="head"' in client.get("/chapters/subtitles/summary").get_data(
        as_text=True
    )
    answer = client.post(
        f"/chapters/subtitles/{state.id}/undo",
        data={"name": "Film.en.srt"},
        headers=ASYNC,
    ).get_json()
    assert answer["ok"] and "original timing" in answer["messages"][0]["text"]
    assert sidecar.read_text() == original
    assert not sidecar.with_name("Film.en.srt.unsynced").exists()
    state = _state(part)
    assert state.subtitle_status == "existing"
    assert state.subtitle_sync["files"]["Film.en.srt"]["outcome"] == "undone"
    # Your choice stands: checking everything again leaves it alone.
    from curatarr import outside_subtitles

    assert outside_subtitles.check_all() == 0
    assert outside_subtitles.sweep() == 0
    # Undoing twice is refused, politely.
    again = client.post(
        f"/chapters/subtitles/{state.id}/undo",
        data={"name": "Film.en.srt"},
        headers=ASYNC,
    ).get_json()
    assert not again["ok"] and "weren't re-timed" in again["messages"][0]["text"]


def test_set_aside_subtitles_can_be_brought_back(app, client, sync_env):
    part, sidecar, speech, _calls, _arr = sync_env
    text = _subtitles(speech, lambda t: t if t < DURATION / 2 else t + 6.0)
    sidecar.write_text(text, encoding="utf-8")
    state = _run(part)
    assert "Set aside" in client.get("/chapters/subtitles").get_data(as_text=True)
    answer = client.post(
        f"/chapters/subtitles/{state.id}/restore",
        data={"name": "Film.en.srt"},
        headers=ASYNC,
    ).get_json()
    assert answer["ok"] and sidecar.read_text() == text
    assert generated_path(part.file_path).exists()  # Curatarr's stay
    assert _state(part).subtitle_sync["files"]["Film.en.srt"]["outcome"] == "kept"


def test_check_again_and_check_all(app, client, sync_env):
    from curatarr import outside_subtitles

    part, sidecar, speech, _calls, _arr = sync_env
    sidecar.write_text(_subtitles(speech, lambda t: 8.0 + t), encoding="utf-8")
    state = _run(part)
    answer = client.post(
        f"/chapters/subtitles/{state.id}/recheck", headers=ASYNC
    ).get_json()
    assert answer["ok"] and _state(part).job_state == "queued"
    enrichment.dispatch(synchronous=True)
    state = _state(part)
    # In sync now, but still listed as what Curatarr re-timed.
    assert state.subtitle_status == "resynced"
    assert state.subtitle_sync["files"]["Film.en.srt"]["backup"]
    assert client.post("/chapters/subtitles/check-all", headers=ASYNC).get_json()["ok"]
    assert outside_subtitles.sweep() == 1


def test_curatarrs_own_for_every_file(app, client, sync_env):
    part, sidecar, speech, _calls, _arr = sync_env
    sidecar.write_text(_subtitles(speech, lambda t: t), encoding="utf-8")
    assert _run(part).subtitle_status == "existing"
    answer = client.post("/chapters/subtitles/make-all", headers=ASYNC).get_json()
    assert answer["ok"] and "Queued 1 file" in answer["messages"][0]["text"]
    enrichment.dispatch(synchronous=True)
    assert _state(part).subtitle_status == "generated"
    assert generated_path(part.file_path).exists() and sidecar.exists()
    # Removed again: no longer wanted regardless.
    enrichment.request(part, "remove_subtitles")
    enrichment.dispatch(synchronous=True)
    assert not (_state(part).subtitle_sync or {}).get("own")


def test_slightly_uneven_subtitles_are_close_enough_to_keep():
    speech = _speech()
    # Out by up to 2.5 s in places, by different amounts: they read fine.
    steps = {0: 0.2, 1: 2.5, 2: -0.4, 3: 1.0}
    stretches = subtitle_sync.windows(DURATION)

    def timing(t):
        index = next(i for i, (s, e) in enumerate(stretches) if s <= t < e)
        return t + steps[index]

    result = _judge(_subtitles(speech, timing), speech)
    assert result.verdict == "in_sync" and "close enough" in result.reason


def test_one_stretch_in_another_language_doesnt_stop_the_check(sync_env, monkeypatch):
    part, sidecar, speech, _calls, _arr = sync_env
    first = subtitle_sync.windows(DURATION)[0]

    def fake_transcribe(info, model, device, cache, window=None):
        found = [s for s in speech if window and window[0] <= s.start < window[1]]
        return found, {"language": "cy" if window == first else "en"}

    monkeypatch.setattr("curatarr.transcribe.transcribe", fake_transcribe)
    sidecar.write_text(_subtitles(speech, lambda t: 8.0 + t), encoding="utf-8")
    assert _run(part).subtitle_status == "resynced"


def test_retiming_works_where_file_dates_cant_be_copied(sync_env, monkeypatch):
    part, sidecar, speech, _calls, _arr = sync_env

    def refused(*_a, **_k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("shutil.copystat", refused)
    monkeypatch.setattr("shutil.copymode", refused)
    sidecar.write_text(_subtitles(speech, lambda t: 8.0 + t), encoding="utf-8")
    assert _run(part).subtitle_status == "resynced"
    assert sidecar.with_name("Film.en.srt.unsynced").exists()


def test_files_set_aside_under_older_rules_are_put_back_and_checked_again(
    sync_env,
):
    from curatarr import outside_subtitles

    part, sidecar, speech, _calls, _arr = sync_env
    text = _subtitles(speech, lambda t: t + 0.3)  # in sync really
    sidecar.write_text(text, encoding="utf-8")
    state = _run(part)
    # As an older version left it: set aside, and Curatarr's own made.
    aside = sidecar.with_name("Film.en.srt.out-of-sync")
    sidecar.rename(aside)
    state.subtitle_sync = {
        "files": {"Film.en.srt": {"outcome": "set_aside", "aside": aside.name}}
    }
    db.session.commit()
    enrichment.request(part, "subtitles")
    enrichment.dispatch(synchronous=True)
    assert generated_path(part.file_path).exists()
    set_setting(outside_subtitles.RULES_KEY, 1)
    assert outside_subtitles.revisit() == 1
    assert sidecar.read_text() == text and not aside.exists()
    assert not generated_path(part.file_path).exists()
    assert outside_subtitles.sweep() == 1
    enrichment.dispatch(synchronous=True)
    state = _state(part)
    assert state.subtitle_status == "existing"
    assert state.subtitle_sync["files"]["Film.en.srt"]["outcome"] == "in_sync"
    assert outside_subtitles.revisit() == 0  # once


def test_files_retimed_before_the_time_fix_are_retimed_again(sync_env):
    from curatarr import outside_subtitles
    from curatarr.enrichment import _sidecar_key

    part, sidecar, speech, _calls, _arr = sync_env
    original = _subtitles(speech, lambda t: 8.0 + t)
    sidecar.write_text(original, encoding="utf-8")
    state = _run(part)
    right = sidecar.read_text()
    # As an older version wrote it, nobody having changed it since.
    sidecar.write_text(right.replace(",0", ",9"), encoding="utf-8")
    record = dict(state.subtitle_sync)
    record["files"] = {
        "Film.en.srt": record["files"]["Film.en.srt"] | {"key": _sidecar_key(sidecar)}
    }
    state.subtitle_sync = record
    db.session.commit()
    set_setting(outside_subtitles.RULES_KEY, 3)
    assert outside_subtitles.revisit() == 1
    assert sidecar.read_text() == right
    entry = _state(part).subtitle_sync["files"]["Film.en.srt"]
    assert entry["key"] == _sidecar_key(sidecar) and entry["outcome"] == "resynced"


def test_which_episode_a_file_is_from_its_seasons_subtitles():
    speech = _speech()
    others = {
        2: subtitle_sync.parse_srt(_subtitles(_speech(seed=2), lambda t: t)),
        3: subtitle_sync.parse_srt(_subtitles(speech, lambda t: t + 0.5)),
        4: subtitle_sync.parse_srt(_subtitles(_speech(seed=4), lambda t: t)),
    }
    number, found, spoken = subtitle_sync.which_episode(speech, others)
    assert number == 3 and found > 0.8 * spoken
    del others[3]
    assert subtitle_sync.which_episode(speech, others) is None


def test_a_subtitle_check_records_which_episode_a_file_really_is(app, tmp_path):
    from curatarr.enrichment import _other_episode
    from curatarr.models import MediaPart

    media = _show_with_files(tmp_path, {2: _speech(seed=2), 3: _speech()})
    part = MediaPart(
        media_identity_id=media.id,
        kind="episode",
        season_number=1,
        episode_number=1,
        has_file=True,
        file_path=str(tmp_path / "S01E01.mkv"),
    )
    db.session.add(part)
    db.session.commit()
    # E1's file says what E3's subtitles say.
    assert _other_episode(part, _speech()) | {} == {
        "fits": 3,
        "found": pytest.approx(len(_speech()), abs=8),
        "spoken": len(_speech()),
    }


def _show_with_files(folder, subtitles):
    """A show with an episode file per number, each with English subtitles
    made from the given speech."""
    from curatarr.models import Library, MediaIdentity, MediaPart

    library = Library(jellyfin_library_id="tv", name="TV", media_type="tvshows")
    db.session.add(library)
    db.session.flush()
    media = MediaIdentity(
        library_id=library.id, media_type="series", title="Show", sonarr_id=7
    )
    db.session.add(media)
    db.session.flush()
    for number, speech in subtitles.items():
        video = folder / f"S01E{number:02}.mkv"
        video.touch()
        video.with_name(f"S01E{number:02}.en.srt").write_text(
            _subtitles(speech, lambda t: t), encoding="utf-8"
        )
        db.session.add(
            MediaPart(
                media_identity_id=media.id,
                kind="episode",
                season_number=1,
                episode_number=number,
                has_file=True,
                file_path=str(video),
            )
        )
    db.session.commit()
    return media
