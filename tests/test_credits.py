from datetime import timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from curatarr import credits
from curatarr.enrichment import CREDITS_RULE, _place_credits, undo_marker


def test_lettering_is_thin_bright_strokes_in_lines():
    pic = np.zeros((credits.TEXT_H, credits.TEXT_W), np.uint8)
    for row in range(100, 110):  # a line of lettering: one-pixel strokes
        pic[row, 40:440:4] = 230
    assert credits.text_score(pic) > credits.TEXT
    assert credits.text_score(np.full_like(pic, 230)) == 0  # a bright picture
    blob = np.zeros_like(pic)
    blob[50:200, 50:300] = 230  # a big bright shape: edges, no strokes
    assert credits.text_score(blob) < credits.TEXT


def test_the_credits_are_the_last_stretch_of_lettering_to_the_end():
    scores = [0] * 100 + [900] * 20 + [0] * 100 + [900] * 30 + [0] * 3 + [900] * 20
    start = 1000.0
    duration = start + len(scores)
    assert credits.last_run(scores, start, duration) == start + 220
    # a caption or two before isn't credits; a stretch that stops long before
    # the end isn't either
    assert credits.last_run([900] * 5 + [0] * 100, start, start + 105) is None
    assert credits.last_run([900] * 30 + [0] * 100, start, start + 130) is None


def test_a_start_moves_back_to_the_fade_before_it():
    found = [(96.0, "cut"), (97.4, "black"), (99.5, "cut")]
    assert credits.snap(100.0, found) == (97.4, "black")
    assert credits.snap(100.0, [(99.5, "cut"), (95.0, "cut")]) == (99.5, "cut")
    assert credits.snap(100.0, [(80.0, "black")]) == (100.0, "")


def test_breaks_are_found_in_the_picture():
    pics = np.full((30, credits.BREAK_H, credits.BREAK_W), 120, np.uint8)
    pics[10:15] = 0  # a fade to black at 1 s
    pics[20:] = 20  # a hard cut to a darker shot at 2 s
    assert credits.find_breaks(pics, 50.0) == [
        (51.0, "black"),
        (51.5, "cut"),
        (52.0, "cut"),
    ]


def test_runs_of_black_are_measured():
    pics = np.full((40, credits.BREAK_H, credits.BREAK_W), 90, np.uint8)
    pics[5:8] = 4  # a short flash of black
    pics[20:35] = 15  # a dim fade: black at a looser level only
    assert credits.blacks(pics, 10.0) == [(10.5, 0.3)]
    assert credits.blacks(pics, 10.0, level=20) == [(10.5, 0.3), (12.0, 1.5)]


def test_the_earlier_start_wins(monkeypatch):
    monkeypatch.setattr(credits, "snapped", lambda _path, t: (t - 2, "black"))
    monkeypatch.setattr(credits, "lettering_start", lambda _path, _d: 2400.0)
    found = credits.credits_start("file", 2500.0, guess=2410.0)
    assert found.start == 2398.0 and "lettering" in found.how
    found = credits.credits_start("file", 2500.0, guess=2390.0)
    assert found.start == 2388.0 and "Intro Skipper" in found.how


class FakePlugin:
    def __init__(self):
        self.added, self.hidden, self.restored = [], [], []

    def add(self, item_id, kind, start, end):
        self.added.append((kind, start, end))
        return f"ours-{len(self.added)}"

    def hide(self, item_id, segment_id):
        self.hidden.append(segment_id)

    def restore(self, item_id, segment_id):
        self.restored.append(segment_id)


def _ep(duration=2600.0, rule=None):
    return SimpleNamespace(
        info=SimpleNamespace(duration=duration, path="file"),
        state=SimpleNamespace(id="state", credits_rule=rule),
    )


