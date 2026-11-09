"""Generated subtitles: cue building (pure) and the subtitle job."""

from itertools import pairwise
from pathlib import Path

import pytest
from test_enrichment import _movie, media_env, not_paused  # noqa: F401

from curatarr import db, enrichment
from curatarr.chapters import Segment
from curatarr.models import ChapterState, LifecycleAction
from curatarr.services import set_setting
from curatarr.subtitles import (
    MAX_CUE_SECONDS,
    MAX_LINE,
    cues_from,
    embedded_english,
    generated_path,
    sidecar_english,
    srt_text,
)
from curatarr.transcribe import RULE


def test_cues_are_readable_and_never_overlap():
    long = " ".join(f"word{i}" for i in range(60))
    segments = [
        Segment(0.0, 2.0, " Hello there. "),
        Segment(2.0, 20.0, long),
        Segment(20.0, 20.2, "Hi"),
        Segment(20.25, 22.0, "Quick reply"),
    ]
    cues = cues_from(segments)
    assert cues[0].lines == ["Hello there."]
    for cue in cues:
        assert len(cue.lines) <= 2
        assert all(len(line) <= MAX_LINE for line in cue.lines)
        assert cue.end - cue.start <= MAX_CUE_SECONDS + 1e-6
    for before, after in pairwise(cues):
        assert before.end <= after.start
    words = " ".join(" ".join(c.lines) for c in cues).split()
    assert words == ["Hello", "there.", *long.split(), "Hi", "Quick", "reply"]


def test_srt_format():
    text = srt_text(cues_from([Segment(61.5, 63.25, "First line here")]))
    assert text == "1\n00:01:01,500 --> 00:01:03,250\nFirst line here\n"


def test_english_detection_in_the_file_and_beside_it(tmp_path):
    assert embedded_english([{"codec_type": "subtitle", "language": "eng"}])
    assert embedded_english([{"codec_type": "subtitle", "language": None}])
    assert not embedded_english(
        [{"codec_type": "subtitle", "language": "eng", "forced": True}]
    )
    assert not embedded_english([{"codec_type": "subtitle", "language": "spa"}])
    video = tmp_path / "Show - S01E01.mkv"
    video.touch()
    assert sidecar_english(str(video)) == []
    (tmp_path / "Show - S01E01.es.srt").touch()
    (tmp_path / "Show - S01E01.en.forced.srt").touch()
    (tmp_path / "Show - S01E01.en.Curatarr.srt").touch()
    (tmp_path / "Show - S01E011.en.srt").touch()  # another episode
    assert sidecar_english(str(video)) == []
    (tmp_path / "Show - S01E01.en.sup").touch()  # Blu-ray pictures
    assert sidecar_english(str(video)) == ["Show - S01E01.en.sup"]
    assert sidecar_english(str(video), text_only=True) == []
    (tmp_path / "Show - S01E01.en.sup").unlink()
    (tmp_path / "Show - S01E01.eng.sdh.srt").touch()
    assert sidecar_english(str(video)) == ["Show - S01E01.eng.sdh.srt"]
    assert generated_path(str(video)).name == "Show - S01E01.en.Curatarr.srt"


@pytest.fixture
def subtitle_env(media_env, tmp_path, monkeypatch):  # noqa: F811
    files, arr = media_env
    library, media, part = _movie(path=str(tmp_path / "Film (2020)" / "Film.mkv"))
    Path(part.file_path).parent.mkdir()
    Path(part.file_path).touch()
    files.add(part.file_path)
    set_setting(
        "chapter_library_settings",
        {
            library.id: {
                "enabled": True,
                "mode": "navigation",
                "subtitles": {"enabled": True},
            }
        },
    )
    calls = []

    def fake_transcribe(info, model, device, cache, window=None, **_kwargs):
        calls.append(info.path if window is None else window)
        return [Segment(1, 3, "Hello."), Segment(4, 6, "Goodbye.")], {
            "language": "en",
            "rule": RULE,
            "cached": len(calls) > 1,
        }

    monkeypatch.setattr("curatarr.transcribe.transcribe", fake_transcribe)
    return files, arr, library, media, part, calls


