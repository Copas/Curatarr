from types import SimpleNamespace

import numpy as np

from curatarr import credits, footage
from curatarr.enrichment import INTRO_RULE, _place_intro


def _print(seconds, seed):
    """A fingerprint of ``seconds`` of varied pictures, and no sound."""
    rng = np.random.default_rng(seed)
    thumbs = rng.integers(0, 255, (seconds, footage.H, footage.W), dtype=np.uint8)
    hashes, flat = footage.picture_hashes(thumbs)
    return footage.Print(hashes, flat, None), thumbs


def test_the_intro_is_the_footage_the_season_shares():
    titles = np.random.default_rng(99).integers(
        0, 255, (40, footage.H, footage.W), dtype=np.uint8
    )
    episodes = []
    for n, at in enumerate((60, 75, 50, 90, 65)):
        _unused, thumbs = _print(1200, n)
        thumbs[at : at + 40] = titles  # the same titles, a different place
        thumbs[5:9] = titles[:4]  # a 4-second logo: too short to be the intro
        hashes, flat = footage.picture_hashes(thumbs)
        episodes.append(footage.Print(hashes, flat, None))
    assert footage.intro_block(episodes[0], episodes[1:], 1200.0) == (60, 100)
    assert footage.intro_block(episodes[0], episodes[1:3], 1200.0) is None  # too few


def test_the_intro_is_the_shared_stretch_by_intro_skippers_not_a_longer_one():
    rng = np.random.default_rng(7)
    titles = rng.integers(0, 255, (30, footage.H, footage.W), dtype=np.uint8)
    studio = rng.integers(0, 255, (60, footage.H, footage.W), dtype=np.uint8)
    episodes = []
    for n in range(5):
        _unused, thumbs = _print(1200, 20 + n)
        thumbs[0:30] = titles  # the title sequence
        thumbs[100:160] = studio  # a longer shared studio opening, later
        hashes, flat = footage.picture_hashes(thumbs)
        episodes.append(footage.Print(hashes, flat, None))
    assert footage.intro_block(episodes[0], episodes[1:], 1200.0) == (100, 160)
    assert footage.intro_block(episodes[0], episodes[1:], 1200.0, (0, 22)) == (0, 30)
    assert footage.intro_block(episodes[0], episodes[1:], 1200.0, (300, 320)) is None


def test_blank_pictures_are_no_evidence():
    thumbs = np.zeros((10, footage.H, footage.W), np.uint8)
    _hashes, flat = footage.picture_hashes(thumbs)
    assert flat.all()


def test_intro_skippers_intro_only_ever_widens():
    assert footage.widened((40.0, 68.0), (37.0, 72.0)) == (37.0, 72.0)
    # an edge further out than WIDEN, or further in, stays Intro Skipper's
    assert footage.widened((213.0, 276.0), (211.0, 287.0)) == (211.0, 276.0)
    assert footage.widened((0.0, 24.0), (3.0, 28.0)) == (0.0, 28.0)
    assert footage.widened((0.0, 30.0), (2.0, 30.0)) is None
    # under a second further out isn't worth a change
    assert footage.widened((78.7, 108.1), (78.0, 108.1)) is None
    assert footage.widened((78.7, 108.1), (77.7, 108.5)) == (77.7, 108.1)
    assert footage.widened((0.0, 30.0), (300.0, 330.0)) is None  # elsewhere
    assert footage.snap(100.0, [(97.4, "cut"), (101.0, "black")]) == 101.0
    assert footage.snap(100.0, [(90.0, "cut")]) == 100.0


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


def _ep(block, rule=None):
    return SimpleNamespace(
        info=SimpleNamespace(duration=2600.0, path="file"),
        state=SimpleNamespace(id="state", intro_rule=rule),
        intro_block=lambda near=None: block,
    )


def test_intro_skippers_intro_is_replaced_by_a_wider_one(monkeypatch):
    monkeypatch.setattr(credits, "frames", lambda *_a: np.zeros((1, 1, 1)))
    monkeypatch.setattr(credits, "find_breaks", lambda _p, start: [])
    theirs = {"Id": "theirs", "Start": 40.0, "End": 68.0, "Source": "Chromaprint"}
    plugin, hidden = FakePlugin(), []
    assert _place_intro(_ep((37, 72)), plugin, "item", [theirs], hidden)
    assert plugin.added == [("Introduction", 37, 72)] and plugin.hidden == ["theirs"]
    assert hidden[0]["replaced_by"] == "ours-1" and hidden[0]["type"] == "Introduction"
    # checked again by the same rules: nothing to do
    assert (
        _place_intro(_ep((37, 72), INTRO_RULE), FakePlugin(), "item", [], hidden)
        is None
    )
    # by older rules: put back, then widened again
    again = FakePlugin()
    assert _place_intro(_ep((37, 72), 0), again, "item", [], hidden)
    assert again.restored == ["theirs"] and again.added == [("Introduction", 37, 72)]
    assert len(hidden) == 1
    # nothing shared, no intro, or two intros: left alone
    for block, intros in (
        (None, [theirs]),
        ((37, 72), []),
        ((37, 72), [theirs, {**theirs, "Id": "other"}]),
    ):
        plugin = FakePlugin()
        assert _place_intro(_ep(block), plugin, "item", intros, []) is None
        assert plugin.added == []