def test_intro_skippers_late_credits_are_replaced(monkeypatch):
    monkeypatch.setattr(
        credits,
        "credits_start",
        lambda _p, _d, guess: credits.Found(
            guess - 3, "Intro Skipper's, moved to a black"
        ),
    )
    plugin, hidden = FakePlugin(), []
    segment = {"Id": "theirs", "Start": 2553.0, "End": 2600.0, "Source": "Chromaprint"}
    assert _place_credits(_ep(), plugin, "item", [segment], hidden) == "credits moved"
    assert plugin.added == [("Credits", 2550.0, 2600.0)]
    assert plugin.hidden == ["theirs"]
    assert hidden[0]["replaced_by"] == "ours-1" and hidden[0]["new_start"] == 2550.0
    # checked again by the same rules: Curatarr's marker is already there
    current = _ep(rule=CREDITS_RULE)
    assert _place_credits(current, FakePlugin(), "item", [], hidden) is None
    # by older rules: what was there is put back and placed again
    again = FakePlugin()
    assert _place_credits(_ep(rule=1), again, "item", [], hidden) == "credits moved"
    assert again.hidden == ["ours-1", "theirs"] and again.restored == ["theirs"]
    assert again.added == [("Credits", 2550.0, 2600.0)]
    assert len(hidden) == 1 and hidden[0]["placed"]


def test_credits_intro_skipper_missed_are_added_and_can_be_removed(app, monkeypatch):
    monkeypatch.setattr(
        credits,
        "credits_start",
        lambda _p, _d, guess: credits.Found(2560.0, "credit lettering"),
    )
    plugin, hidden = FakePlugin(), []
    assert _place_credits(_ep(), plugin, "item", [], hidden) == "credits added"
    assert plugin.added == [("Credits", 2560.0, 2600.0)]
    assert hidden[0]["added"] and hidden[0]["id"] == "ours-1"

    monkeypatch.setattr("curatarr.enrichment.client", lambda _kind: None)
    monkeypatch.setattr("curatarr.integrations.IntroSkipper", lambda _j: plugin)
    monkeypatch.setattr("curatarr.enrichment.jellyfin_item_id", lambda *_a: "item")
    monkeypatch.setattr("curatarr.enrichment.audit", lambda *_a, **_k: None)
    monkeypatch.setattr("curatarr.enrichment._part_label", lambda _p: "S01E01")
    state = SimpleNamespace(
        id="state",
        part=None,
        media=SimpleNamespace(id="m"),
        markers_hidden=list(hidden),
    )
    assert "Removed" in undo_marker(state, "hidden", "ours-1")
    assert plugin.hidden == ["ours-1"] and plugin.restored == []
    assert state.markers_hidden is None


def test_credits_are_left_alone_when_unsure(monkeypatch):
    plugin = FakePlugin()
    segment = {"Id": "theirs", "Start": 2553.0, "End": 2600.0}
    for found in (
        None,
        credits.Found(2552.5, "same place"),  # under a second away
        credits.Found(2300.0, "far earlier"),  # more than 3 minutes earlier
        credits.Found(1000.0, "mid-episode"),
    ):
        monkeypatch.setattr(credits, "credits_start", lambda _p, _d, _g, f=found: f)
        assert _place_credits(_ep(), plugin, "item", [segment], []) is None
    # two credits markers near the end: the other checks' business
    late = {"Id": "other", "Start": 2580.0, "End": 2600.0}
    assert _place_credits(_ep(), plugin, "item", [segment, late], []) is None
    assert plugin.added == []


