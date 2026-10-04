"""Pure, deterministic lifecycle policy calculations."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULTS: dict[str, Any] = {
    "acquisition_threshold": 1,
    "meaningful_threshold": 2,
    "tv_inactivity_days": 90,
    "movie_inactivity_days": 90,
    "grace_days": 30,
    "minimum_mode": "first_n_episodes",
    "minimum_episodes": 3,
    "keep_one_season_ahead": True,
    "manage_specials": False,
    "review_mode": "require_review",
    "review_expiry": "manual_forever",
    "notice_days": 14,
    "inactivity_review_mode": None,
    "quota_review_mode": None,
    "quota_enabled": False,
    "high_water_bytes": None,
    "low_water_bytes": None,
    "free_space_enabled": False,
    "disk_path": None,
    "low_free_percent": 15,
    "critical_free_percent": 8,
    "low_pressure_review_mode": None,
    "critical_pressure_review_mode": None,
    "never_purge": False,
    "dry_run": True,
}

BOUNDS = {
    "acquisition_threshold": (1, 100),
    "meaningful_threshold": (1, 100),
    "tv_inactivity_days": (1, 3650),
    "movie_inactivity_days": (1, 3650),
    "grace_days": (0, 3650),
    "minimum_episodes": (1, 100),
    "notice_days": (0, 365),
    "low_free_percent": (1, 99),
    "critical_free_percent": (1, 99),
}

ENUMS = {
    "minimum_mode": {"first_n_episodes", "season_1", "entire_series"},
    "review_mode": {"recommend", "require_review", "automatic"},
    "inactivity_review_mode": {"recommend", "require_review", "automatic"},
    "quota_review_mode": {"recommend", "require_review", "automatic"},
    "low_pressure_review_mode": {"recommend", "require_review", "automatic"},
    "critical_pressure_review_mode": {"recommend", "require_review", "automatic"},
    "review_expiry": {"manual_forever", "auto_delete_after_notice"},
    "purge_strategy": {
        "oldest_unwatched_first",
        "oldest_watched_nonfavorite_first",
        "weighted",
    },
}
BOOL_FIELDS = {
    "keep_one_season_ahead",
    "manage_specials",
    "quota_enabled",
    "free_space_enabled",
    "never_purge",
    "dry_run",
}


def validate_policy(values: dict[str, Any], *, complete: bool = True) -> dict[str, Any]:
    clean = {}
    for key, value in values.items():
        if key not in DEFAULTS and key != "purge_strategy":
            raise ValueError(f"Unknown policy field: {key}")
        if value is None:
            continue
        if key in BOUNDS:
            if type(value) is not int or not BOUNDS[key][0] <= value <= BOUNDS[key][1]:
                raise ValueError(f"Invalid {key}")
        elif key in ENUMS:
            if value not in ENUMS[key]:
                raise ValueError(f"Invalid {key}")
        elif key in BOOL_FIELDS:
            if type(value) is not bool:
                raise ValueError(f"Invalid {key}")
        elif key in {"high_water_bytes", "low_water_bytes"} and (
            type(value) is not int or value < 0
        ):
            raise ValueError(f"Invalid {key}")
        elif key == "disk_path" and (
            not isinstance(value, str) or not value.startswith("/")
        ):
            raise ValueError("Disk path must be an absolute arr-side path")
        clean[key] = value
    combined = DEFAULTS | clean
    if complete and combined["quota_enabled"]:
        high, low = combined["high_water_bytes"], combined["low_water_bytes"]
        if high is None or low is None or high <= low:
            raise ValueError("High-water bytes must exceed low-water bytes")
    if complete and combined["critical_free_percent"] >= combined["low_free_percent"]:
        raise ValueError("Critical free-space threshold must be below low threshold")
    if complete and combined["free_space_enabled"] and not combined["disk_path"]:
        raise ValueError("Disk path is required when free-space enforcement is enabled")
    return clean


def effective_policy(
    media_type: str, global_values=None, library_values=None, title_values=None
):
    values = DEFAULTS | {
        "purge_strategy": (
            "oldest_unwatched_first"
            if media_type == "series"
            else "oldest_watched_nonfavorite_first"
        )
    }
    sources = {key: "default" for key in values}
    for source, layer in (
        ("global", global_values or {}),
        ("library", library_values or {}),
        ("title", title_values or {}),
    ):
        for key, value in validate_policy(layer, complete=False).items():
            values[key] = value
            sources[key] = source
    validate_policy(values)
    return values, sources


def retained(part, policy: dict) -> bool:
    if part.season_number == 0 and not policy["manage_specials"]:
        return True
    if policy["minimum_mode"] == "entire_series":
        return True
    if policy["minimum_mode"] == "season_1":
        return part.season_number == 1
    return part.season_number == 1 and part.episode_number <= policy["minimum_episodes"]


def reclaimable_bytes(parts, policy: dict) -> int:
    return sum(
        part.size_bytes
        for part in parts
        if part.kind == "episode" and part.has_file and not retained(part, policy)
    )


def minimum_satisfied(parts, policy: dict) -> bool:
    if policy["minimum_mode"] == "entire_series":
        return True
    first_season = sorted(
        (part for part in parts if part.kind == "episode" and part.season_number == 1),
        key=lambda part: part.episode_number or 0,
    )
    if not first_season:
        return False
    if policy["minimum_mode"] == "season_1":
        return all(part.has_file for part in first_season)
    expected = first_season[: policy["minimum_episodes"]]
    return all(part.has_file for part in expected)


def acquisition_seasons(parts, current_season: int, policy: dict) -> list[int]:
    """The current season and the next known regular season, never N+2."""
    if current_season == 0 and not policy["manage_specials"]:
        return []
    regular = sorted(
        {
            p.season_number
            for p in parts
            if p.kind == "episode" and p.season_number and p.season_number > 0
        }
    )
    if current_season not in regular:
        return []
    result = [current_season]
    following = [season for season in regular if season > current_season]
    if following and policy["keep_one_season_ahead"]:
        result.append(following[0])
    return result


@dataclass(frozen=True)
class CandidateInput:
    media_id: str
    media_type: str
    added_at: datetime
    last_played_at: datetime | None
    size_bytes: int
    completed_episodes: int = 0
    favorite: bool = False
    never_purge: bool = False
    active_queue: bool = False
    snoozed_until: datetime | None = None
    mapped: bool = True


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def eligible(
    item: CandidateInput, policy: dict, now: datetime, *, healthy=True
) -> bool:
    added = _aware(item.added_at)
    now = _aware(now)
    if not healthy or not item.mapped or item.never_purge or item.active_queue:
        return False
    if item.snoozed_until and _aware(item.snoozed_until) > now:
        return False
    if added + timedelta(days=policy["grace_days"]) > now:
        return False
    if (
        item.media_type == "series"
        and item.last_played_at
        and _aware(item.last_played_at) + timedelta(days=policy["tv_inactivity_days"])
        > now
    ):
        return False
    return item.size_bytes > 0


def inactivity_due(item: CandidateInput, policy: dict, now: datetime) -> bool:
    days = (
        policy["tv_inactivity_days"]
        if item.media_type == "series"
        else policy["movie_inactivity_days"]
    )
    reference = max(
        _aware(item.added_at), _aware(item.last_played_at) or _aware(item.added_at)
    )
    return reference + timedelta(days=days) <= _aware(now)


def rank(items: list[CandidateInput], strategy: str, now: datetime, meaningful=2):
    epoch = datetime(1970, 1, 1, tzinfo=UTC)

    def key(item):
        watched = item.last_played_at is not None
        last = _aware(item.last_played_at) or epoch
        added = _aware(item.added_at)
        favorite = int(item.favorite)
        if strategy == "oldest_unwatched_first":
            return (
                favorite,
                int(item.completed_episodes >= meaningful),
                int(watched),
                last if watched else added,
                added,
                item.media_id,
            )
        if strategy == "oldest_watched_nonfavorite_first":
            return (
                favorite,
                int(not watched),
                last if watched else added,
                added,
                item.media_id,
            )
        if strategy == "weighted":
            score, _ = weighted_score(item, now, meaningful)
            return (-score, item.media_id)
        raise ValueError(f"Unknown purge strategy: {strategy}")

    return sorted(items, key=key)


def weighted_score(item: CandidateInput, now: datetime, meaningful=2):
    age = max(0, (_aware(now) - _aware(item.added_at)).days)
    last_age = (
        max(0, (_aware(now) - _aware(item.last_played_at)).days)
        if item.last_played_at
        else 0
    )
    detail = {
        "last_play_days": last_age * 1.0,
        "acquired_days": age * 0.2,
        "size_gib": item.size_bytes / (1024**3) * 0.5,
        "below_meaningful_bonus": 20 if item.completed_episodes < meaningful else 0,
        "unwatched_bonus": 30 if item.last_played_at is None else 0,
        "favorite_penalty": -200 if item.favorite else 0,
    }
    return sum(detail.values()), detail


def select_to_low_water(items: list[CandidateInput], current: int, high: int, low: int):
    if high <= low:
        raise ValueError("High-water bytes must exceed low-water bytes")
    if current <= high:
        return [], 0
    required = current - low
    selected, reclaimed = [], 0
    for item in items:
        selected.append(item)
        reclaimed += item.size_bytes
        if reclaimed >= required:
            break
    return selected, max(0, required - reclaimed)
