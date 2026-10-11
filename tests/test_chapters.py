"""Chapter planning rules (curatarr/chapters.py): pure, no files or network."""

import pytest

from curatarr.chapters import (
    END_MARGIN_SECONDS,
    Chapter,
    ChapterConfigError,
    InvalidChapters,
    Segment,
    Spacing,
    candidates_in,
    chapter_file_text,
    config_hash,
    effective_config,
    fingerprint,
    fixed_interval_plan,
    next_window,
    scene_aligned_plan,
    semantic_plan,
    timestamp,
    validate_chapters,
    validate_global_options,
    validate_layer,
)

CONFIG = effective_config({})
MINUTE = 60.0


def starts(plan):
    return [round(c.start, 3) for c in plan.chapters]


def test_defaults_match_the_specification():
    assert CONFIG["enabled"] is False
    assert CONFIG["minimum_duration_minutes"] == 20
    assert (
        CONFIG["minimum_spacing_minutes"],
        CONFIG["target_spacing_minutes"],
        CONFIG["maximum_spacing_minutes"],
    ) == (4, 7, 10)
    assert CONFIG["preserve_existing"] is True
    assert CONFIG["processing"] == {
        "new_media": True,
        "existing_library": False,
        "dry_run": False,
    }
    assert CONFIG["semantic"]["transcription"] == "faster-whisper"
    assert CONFIG["semantic"]["model"] == "medium"
    assert CONFIG["naming"]["fallback_format"] == "Chapter {number}"


def test_fixed_intervals_start_at_zero_and_stop_before_the_end():
    plan = fixed_interval_plan(60 * MINUTE, CONFIG)
    assert starts(plan) == [m * 7 * MINUTE for m in range(9)]
    assert [c.name for c in plan.chapters][:2] == ["Chapter 1", "Chapter 2"]
    # With a 57:30 end, the 56:00 target moves back to 55:30 to stay two
    # minutes clear of the end (still at least 4 minutes after 49:00).
    short = fixed_interval_plan(57.5 * MINUTE, CONFIG)
    assert starts(short)[-1] == 55.5 * MINUTE
    assert all(57.5 * MINUTE - s >= END_MARGIN_SECONDS for s in starts(short))


def test_no_boundary_fits_in_a_short_remainder():
    spacing = Spacing.from_config(CONFIG)
    # Earliest allowed is 4 min after the previous one, but the end margin is
    # reached first.
    assert next_window(0, 5 * MINUTE, spacing) is None
    window = next_window(0, 8 * MINUTE, spacing)
    assert window.earliest == 4 * MINUTE and window.latest == 6 * MINUTE
    assert window.target == 6 * MINUTE  # clamped into the allowed range


def test_scene_alignment_picks_the_nearest_change_within_spacing():
    scenes = [3 * MINUTE, 6.5 * MINUTE, 7.4 * MINUTE, 12 * MINUTE, 30 * MINUTE]
    plan = scene_aligned_plan(30 * MINUTE, scenes, CONFIG)
    # 7.4 is nearer 7:00 than 6.5; next target 14.4, nearest in [11.4,17.4] is 12;
    # next target 19, window [16, 22] has no scene change, so the target is used.
    assert starts(plan)[:4] == [0, 7.4 * MINUTE, 12 * MINUTE, 19 * MINUTE]
    assert any("no scene change" in note for note in plan.notes)
    gaps = [b - a for a, b in zip(starts(plan), starts(plan)[1:])]
    assert all(4 * MINUTE <= gap <= 10 * MINUTE for gap in gaps)


def test_scene_changes_outside_minimum_spacing_are_ignored():
    plan = scene_aligned_plan(30 * MINUTE, [1 * MINUTE, 2 * MINUTE], CONFIG)
    assert 1 * MINUTE not in starts(plan) and 2 * MINUTE not in starts(plan)


def test_validation_rejects_unsafe_chapter_lists():
    validate_chapters([Chapter(0, "A"), Chapter(60, "B")], 120)
    with pytest.raises(InvalidChapters):
        validate_chapters([Chapter(5, "A")], 120)  # must start at zero
    with pytest.raises(InvalidChapters):
        validate_chapters([Chapter(0, "A"), Chapter(60, "B"), Chapter(60, "C")], 120)
    with pytest.raises(InvalidChapters):
        validate_chapters([Chapter(0, "A"), Chapter(130, "B")], 120)
    with pytest.raises(InvalidChapters):
        validate_chapters([Chapter(0, "two\nlines")], 120)
    with pytest.raises(InvalidChapters):
        validate_chapters([], 120)


def test_chapter_file_uses_the_simple_mkvpropedit_format():
    text = chapter_file_text([Chapter(0, "Opening"), Chapter(434, "Chapter 2")])
    assert text == (
        "CHAPTER01=00:00:00.000\nCHAPTER01NAME=Opening\n"
        "CHAPTER02=00:07:14.000\nCHAPTER02NAME=Chapter 2\n"
    )
    assert timestamp(3723.4567) == "01:02:03.457"