def test_episodes_checked_under_older_rules_are_checked_again(app, monkeypatch):
    from datetime import timedelta

    from curatarr import db, enrichment, up_next
    from curatarr.models import ChapterState, Library, MediaIdentity, MediaPart, utcnow

    monkeypatch.setattr(up_next, "part_ids", lambda users=None: [])
    library = Library(jellyfin_library_id="tv", name="TV", media_type="tv")
    db.session.add(library)
    db.session.flush()
    show = MediaIdentity(library_id=library.id, media_type="series", title="Show")
    db.session.add(show)
    db.session.flush()
    states = []
    for number in (1, 2):
        part = MediaPart(
            media_identity_id=show.id,
            kind="episode",
            season_number=1,
            episode_number=number,
            has_file=True,
            file_path=f"/tv/Show/S01E0{number}.mkv",
        )
        db.session.add(part)
        db.session.flush()
        state = ChapterState(media_part_id=part.id, media_identity_id=show.id)
        state.job_state = "idle"
        state.markers_checked_at = utcnow() - timedelta(days=number)
        db.session.add(state)
        states.append(state)
    db.session.commit()
    monkeypatch.setattr(enrichment, "RECHECK_QUEUED", 1)
    busy = ChapterState(media_part_id="busy", media_identity_id=show.id)
    busy.job_state, busy.requested_action = "queued", "auto"
    db.session.add(busy)
    db.session.commit()
    # chapter work waiting doesn't hold them back: a few kept queued…
    assert enrichment.recheck_credits() == 1
    assert states[1].requested_action == "recap"  # …longest-checked first
    assert states[1].markers_checked_at is None
    assert enrichment.recheck_credits() == 0  # …and no more while that waits
    monkeypatch.setattr(
        up_next, "part_ids", lambda users=None: [states[0].media_part_id]
    )
    assert enrichment.recheck_credits() == 1  # about to be watched: straight away
    db.session.delete(busy)
    for state in states:
        state.job_state, state.credits_rule = "idle", enrichment.CREDITS_RULE
        state.intro_rule = enrichment.INTRO_RULE
        state.markers_checked_at = state.recap_checked_at = utcnow()
        state.requested_at = None  # some hours later
    db.session.commit()
    assert enrichment.recheck_credits() == 0  # checked the current way
    # a recap found by older rules: only the recap is checked again
    states[0].recap_source, states[0].recap_rule = "earlier episodes", 1
    states[1].recap_source = "no opener"  # no transcript to look in: left alone
    db.session.commit()
    assert enrichment.recheck_credits() == 1
    assert states[0].recap_checked_at is None
    assert states[0].markers_checked_at is not None
    states[0].job_state, states[0].recap_rule = "idle", enrichment.RECAP_RULE
    db.session.commit()
    assert enrichment.recheck_credits() == 0
    # None found by older rules, with a saved transcript: a montage nobody
    # announces may be there (recaps.find_montage), so it's looked at again.
    states[1].transcript_key = "saved"
    db.session.commit()
    assert enrichment.recheck_credits() == 1
    assert states[1].recap_checked_at is None
    assert states[1].markers_checked_at is not None


def test_a_marker_already_gone_counts_as_hidden():
    from curatarr.integrations import IntegrationError, IntroSkipper

    class Jellyfin:
        def request(self, method, path, **_kwargs):
            raise IntegrationError("not found", status_code=404)

    IntroSkipper(Jellyfin()).hide("item", "segment")  # no error: nothing to undo

    class Down:
        def request(self, method, path, **_kwargs):
            raise IntegrationError("unavailable", status_code=503)

    with pytest.raises(IntegrationError):
        IntroSkipper(Down()).hide("item", "segment")


def test_an_episode_that_cant_be_rechecked_doesnt_hold_up_the_rest(app, monkeypatch):
    """The second half of a two-episode file has no Jellyfin item: it used to
    stay on the old rules and be picked every cycle, and while anything was
    up next the rest of the library wasn't rechecked at all."""
    from curatarr import db, enrichment, up_next
    from curatarr.models import ChapterState, Library, MediaIdentity, MediaPart, utcnow

    library = Library(jellyfin_library_id="tv", name="TV", media_type="tv")
    db.session.add(library)
    db.session.flush()
    show = MediaIdentity(library_id=library.id, media_type="series", title="Show")
    db.session.add(show)
    db.session.flush()
    states = []
    for number in (1, 2, 3):
        part = MediaPart(
            media_identity_id=show.id,
            kind="episode",
            season_number=1,
            episode_number=number,
            has_file=True,
        )
        db.session.add(part)
        db.session.flush()
        state = ChapterState(media_part_id=part.id, media_identity_id=show.id)
        state.job_state = "idle"
        state.markers_checked_at = utcnow() - timedelta(days=number)
        db.session.add(state)
        states.append(state)
    db.session.commit()
    monkeypatch.setattr(enrichment, "RECHECK_QUEUED", 2)
    monkeypatch.setattr(
        up_next, "part_ids", lambda users=None: [states[0].media_part_id]
    )
    # up next, and the oldest of the rest beside it
    assert enrichment.recheck_credits() == 2
    assert {s.requested_action for s in states[::2]} == {"recap"}
    # the up-next one comes back unchanged (no Jellyfin item): not again soon
    states[0].job_state, states[0].requested_action = "idle", None
    states[2].job_state, states[2].requested_action = "idle", None
    db.session.commit()
    assert enrichment.recheck_credits() == 1  # …so the remaining one gets a turn
    assert states[1].requested_action == "recap"
    assert states[0].requested_action is None


def test_no_jellyfin_item_counts_as_checked(app):
    from types import SimpleNamespace

    from curatarr import enrichment

    state = SimpleNamespace(intro_rule=None)
    ep = SimpleNamespace(
        state=state,
        part=SimpleNamespace(jellyfin_id=None, kind="episode"),
        media=SimpleNamespace(jellyfin_id="show"),
    )
    assert enrichment._check_markers(ep) == "markers: no Jellyfin item"
    assert state.intro_rule == enrichment.INTRO_RULE