def _state(part):
    db.session.expire_all()
    return db.session.query(ChapterState).filter_by(media_part_id=part.id).one()


def test_chapter_job_chains_into_subtitles(subtitle_env):
    _files, arr, _library, _media, part, _calls = subtitle_env
    enrichment.request(part, "generate")
    enrichment.dispatch(synchronous=True)
    assert _state(part).requested_action == "subtitles"  # queued after chapters
    enrichment.dispatch(synchronous=True)
    state = _state(part)
    target = generated_path(part.file_path)
    assert state.subtitle_status == "generated" and target.exists()
    assert "Hello." in target.read_text() and state.subtitle_path == str(target)
    assert arr.refreshed.count("jf-film") == 2  # after chapters, after subtitles
    assert (
        db.session.query(LifecycleAction)
        .filter_by(action_type="subtitles_generated")
        .count()
        == 1
    )


def test_existing_english_is_respected_and_others_files_untouched(subtitle_env):
    _files, _arr, _library, _media, part, calls = subtitle_env
    sidecar = Path(part.file_path).with_name("Film.en.srt")
    sidecar.write_text("theirs")
    enrichment.request(part, "subtitles")
    enrichment.dispatch(synchronous=True)
    # Listened to for the sync check (too little to tell), never transcribed
    # in full, and their file is left as it was.
    assert _state(part).subtitle_status == "existing"
    assert calls and all(isinstance(call, tuple) for call in calls)
    assert sidecar.read_text() == "theirs"
    sidecar.unlink()
    target = generated_path(part.file_path)
    target.write_text("someone else's")
    enrichment.request(part, "subtitles")
    enrichment.dispatch(synchronous=True)
    assert _state(part).subtitle_status == "existing"
    assert target.read_text() == "someone else's"
    enrichment.request(part, "remove_subtitles")
    enrichment.dispatch(synchronous=True)
    assert target.exists() and "left alone" in _state(part).subtitle_error


PGS = {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "language": "eng"}


def _with_picture_subtitles(files, monkeypatch):
    plain = files.probe

    def probe(path):
        info = plain(path)
        info.streams.append(dict(PGS))
        return info

    monkeypatch.setattr(enrichment, "probe", probe)


def test_picture_subtitles_get_a_text_track(subtitle_env, monkeypatch):
    files, _arr, _library, _media, part, calls = subtitle_env
    _with_picture_subtitles(files, monkeypatch)
    assert embedded_english([PGS]) and not embedded_english([PGS], text_only=True)
    enrichment.request(part, "subtitles")
    enrichment.dispatch(synchronous=True)
    assert _state(part).subtitle_status == "generated" and calls


def test_picture_subtitles_can_be_left_as_they_are(subtitle_env, monkeypatch):
    from curatarr import picture_subtitles

    files, _arr, library, _media, part, calls = subtitle_env
    _with_picture_subtitles(files, monkeypatch)
    settings = {
        "enabled": True,
        "mode": "navigation",
        "subtitles": {"enabled": True, "replace_pictures": False},
    }
    set_setting("chapter_library_settings", {library.id: settings})
    enrichment.request(part, "subtitles")
    enrichment.dispatch(synchronous=True)
    assert _state(part).subtitle_status == "pictures" and calls == []
    assert enrichment.maintenance_summary()["subtitles_have"] == 1
    assert picture_subtitles.sweep() == 0
    # Switched on: queued for a text track.
    settings["subtitles"]["replace_pictures"] = True
    set_setting("chapter_library_settings", {library.id: settings})
    assert picture_subtitles.sweep() == 1
    assert _state(part).requested_action == "subtitles"


def test_files_checked_before_are_looked_at_again(subtitle_env, monkeypatch):
    from curatarr import picture_subtitles

    files, _arr, _library, _media, part, _calls = subtitle_env
    enrichment.request(part, "subtitles")
    state = _state(part)
    state.job_state, state.subtitle_status = "idle", "existing"
    db.session.commit()
    assert picture_subtitles.sweep() == 0  # text-only check: nothing to do yet
    _with_picture_subtitles(files, monkeypatch)
    set_setting("picture_subtitles", {})
    assert picture_subtitles.sweep() == 1
    assert _state(part).job_state == "queued"
    enrichment.dispatch(synchronous=True)
    assert _state(part).subtitle_status == "generated"
    assert picture_subtitles.sweep() == 0  # finished