def test_fingerprint_matches_chapters_read_back_from_a_file():
    written = [Chapter(0, "Opening"), Chapter(434.0004, "Next ")]
    read_back = [{"start": 0.0, "name": "Opening"}, {"start": 434.0, "name": "Next"}]
    assert fingerprint(written) == fingerprint(read_back)
    assert fingerprint(read_back) != fingerprint(read_back[:1])


def test_layers_inherit_and_validate():
    config = effective_config(
        {"enabled": True, "target_spacing_minutes": 8},
        {"mode": "navigation", "processing": {"dry_run": True}},
    )
    assert config["enabled"] and config["mode"] == "navigation"
    assert config["target_spacing_minutes"] == 8
    assert config["processing"] == {
        "new_media": True,
        "existing_library": False,
        "dry_run": True,
    }
    for bad in (
        {"mode": "always"},
        {"unknown": 1},
        {"semantic": {"model": "huge"}},
        {"minimum_spacing_minutes": 9},  # above the 7-minute target
        {"naming": {"fallback_format": "Part"}},
        {"naming": {"fallback_format": "Part {0}"}},
        {"processing": {"dry_run": "yes"}},
        {
            "mode": "navigation",
            "fallback": {"scene_detection": False, "fixed_intervals": False},
        },
    ):
        with pytest.raises(ChapterConfigError):
            validate_layer(bad)


def test_config_hash_tracks_settings_that_shape_chapters():
    base = config_hash(CONFIG)
    assert config_hash(effective_config({"processing": {"dry_run": True}})) == base
    assert config_hash(effective_config({"target_spacing_minutes": 8})) != base


def test_global_options_are_bounded():
    assert validate_global_options({})["concurrency"] == 1
    for bad in (
        {"concurrency": 0},
        {"concurrency": 9},
        {"scene_threshold": 2},
        {"whisper_device": "tpu"},
        {"ollama_url": "ftp://x"},
        {"path_mappings": [{"arr": "/tv"}]},
        {"surprise": True},
    ):
        with pytest.raises(ChapterConfigError):
            validate_global_options(bad)


def _speech(duration):
    """A segment every 20 seconds, with a 5-second pause at each 6:50."""
    segments, t = [], 0.0
    while t < duration:
        length = 15.0 if int(t) % 410 else 10.0
        segments.append(Segment(t, t + length, f"words at {int(t)}"))
        t += 20.0
    return segments


def test_candidates_merge_scene_changes_inside_pauses():
    segments = [Segment(0, 400, "a"), Segment(405, 500, "b")]
    window = next_window(0, 60 * MINUTE, Spacing.from_config(CONFIG))
    found = candidates_in(window, [402.0, 300.0], segments)
    merged = [c for c in found if c.time == 402.0]
    assert merged and merged[0].scene and merged[0].pause
    assert any(c.time == 300.0 and c.scene and not c.pause for c in found)


def test_semantic_plan_uses_model_choices_and_names():
    duration = 30 * MINUTE
    scenes = [6.8 * MINUTE, 7.5 * MINUTE, 14 * MINUTE, 21 * MINUTE, 22 * MINUTE]
    calls = []

    def choose(window, candidates, excerpt):
        calls.append((window, [c.time for c in candidates], excerpt))
        return 0  # always the earliest candidate offered

    def name(excerpts):
        return ["Opening", "The Plan", "Into the Night", "Aftermath", "Extra"]

    plan = semantic_plan(duration, scenes, _speech(duration), CONFIG, choose, name)
    assert plan.method == "semantic"
    assert plan.chapters[0].start == 0 and plan.chapters[0].name == "Opening"
    assert len(calls) == len(plan.chapters) - 1
    for window, offered, excerpt in calls:
        assert all(window.earliest <= t <= window.latest for t in offered)
        assert excerpt  # only nearby transcript is sent
    validate_chapters(plan.chapters, duration)


def test_semantic_plan_falls_back_to_numbered_names_when_naming_fails():
    duration = 30 * MINUTE

    plan = semantic_plan(
        duration,
        [7 * MINUTE, 14 * MINUTE, 21 * MINUTE],
        _speech(duration),
        CONFIG,
        lambda *_: 0,
        lambda _excerpts: ["", "   ", "x" * 200],
    )
    assert [c.name for c in plan.chapters][:3] == [
        "Chapter 1",
        "Chapter 2",
        "Chapter 3",
    ]


def test_semantic_plan_needs_speech_and_a_model_decision():
    with pytest.raises(InvalidChapters):
        semantic_plan(30 * MINUTE, [], [], CONFIG, lambda *_: 0, lambda _e: [])
    with pytest.raises(InvalidChapters):
        semantic_plan(
            30 * MINUTE,
            [7 * MINUTE, 14 * MINUTE, 21 * MINUTE],
            _speech(30 * MINUTE),
            CONFIG,
            lambda *_: None,
            lambda _e: [],
        )