def test_a_name_card_on_black_counts_as_credits():
    pic = np.zeros((credits.TEXT_H, credits.TEXT_W), np.uint8)
    for row in range(130, 136):  # one small name, centred
        pic[row, 190:290:4] = 230
    assert credits.text_score(pic) < credits.TEXT  # too few letters alone…
    assert credits.credit_score(pic) == credits.TEXT  # …but black all round
    scene = pic.copy()
    scene[:, :] = np.maximum(scene, 40)  # the same strokes in a dim scene
    scene[130:136, 190:290:4] = 230
    assert credits.credit_score(scene) < credits.TEXT
    assert credits.credit_score(np.zeros_like(pic)) == 0  # plain black


def test_recaps_intro_skipper_guessed_again_are_hidden_again(monkeypatch):
    from curatarr import enrichment
    from curatarr.chapters import Segment

    marked = []

    def mark(state, part, media, kind, *, clear_guesses=False):
        marked.append((kind, clear_guesses))
        if clear_guesses:
            state.markers_hidden = [*(state.markers_hidden or []), {"id": "new"}]

    monkeypatch.setattr(enrichment, "_mark_segment", mark)

    def episode(source, said=None, end=None):
        state = SimpleNamespace(recap_source=source, recap_end=end, markers_hidden=None)
        return SimpleNamespace(
            state=state,
            part=None,
            media=None,
            span=lambda *_a, **_k: said,
        )

    cold_open = [Segment(0, 5, "Who took the last donut?")]
    previously = [Segment(0, 3, "Previously on Show...")]
    # no "previously on": the plugin's new guess goes
    ep = episode("no opener")
    assert enrichment._settle_recap_guesses(ep) == "1 guessed recap hidden"
    assert marked[-1] == ("Recap", True)
    # recorded by a chapter job as none: the saved transcript decides
    ep = episode("none", said=cold_open)
    assert enrichment._settle_recap_guesses(ep)
    assert ep.state.recap_source == "no opener"
    marked.clear()
    assert enrichment._settle_recap_guesses(episode("none", said=previously)) is None
    assert enrichment._settle_recap_guesses(episode("none", said=None)) is None
    assert enrichment._settle_recap_guesses(episode("unclear:3")) is None
    assert marked == []
    # Curatarr's own recap: put back in place of any guess
    assert enrichment._settle_recap_guesses(episode("closing line", end=80.0)) is None
    assert marked == [("Recap", False)]


def test_credits_pictures_are_not_read_again_for_an_intro_recheck(app, monkeypatch):
    from types import SimpleNamespace

    from curatarr import enrichment, integrations

    class Plugin:
        def __init__(self, _client):
            pass

        def segments(self, _item_id):
            return []

    read = []
    monkeypatch.setattr(enrichment, "jellyfin_item_id", lambda _p, _m: "item")
    monkeypatch.setattr(enrichment, "client", lambda _name: None)
    monkeypatch.setattr(integrations, "IntroSkipper", Plugin)
    monkeypatch.setattr(
        enrichment, "_place_credits", lambda *_a: read.append(1) or "credits added"
    )
    monkeypatch.setattr(enrichment, "_place_intro", lambda *_a: None)
    monkeypatch.setattr(enrichment, "_settle_recap_guesses", lambda _ep: None)

    def episode(credits_rule):
        state = SimpleNamespace(
            id="state",
            markers_hidden=None,
            preview_start=None,
            preview_end=None,
            credits_rule=credits_rule,
            intro_rule=1,
        )
        return SimpleNamespace(
            state=state, part=None, media=None, info=SimpleNamespace(duration=2600.0)
        )

    # only the intro rules changed: the credits' half minute of pictures is
    # skipped…
    assert enrichment._check_markers(episode(enrichment.CREDITS_RULE)) == "markers: ok"
    assert read == []
    # …but read when the credits rules changed, or it was never checked
    ep = episode(None)
    assert enrichment._check_markers(ep) == "markers: credits added"
    assert read == [1] and ep.state.credits_rule == enrichment.CREDITS_RULE
    # a new copy of the file has its credits looked for again
    enrichment._forget_old_file(ep.state)
    assert ep.state.credits_rule is None