def test_non_english_audio_is_skipped(subtitle_env, monkeypatch):
    _files, _arr, _library, _media, part, _calls = subtitle_env
    monkeypatch.setattr(
        "curatarr.transcribe.transcribe",
        lambda *_a, **_k: ([Segment(1, 2, "Hola.")], {"language": "es"}),
    )
    enrichment.request(part, "subtitles")
    enrichment.dispatch(synchronous=True)
    assert _state(part).subtitle_status == "skipped_language"
    assert not generated_path(part.file_path).exists()


def test_remove_only_unchanged_generated_subtitles(subtitle_env):
    _files, _arr, _library, _media, part, _calls = subtitle_env
    enrichment.request(part, "subtitles")
    enrichment.dispatch(synchronous=True)
    target = generated_path(part.file_path)
    target.write_text(target.read_text() + "\nedited")
    enrichment.request(part, "remove_subtitles")
    enrichment.dispatch(synchronous=True)
    assert target.exists()
    target.write_text(target.read_text().removesuffix("\nedited"))
    enrichment.request(part, "remove_subtitles")
    enrichment.dispatch(synchronous=True)
    assert not target.exists() and _state(part).subtitle_status == "removed"


def test_bulk_queue_and_pages(app, client, subtitle_env):
    _files, _arr, library, media, part, _calls = subtitle_env
    response = client.post("/chapters/subtitles", data={"library": library.id})
    assert response.status_code == 302 and _state(part).requested_action == "subtitles"
    enrichment.dispatch(synchronous=True)
    assert enrichment.queue_subtitles(library.id) == 0  # already done
    page = client.get(f"/chapters/parts/{part.id}").get_data(as_text=True)
    assert "Generated subtitles:</span> Generated" in page
    assert "Remove generated subtitles" in page
    assert 'Subtitles added</h3><p class="stat-value mb-0">1<' in client.get(
        "/chapters"
    ).get_data(as_text=True)
    assert "Generated subtitles" in client.get(f"/titles/{media.id}").get_data(
        as_text=True
    )


def test_new_imports_go_before_subtitles_that_need_transcribing(subtitle_env):
    from curatarr.models import MediaPart

    _files, _arr, _library, media, part, _calls = subtitle_env
    other = MediaPart(
        media_identity_id=media.id, kind="movie", has_file=True, file_path="/x.mkv"
    )
    db.session.add(other)
    db.session.commit()
    enrichment.request(part, "subtitles")  # no saved transcript: a backfill
    enrichment.request(other, "auto")  # a new import, queued later
    ids = [s.media_part_id for s in enrichment._due_jobs(10, scans=False)]
    assert ids.index(other.id) < ids.index(part.id)
    state = _state(part)
    state.transcript_key = "saved"
    db.session.commit()
    ids = [s.media_part_id for s in enrichment._due_jobs(10, scans=False)]
    assert ids[0] == part.id  # quick with a saved transcript: first


ENGLISH_LINES = "\n".join(
    f"{n}\n00:00:{n:02},000 --> 00:00:{n:02},900\nWhat do you want me to do with it?\n"
    for n in range(1, 30)
)
SPANISH_LINES = "\n".join(
    f"{n}\n00:00:{n:02},000 --> 00:00:{n:02},900\n¿Por qué actuaba tan raro con ella?\n"
    for n in range(1, 30)
)


