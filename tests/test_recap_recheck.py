"""Recaps whose end couldn't be placed yet are checked again later."""

from curatarr import db, enrichment, recap_recheck
from curatarr.chapters import Segment
from curatarr.models import ChapterState, Library, MediaIdentity, MediaPart
from curatarr.services import setting

OPENS_WITH_RECAP = [
    Segment(2.0, 4.0, "Previously on Show..."),
    Segment(4.0, 7.0, "We have to find the way out of this town."),
]
NO_RECAP = [Segment(2.0, 5.0, "Morning. Did you sleep at all last night?")]


def _series(episodes):
    """A show whose episodes are {number: (recap_source, transcript or None)}."""
    library = Library(jellyfin_library_id="tv", name="TV", media_type="tv")
    db.session.add(library)
    db.session.flush()
    media = MediaIdentity(library_id=library.id, media_type="series", title="Show")
    db.session.add(media)
    db.session.flush()
    states = {}
    for number, (source, transcript) in episodes.items():
        part = MediaPart(
            media_identity_id=media.id,
            kind="episode",
            season_number=1,
            episode_number=number,
            has_file=True,
            file_path=f"/tv/Show/S01E{number:02}.mkv",
        )
        db.session.add(part)
        db.session.flush()
        state = ChapterState(
            media_part_id=part.id,
            media_identity_id=media.id,
            recap_source=source,
            transcript_key=f"t{number}" if transcript is not None else None,
        )
        db.session.add(state)
        states[number] = state
    db.session.commit()
    return states, {f"t{n}": t for n, (_s, t) in episodes.items() if t is not None}


def test_episodes_said_to_have_no_recap_that_open_with_one_are_checked_again(
    app, monkeypatch
):
    states, transcripts = _series(
        {
            1: (None, NO_RECAP),
            2: ("none", OPENS_WITH_RECAP),  # missed: checked again
            3: ("none", NO_RECAP),  # really none
            4: ("rechecked", OPENS_WITH_RECAP),
        }
    )
    monkeypatch.setattr(enrichment, "_transcript", transcripts.get)
    assert recap_recheck.sweep() == 2
    db.session.expire_all()
    queued = {n for n, s in states.items() if s.requested_action == "recap"}
    assert queued == {2, 4}
    assert all(states[n].recap_checked_at is None for n in queued)
    assert setting(recap_recheck.KEY)["finished_at"]
    for state in states.values():
        state.job_state, state.requested_action = "idle", None
    db.session.commit()
    assert recap_recheck.sweep() == 0  # once


def test_an_unclear_recap_waits_for_more_earlier_episodes(app):
    states, _transcripts = _series(
        {1: (None, NO_RECAP), 2: ("unclear:0", OPENS_WITH_RECAP)}
    )
    assert recap_recheck.earlier_transcribed(states[2], states[2].part) == 1
    assert recap_recheck._recheck_unclear() == 1  # one more transcribed since
    db.session.expire_all()
    assert states[2].requested_action == "recap"
    states[2].job_state, states[2].recap_source = "idle", "unclear:1"
    db.session.commit()
    assert recap_recheck._recheck_unclear() == 0  # nothing new to match against
    assert recap_recheck.unclear_source(states[2], states[2].part) == "unclear:1"