def test_a_marker_curatarr_placed_that_jellyfin_no_longer_shows_is_placed_again():
    """A redo that gave up hid Curatarr's marker and couldn't put Intro
    Skipper's back: the episode shows neither, so it's placed again."""
    from curatarr.enrichment import _lost_markers

    hidden = [
        {"id": "is-intro", "type": "Introduction", "placed": True, "replaced_by": "ours-1"},
        {"id": "ours-2", "type": "Credits", "placed": True, "added": True},
    ]  # fmt: skip
    showing = [
        {"Id": "ours-1", "Type": "Introduction", "Source": "User"},
        {"Id": "ours-2", "Type": "Credits", "Source": "User"},
    ]
    assert _lost_markers(hidden, showing) == (False, False)
    assert _lost_markers(hidden, showing[1:]) == (True, False)
    suppressed = [{**showing[0]}, {**showing[1], "Suppressed": True}]
    assert _lost_markers(hidden, suppressed) == (False, True)
    assert _lost_markers(None, []) == (False, False)


def test_an_episode_about_to_be_watched_isnt_left_resting(app, monkeypatch):
    """FROM S4E8 and E9 were checked again for one rule in the evening; under
    the next rule they sat out the six-hour rest while being watched. Up
    next, the rest is a quarter of an hour."""
    from datetime import timedelta

    from curatarr import db, enrichment, up_next
    from curatarr.models import ChapterState, Library, MediaIdentity, MediaPart, utcnow

    library = Library(jellyfin_library_id="tv", name="TV", media_type="tv")
    db.session.add(library)
    db.session.flush()
    show = MediaIdentity(library_id=library.id, media_type="series", title="Show")
    db.session.add(show)
    db.session.flush()
    states = []
    for number in (8, 9):
        part = MediaPart(
            media_identity_id=show.id, kind="episode", season_number=4,
            episode_number=number, has_file=True, file_path=f"/tv/Show/S04E0{number}.mkv",
        )  # fmt: skip
        db.session.add(part)
        db.session.flush()
        state = ChapterState(media_part_id=part.id, media_identity_id=show.id)
        state.job_state = "idle"
        state.markers_checked_at = utcnow()
        state.credits_rule, state.intro_rule = (
            enrichment.CREDITS_RULE,
            enrichment.INTRO_RULE,
        )
        state.recap_source, state.recap_rule = (
            "earlier episodes",
            enrichment.RECAP_RULE - 1,
        )
        state.requested_at = utcnow() - timedelta(hours=2)  # an earlier rule's recheck
        db.session.add(state)
        states.append(state)
    db.session.commit()
    monkeypatch.setattr(enrichment, "RECHECK_QUEUED", 0)  # the library waits
    monkeypatch.setattr(up_next, "part_ids", lambda users=None: [])
    assert enrichment.recheck_credits() == 0  # resting
    monkeypatch.setattr(
        up_next, "part_ids", lambda users=None: [states[1].media_part_id]
    )
    assert enrichment.recheck_credits() == 1  # up next: not left resting
    assert states[1].requested_action == "recap" and states[0].requested_action is None


def test_redoing_credits_frees_the_database_before_reading_the_pictures(
    app, monkeypatch
):
    """Hiding the old marker started a write that was held while the credits'
    pictures were read (a minute or more): every other job waited on it."""
    from conftest import database_free

    from curatarr import db
    from curatarr.models import MediaSegment
    from curatarr.segments import SegmentStore

    for sid, start, hidden_now in (("ours-1", 2550.0, False), ("theirs", 2553.0, True)):
        db.session.add(
            MediaSegment(
                id=sid,
                item_id="item",
                kind="Credits",
                start=start,
                end=2600.0,
                hidden=hidden_now,
            )
        )
    db.session.commit()
    free = []

    def reading(_p, _d, guess):
        free.append(database_free(app))
        return credits.Found(guess - 3, "moved to a black")

    monkeypatch.setattr(credits, "credits_start", reading)
    hidden = [
        {"id": "theirs", "type": "Credits", "start": 2553.0, "end": 2600.0}
        | {"replaced_by": "ours-1", "placed": True}
    ]
    store = SegmentStore(forward=False)
    assert _place_credits(_ep(rule=1), store, "item", [], hidden) == "credits moved"
    assert free == [True]
    db.session.commit()
    assert db.session.get(MediaSegment, "ours-1").hidden