def test_untagged_subtitles_count_as_english_only_if_they_read_as_english(tmp_path):
    from curatarr.subtitles import read_text, written_in_english

    video = tmp_path / "Film.mkv"
    video.touch()
    untagged = tmp_path / "Film.srt"
    untagged.write_text(SPANISH_LINES, encoding="cp1252")
    assert written_in_english(untagged) is False
    assert sidecar_english(str(video)) == []
    untagged.write_text(ENGLISH_LINES, encoding="utf-16")  # with its marker
    assert written_in_english(untagged) is True
    assert sidecar_english(str(video)) == ["Film.srt"]
    assert read_text(untagged) == (ENGLISH_LINES, "utf-16")
    untagged.write_bytes(ENGLISH_LINES.encode("utf-16-le"))  # without
    assert read_text(untagged) == (ENGLISH_LINES, "utf-16-le")
    assert written_in_english(untagged) is True
    untagged.write_text("1\n00:00:01,000 --> 00:00:02,000\nHola.\n")
    assert written_in_english(untagged) is None  # too few words to tell
    assert sidecar_english(str(video)) == ["Film.srt"]
    ass = tmp_path / "Film.ass"
    untagged.unlink()
    ass.write_text(
        "[V4+ Styles]\nFormat: Name, Fontname\nStyle: Default,Arial\n[Events]\n"
        + "".join(
            f"Dialogue: 0,0:00:{n:02}.00,0:00:{n:02}.90,Default,,0,0,0,,"
            "¿Por qué actuaba\\Ntan raro con ella?\n"
            for n in range(1, 30)
        )
    )
    assert written_in_english(ass) is False


def test_a_film_with_only_foreign_subtitles_gets_english_ones(subtitle_env):
    _files, _arr, _library, _media, part, _calls = subtitle_env
    foreign = Path(part.file_path).with_name("Film.srt")
    foreign.write_text(SPANISH_LINES, encoding="utf-8")
    enrichment.request(part, "subtitles")
    enrichment.dispatch(synchronous=True)
    assert _state(part).subtitle_status == "generated"
    assert foreign.read_text(encoding="utf-8") == SPANISH_LINES  # left alone


def _said(*words):
    """A segment from (start, end, word) triples, Whisper-style."""
    return Segment(words[0][0], words[-1][1], "".join(w for _s, _e, w in words), words)


def test_lines_are_timed_from_each_word():
    segments = [
        # A segment padded with silence: said only at the end.
        Segment(
            6.8,
            26.6,
            " Hold for the calculation.",
            (
                (25.1, 25.4, " Hold"),
                (25.4, 25.6, " for"),
                (25.6, 25.8, " the"),
                (25.8, 26.6, " calculation."),
            ),
        ),
        # A number in pieces, and a sentence running into the next segment.
        _said(
            (29.9, 30.2, " 1"),
            (30.2, 30.6, ",014"),
            (30.6, 31.0, " feet"),
            (31.0, 31.4, " and"),
            (31.4, 31.6, " no"),
        ),
        _said((31.6, 31.9, " one"), (31.9, 32.3, " else.")),
        # A pause: a new line.
        _said((40.0, 40.4, " Dump"), (40.4, 40.7, " it")),
        _said((42.0, 42.3, " out.")),
    ]
    cues = cues_from(segments)
    text = [" ".join(c.lines) for c in cues]
    assert text == [
        "Hold for the calculation.",
        "1,014 feet and no one else.",
        "Dump it",
        "out.",
    ]
    hold = cues[0]
    assert 24.9 <= hold.start <= 25.1 and hold.end <= 27.6  # not from 6.8, once
    assert 29.7 <= cues[1].start <= 29.9
    assert cues[2].end < cues[3].start


def test_a_long_word_doesnt_stretch_its_line_into_silence():
    cues = cues_from([_said((10.0, 18.0, " Hello."))])
    assert cues[0].start >= 16.3 and cues[0].end <= 19.0


def test_transcripts_keep_word_times(tmp_path):
    import json

    from curatarr.transcribe import has_words, load

    path = tmp_path / "t.json"
    path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "start": 1,
                        "end": 2,
                        "text": " Hi.",
                        "words": [[1.1, 1.5, " Hi."]],
                    }
                ]
            }
        )
    )
    segments, _details = load(path)
    assert segments[0].words == ((1.1, 1.5, " Hi."),) and has_words(segments)
    path.write_text(json.dumps({"segments": [{"start": 1, "end": 2, "text": " Hi."}]}))
    assert not has_words(load(path)[0])  # made before words were timed
    assert has_words([])  # nothing said: nothing to time


