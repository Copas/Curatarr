from types import SimpleNamespace

import numpy as np

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
    assert enrichment.recheck_credits() == 1  # one at a time…
    assert states[1].requested_action == "recap"  # …longest-checked first
    assert states[1].markers_checked_at is None
    assert enrichment.recheck_credits() == 0  # …and not while it's waiting
    monkeypatch.setattr(
        up_next, "part_ids", lambda users=None: [states[0].media_part_id]
    )
    assert enrichment.recheck_credits() == 1  # about to be watched: straight away
    for state in states:
        state.job_state, state.credits_rule = "idle", enrichment.CREDITS_RULE
        state.markers_checked_at = state.recap_checked_at = utcnow()
    db.session.commit()
    assert enrichment.recheck_credits() == 0  # checked the current way
    # a recap found by older rules: only the recap is checked again
    states[0].recap_source, states[0].recap_rule = "earlier episodes", 1
    states[1].recap_source = "no opener"  # nothing to find: left alone
    db.session.commit()
    assert enrichment.recheck_credits() == 1
    assert states[0].recap_checked_at is None
    assert states[0].markers_checked_at is not None
    states[0].job_state, states[0].recap_rule = "idle", enrichment.RECAP_RULE
    db.session.commit()
    assert enrichment.recheck_credits() == 0
