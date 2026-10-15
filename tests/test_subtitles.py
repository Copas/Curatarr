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

    def fake_transcribe(info, model, device, cache):
        calls.append(info.path)
        return [Segment(1, 3, "Hello."), Segment(4, 6, "Goodbye.")], {
            "language": "en",
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
    assert _state(part).subtitle_status == "existing" and calls == []
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


def test_non_english_audio_is_skipped(subtitle_env, monkeypatch):
    _files, _arr, _library, _media, part, _calls = subtitle_env
    monkeypatch.setattr(
        "curatarr.transcribe.transcribe",
        lambda *_a: ([Segment(1, 2, "Hola.")], {"language": "es"}),
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