def test_how_subtitles_were_made_is_kept_and_old_ones_are_remade(
    subtitle_env, monkeypatch
):
    from curatarr import up_next
    from curatarr.services import setting
    from curatarr.subtitles import TIMING

    _files, _arr, _library, _media, part, _calls = subtitle_env
    enrichment.request(part, "subtitles")
    enrichment.dispatch(synchronous=True)
    state = _state(part)
    assert state.subtitle_made_with["timing"] == TIMING
    assert state.subtitle_made_with["engine"] == "faster-whisper"
    assert enrichment.remake_subtitles() == 0  # made the current way
    assert setting(enrichment.REMADE_KEY)["timing"] == TIMING

    state.subtitle_made_with = None  # one made before this was kept
    db.session.commit()
    assert enrichment.remake_subtitles() == 0  # "all done" is remembered…
    set_setting(enrichment.REMADE_KEY, None)  # …until something changes
    other = ChapterState(
        media_part_id="busy", media_identity_id=state.media_identity_id
    )
    other.job_state = "queued"
    db.session.add(other)
    db.session.commit()
    assert enrichment.remake_subtitles() == 0  # other work waits: not now
    monkeypatch.setattr(up_next, "part_ids", lambda users=None: [part.id])
    assert enrichment.remake_subtitles() == 1  # about to be watched: straight away
    db.session.delete(other)
    db.session.commit()
    enrichment.dispatch(synchronous=True)
    assert _state(part).subtitle_made_with["timing"] == TIMING
    monkeypatch.setattr(up_next, "part_ids", lambda users=None: [])
    assert enrichment.remake_subtitles() == 0


def test_a_new_whisper_model_remakes_them():
    from curatarr.subtitles import TIMING, TRANSCRIPTION, outdated

    now = {
        "engine": "faster-whisper",
        "model": "medium",
        "timing": TIMING,
        "transcription": TRANSCRIPTION,
    }
    assert outdated(now, "medium") is None
    assert outdated(now, "large-v3") == "made with faster-whisper medium"
    assert outdated({**now, "timing": 1}, "medium") == "timed the old way"
    assert outdated(None, "medium")


def test_subtitles_transcribed_the_old_way_are_remade():
    from curatarr import subtitles, transcribe

    assert subtitles.TRANSCRIPTION == transcribe.RULE
    made = subtitles.made_with({"model": "turbo", "rule": transcribe.RULE}, "turbo")
    assert subtitles.outdated(made, "turbo") is None
    old = {**made, "transcription": None}  # made before this was kept
    assert subtitles.outdated(old, "turbo") == "transcribed the old way"


def test_a_transcript_made_the_old_way_is_made_again_for_subtitles(
    tmp_path, monkeypatch
):
    import json
    from types import SimpleNamespace

    from curatarr import transcribe

    info = SimpleNamespace(
        path="/tv/Show/S01E01.mkv",
        duration=1500.0,
        streams=[{"index": 1, "codec_type": "audio", "codec_name": "aac"}],
    )
    row = {"start": 1, "end": 2, "text": " Hi.", "words": [[1.1, 1.5, " Hi."]]}
    path = tmp_path / f"{transcribe.cache_key(info, 'turbo')}.json"
    path.write_text(json.dumps({"segments": [row], "details": {"language": "en"}}))
    monkeypatch.setattr(transcribe, "available", lambda: False)
    # good enough for chapters and search…
    _segments, details = transcribe.transcribe(info, "turbo", "cpu", tmp_path)
    assert details["cached"]
    # …but subtitles want it made the current way
    try:
        transcribe.transcribe(info, "turbo", "cpu", tmp_path, words=True)
        raise AssertionError("served the old transcript")
    except transcribe.TranscriptionUnavailable:
        pass
    details = {"language": "en", "rule": transcribe.RULE}
    path.write_text(json.dumps({"segments": [row], "details": details}))
    assert transcribe.transcribe(info, "turbo", "cpu", tmp_path, words=True)[1][
        "cached"
    ]


