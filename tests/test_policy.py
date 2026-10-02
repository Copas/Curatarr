from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from curatarr.policy import (
    CandidateInput,
    acquisition_seasons,
    effective_policy,
    eligible,
    minimum_satisfied,
    rank,
    reclaimable_bytes,
    select_to_low_water,
    validate_policy,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def item(name, days, *, watched=None, favorite=False, size=100):
    return CandidateInput(
        name,
        "movie",
        NOW - timedelta(days=days),
        NOW - timedelta(days=watched) if watched else None,
        size,
        favorite=favorite,
    )


def test_inheritance_and_invalid_quota():
    values, sources = effective_policy(
        "movie",
        {"grace_days": 10},
        {"grace_days": 20},
        {"grace_days": 3, "never_purge": True},
    )
    assert values["grace_days"] == 3 and sources["grace_days"] == "title"
    assert values["never_purge"] is True
    with pytest.raises(ValueError):
        validate_policy(
            {"quota_enabled": True, "high_water_bytes": 100, "low_water_bytes": 100}
        )


def test_acquisition_never_skips_two_seasons():
    parts = [SimpleNamespace(kind="episode", season_number=s) for s in (0, 1, 2, 4)]
    policy, _ = effective_policy("series")
    assert acquisition_seasons(parts, 1, policy) == [1, 2]
    assert acquisition_seasons(parts, 2, policy) == [2, 4]
    assert acquisition_seasons(parts, 0, policy) == []


def test_footprint_and_grace():
    policy, _ = effective_policy("series")
    parts = [
        SimpleNamespace(
            kind="episode",
            season_number=1,
            episode_number=n,
            has_file=True,
            size_bytes=100,
        )
        for n in range(1, 6)
    ]
    assert reclaimable_bytes(parts, policy) == 200
    assert minimum_satisfied(parts, policy)
    parts[1].has_file = False
    assert not minimum_satisfied(parts, policy)
    parts[1].has_file = True
    assert not eligible(item("new", 2), policy, NOW)
    assert eligible(item("old", 40), policy, NOW)
    assert not eligible(
        item("protected", 40).__class__(
            "protected", "movie", NOW - timedelta(days=40), None, 100, never_purge=True
        ),
        policy,
        NOW,
    )
    active_show = CandidateInput(
        "active-show",
        "series",
        NOW - timedelta(days=200),
        NOW - timedelta(days=3),
        100,
    )
    assert not eligible(active_show, policy, NOW)


def test_rankings_and_watermarks():
    movies = [
        item("fav", 200, watched=150, favorite=True),
        item("old_watched", 200, watched=100),
        item("new_watched", 200, watched=10),
        item("unwatched", 200),
    ]
    assert [
        x.media_id for x in rank(movies, "oldest_watched_nonfavorite_first", NOW)
    ] == ["old_watched", "new_watched", "unwatched", "fav"]
    assert rank(movies, "oldest_unwatched_first", NOW)[0].media_id == "unwatched"
    selected, deficit = select_to_low_water(movies, 300, 200, 100)
    assert len(selected) == 2 and deficit == 0
    assert select_to_low_water(movies, 200, 200, 100) == ([], 0)