class FakeWhisper:
    def __init__(self, spoken, *_args, **_kwargs):
        self.spoken, self.calls = spoken, []

    def detect_language(self, _audio, vad_filter):
        assert vad_filter  # decided from the speech alone
        if self.spoken is None:
            raise ValueError("need at least one array to concatenate")
        return self.spoken, 0.9, []

    def transcribe(self, _audio, **options):
        from types import SimpleNamespace

        self.calls.append(options)
        word = SimpleNamespace(start=1.0, end=1.4, word=" Hi.")
        segment = SimpleNamespace(start=1.0, end=1.4, text=" Hi.", words=[word])
        return [segment], SimpleNamespace(language="en")


def test_the_whole_track_is_transcribed_in_the_language_spoken(monkeypatch):
    import sys
    from types import SimpleNamespace

    from curatarr import transcribe

    for spoken in ("en", "it", None):
        made = []

        def model(*_args, spoken=spoken, made=made, **_kwargs):
            made.append(FakeWhisper(spoken))
            return made[-1]

        fake = SimpleNamespace(WhisperModel=model, decode_audio=lambda _path: "pcm")
        monkeypatch.setitem(sys.modules, "faster_whisper", fake)
        rows, details = transcribe._run_model("audio.wav", "turbo", "cpu")
        options = made[0].calls[0]
        assert rows[0]["words"] == [[1.0, 1.4, " Hi."]]
        assert details["rule"] == transcribe.RULE
        if spoken:
            # nothing filtered out, in the language found, without inventing
            # words in long silences
            assert options["vad_filter"] is False and options["language"] == spoken
            assert options["hallucination_silence_threshold"]
            assert details["language"] == spoken
        else:  # nothing said: the filtered way finds nothing quickly
            assert options["vad_filter"] is True and "language" not in options


def test_a_slower_reading_speed_shows_lines_earlier_and_longer():
    from curatarr.subtitles import cues_from

    said = [
        Segment(10.0, 11.0, " Run.", ((10.0, 11.0, " Run."),)),
        Segment(
            20.0,
            21.5,
            " We have to leave before the storm.",
            tuple(
                (20.0 + 0.25 * i, 20.25 + 0.25 * i, f" {w}")
                for i, w in enumerate(
                    ["We", "have", "to", "leave", "before", "the", "storm."]
                )
            ),
        ),
        Segment(22.0, 23.0, " Now.", ((22.0, 23.0, " Now."),)),
    ]
    standard, slow = cues_from(said), cues_from(said, "slow")
    assert slow[0].start < standard[0].start < 10.0
    assert slow[0].end > standard[0].end
    # never into the next line, whatever the speed
    assert slow[1].end <= slow[2].start


def test_another_reading_speed_only_retimes():
    from curatarr import transcribe
    from curatarr.subtitles import made_with, only_retimed, outdated

    made = made_with({"model": "turbo", "rule": transcribe.RULE}, "turbo")
    assert outdated(made, "turbo") is None
    assert outdated(made, "turbo", "slow") == "timed for standard reading"
    assert only_retimed(made, "turbo", "slow")
    assert not only_retimed(made, "turbo")  # nothing to do
    assert not only_retimed(made, "medium", "slow")  # needs transcribing too


def test_a_new_reading_speed_retimes_files_in_a_batch(subtitle_env, monkeypatch):
    from curatarr import up_next

    _files, _arr, library, _media, part, _calls = subtitle_env
    monkeypatch.setattr(up_next, "part_ids", lambda users=None: [])
    enrichment.request(part, "subtitles")
    enrichment.dispatch(synchronous=True)
    assert _state(part).subtitle_made_with["reading"] == "standard"
    assert enrichment.remake_subtitles() == 0
    layers = {library.id: {"subtitles": {"reading": "slow"}}}
    set_setting("chapter_library_settings", layers)
    db.session.commit()
    assert enrichment.remake_subtitles() == 1  # quick: no waiting its turn
    enrichment.dispatch(synchronous=True)
    assert _state(part).subtitle_made_with["reading"] == "slow"
    assert enrichment.remake_subtitles() == 0
