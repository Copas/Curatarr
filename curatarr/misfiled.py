"""Episodes Sonarr filed under the wrong episode.

A release numbered one way and read another (scene numbering applied to a
release already numbered the TVDB way, a special counted into the season)
lands on another episode's slot and is renamed to match. Two independent
pieces of evidence say which episode a file really is:

- **its download's name.** Sonarr's import history keeps it, and it often
  carries the episode's title ("…S03E02.Blood.in.the.Water…") even when the
  number was misread;
- **what's said in it.** Its transcript (from chapter, skip-marker or
  subtitle work) is scored against every episode's title and summary.

When the name gives another episode's title and the transcript fits that
episode best, the file is moved there in Sonarr (Settings → Automatic does it
unattended). When that slot already has a file, the file there must be shown
to be the same episode before the worse copy is deleted; a file there that's
itself waiting to move elsewhere is moved first. When the transcript fits the
episode the file is filed under, the name was wrong and nothing is listed.
Anything less certain is listed on Problems for a person.

The vacated episode is searched for again, unless the show uses scene
numbering and the release's own number was the other episode's: then a
search would ask indexers for that number and bring the mix-up straight
back, so it's unmonitored instead.
"""

import difflib
import os
import re
import time
from datetime import timedelta

from . import db
from .identity import MIN_SCORE, scores
from .integrations import IntegrationError
from .models import ChapterState, MediaIdentity, MediaPart, utcnow

KEY = "misfiled_episodes"
EVERY = timedelta(minutes=30)
RECENT = timedelta(days=14)
RUN_SECONDS = 20
FIXES_PER_RUN = 2
COMMAND_SECONDS = 180  # moving a file across a network share takes a while
CLEAR = 1.15  # the fitting episode must beat every other by this much
OVER_OWN = 1.25  # …and the one it's filed under by this much

_NOISE = re.compile(
    r"[ ._-](?:\d{3,4}p|MULTi|FiNAL|HEVC|AV1|x26[45]|h\.?26[45]|WEB|WEBRip|WEB-DL|"
    r"BluRay|HDTV|EAC3|AAC|DDP?|REPACK|PROPER|AMZN|NF|ATVP|DSNP|HMAX|MAX|HULU|"
    r"iNTERNAL)\b",
    re.IGNORECASE,
)
_SOURCE_RANK = {
    "unknown": 0,
    "dvd": 1,
    "television": 2,
    "televisionraw": 3,
    "webrip": 4,
    "web": 5,
    "bluray": 6,
    "blurayraw": 7,
}


class _Changed(ValueError):
    """Sonarr's episodes aren't as they were when the problem was found."""


# Release names -------------------------------------------------------------


def norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def titles_agree(a, b) -> bool:
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a == b or (min(len(a), len(b)) >= 6 and (a in b or b in a)):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def release_title(name) -> str:
    """The episode title in a release name: "Jail Break" from
    Show.S01E49.Jail.Break.1080p.WEB…"""
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    match = re.search(r"S\d+E\d+(?:-?E?\d+)*[ ._-]+(.*)", stem, re.IGNORECASE)
    if not match:
        return ""
    return re.sub(r"[._]", " ", _NOISE.split(match[1])[0]).strip(" -")


def release_number(name) -> str | None:
    match = re.search(r"S(\d+)E(\d+)", os.path.basename(name or ""), re.IGNORECASE)
    return f"S{int(match[1]):02}E{int(match[2]):02}" if match else None


def code(episode) -> str:
    return (
        f"S{int(episode.get('seasonNumber') or 0):02}"
        f"E{int(episode.get('episodeNumber') or 0):02}"
    )


def _rank(quality) -> tuple[int, int]:
    inner = (quality or {}).get("quality") or {}
    source = str(inner.get("source") or "").lower()
    return int(inner.get("resolution") or 0), _SOURCE_RANK.get(source, 0)


def _quality_name(quality) -> str:
    return ((quality or {}).get("quality") or {}).get("name") or "unknown quality"


# Transcripts ---------------------------------------------------------------


def _transcript(episode_id) -> str | None:
    from .enrichment import cache_dir
    from .transcribe import load

    state = (
        db.session.query(ChapterState)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .filter(
            MediaPart.sonarr_episode_id == episode_id,
            MediaPart.has_file.is_(True),
            ChapterState.transcript_key.isnot(None),
        )
        .first()
    )
    if state is None:
        return None
    hit = load(cache_dir() / "transcripts" / f"{state.transcript_key}.json")
    if not hit:
        return None
    # Recaps and previews are clips of other episodes; leave them out.
    spans = [
        (start, end)
        for start, end in (
            (state.recap_start, state.recap_end),
            (state.preview_start, state.preview_end),
        )
        if end is not None
    ]
    return " ".join(
        s.text for s in hit[0] if not any(a <= s.start < b for a, b in spans)
    )


def fits(text, episodes, own_id, other_id) -> str:
    """Which of two episodes a transcript is: "other" when it fits the other
    episode best, clearly; "own" when it fits its own best, clearly; else
    "unsure". Scored against the whole season (and the other episode, if it's
    in another season), so vague summaries can't win by default."""
    own = next(e for e in episodes if e["id"] == own_id)
    summaries = {
        e["id"]: f"{e.get('title') or ''}. {e.get('overview') or ''}"
        for e in episodes
        if e.get("seasonNumber") == own.get("seasonNumber") or e["id"] == other_id
    }
    fit = scores(text or "", summaries)
    if own_id not in fit or other_id not in fit:
        return "unsure"
    order = sorted(fit, key=fit.get, reverse=True)
    best, top = order[0], fit[order[0]]
    second = fit[order[1]] if len(order) > 1 else 0.0
    if top < MIN_SCORE or top < CLEAR * second:
        return "unsure"
    if best == other_id and top >= OVER_OWN * fit[own_id]:
        return "other"
    if best == own_id:
        return "own"
    return "unsure"


# Finding them ----------------------------------------------------------------


def _findings(sonarr, now) -> dict:
    """{file id: finding} for recent imports whose download name gives another
    episode's title."""
    since = (now - RECENT).strftime("%Y-%m-%dT%H:%M:%SZ")
    records = sonarr.imports_since(since) or []
    series = {s["id"]: s for s in sonarr.series() or []}
    episodes_by_series, files_by_series, found = {}, {}, {}
    for record in records:
        data = record.get("data") or {}
        dropped = data.get("droppedPath") or record.get("sourceTitle") or ""
        linked = record.get("episode") or {}
        got = release_title(dropped)
        if len(norm(got)) < 5 or titles_agree(got, linked.get("title")):
            continue
        try:
            file_id = int(data.get("fileId") or 0)
        except (TypeError, ValueError):
            continue
        series_id = record.get("seriesId")
        if not file_id or file_id in found or series_id not in series:
            continue
        if series_id not in episodes_by_series:
            episodes_by_series[series_id] = sonarr.episodes(series_id) or []
            files_by_series[series_id] = {
                f["id"]: f for f in sonarr.episode_files(series_id) or []
            }
        episodes = episodes_by_series[series_id]
        current = next((e for e in episodes if e.get("id") == linked.get("id")), None)
        if current is None or current.get("episodeFileId") != file_id:
            continue  # fixed, replaced or deleted since the import
        matches = [
            e
            for e in episodes
            if e["id"] != current["id"] and titles_agree(got, e.get("title"))
        ]
        if not matches:
            continue
        show = series[series_id]
        finding = {
            "series_id": series_id,
            "series_title": show.get("title") or "",
            "folder": os.path.basename(str(show.get("path") or "").rstrip("/")),
            "file_id": file_id,
            "download": os.path.basename(dropped),
            "from_id": current["id"],
            "from_code": code(current),
            "from_title": current.get("title") or "",
            "from_monitored": bool(current.get("monitored")),
            "file_quality": _quality_name(
                (files_by_series[series_id].get(file_id) or {}).get("quality")
            ),
        }
        if len(matches) > 1:
            finding["verdict"] = "ambiguous"
            finding["candidates"] = [f"{code(e)} “{e.get('title')}”" for e in matches]
            found[file_id] = finding
            continue
        target = matches[0]
        target_file = (
            files_by_series[series_id].get(target.get("episodeFileId"))
            if target.get("hasFile")
            else None
        )
        scene = bool(show.get("useSceneNumbering")) and release_number(dropped) == code(
            target
        )
        finding |= {
            "to_id": target["id"],
            "to_code": code(target),
            "to_title": target.get("title") or "",
            "target_file_id": int((target_file or {}).get("id") or 0),
            "target_quality": _quality_name((target_file or {}).get("quality")),
            "better_than_target": target_file is not None
            and _rank(files_by_series[series_id].get(file_id, {}).get("quality"))
            > _rank(target_file.get("quality")),
            "scene": scene,
        }
        text = _transcript(current["id"])
        finding["verdict"] = (
            "no_transcript"
            if text is None
            else fits(text, episodes, current["id"], target["id"])
        )
        if target_file is not None:
            target_text = _transcript(target["id"])
            finding["target_is_target"] = target_text is not None and (
                fits(target_text, episodes, target["id"], current["id"]) == "own"
            )
        found[file_id] = finding
    return found


def _plan(finding, findings) -> str | None:
    """How a confirmed finding is fixed now: "move" (the slot is free),
    "replace"/"discard" (the slot holds the same episode: keep the better
    copy), "wait" (the slot's file moves elsewhere first), or None (a person
    should look)."""
    if finding.get("verdict") != "other":
        return None
    if not finding["target_file_id"]:
        return "move"
    moving = findings.get(finding["target_file_id"])
    if moving and moving.get("verdict") == "other":
        return "wait"
    if finding.get("target_is_target"):
        return "replace" if finding["better_than_target"] else "discard"
    return None


def sweep(now=None) -> int:
    """Look at recent imports every half hour; fix confirmed ones when the
    automatic fix is on. Returns how many were fixed."""
    from . import autofix
    from .services import client, set_setting, setting

    now = now or utcnow()
    from .services import _as_datetime

    stored = dict(setting(KEY, {}) or {})
    checked = _as_datetime(stored.get("checked_at"))
    if checked and now - checked < EVERY:
        found = stored.get("findings") or {}
    else:
        try:
            sonarr = client("sonarr")
            db.session.rollback()  # ask Sonarr outside any transaction
            fresh = _findings(sonarr, now)
        except IntegrationError:
            return 0
        old = stored.get("findings") or {}
        found = {}
        for file_id, finding in fresh.items():
            previous = old.get(str(file_id)) or {}
            same = previous.get("to_id") == finding.get("to_id")
            found[str(file_id)] = finding | {
                "dismissed": bool(previous.get("dismissed") and same),
                "failed": previous.get("failed") if same else None,
            }
        stored = {"checked_at": now.isoformat(), "findings": found}
        set_setting(KEY, stored)
        db.session.commit()
    if not autofix.automatic("misfiled"):
        return 0
    by_file = {int(k): v for k, v in found.items()}
    fixed, started = 0, time.monotonic()
    for file_id, finding in sorted(by_file.items()):
        if fixed >= FIXES_PER_RUN or time.monotonic() - started > RUN_SECONDS:
            break
        if finding.get("dismissed") or finding.get("failed") or finding.get("fixed"):
            continue
        if _plan(finding, by_file) in {"move", "replace", "discard"}:
            try:
                fix(file_id, automatic=True)
                fixed += 1
            except ValueError:
                continue
    return fixed


# Showing and fixing them -------------------------------------------------------


def rows() -> list[dict]:
    """Findings for Problems and Overview, with how each would be fixed."""
    from .services import setting

    found = {
        int(k): v for k, v in ((setting(KEY, {}) or {}).get("findings") or {}).items()
    }
    media = {
        m.sonarr_id: m
        for m in db.session.query(MediaIdentity).filter(
            MediaIdentity.sonarr_id.in_(
                {f["series_id"] for f in found.values()} or {0}
            ),
            MediaIdentity.media_type == "series",
        )
    }
    result = []
    for file_id, finding in sorted(
        found.items(), key=lambda kv: (kv[1]["series_title"], kv[1]["from_code"])
    ):
        if finding.get("dismissed") or finding.get("fixed"):
            continue
        if finding.get("verdict") == "own":
            continue  # what's said fits where it's filed: the name was wrong
        plan = _plan(finding, found)
        result.append(
            finding
            | {
                "file_id": file_id,
                "plan": plan,
                "media": media.get(finding["series_id"]),
                "evidence": evidence(finding),
            }
        )
    return result


def count() -> int:
    return len([r for r in rows() if r["plan"] != "wait"])


def evidence(finding) -> list[str]:
    lines = [f"Downloaded as “{finding['download']}”"]
    if finding.get("verdict") == "ambiguous":
        lines.append(
            "Its title fits more than one episode: " + ", ".join(finding["candidates"])
        )
        return lines
    lines.append(
        f"Filed as {finding['from_code']} “{finding['from_title']}”; the name says "
        f"{finding['to_code']} “{finding['to_title']}”"
    )
    lines.append(
        {
            "other": f"What's said in it fits {finding['to_code']} best",
            "unsure": "What's said in it doesn't clearly fit either episode",
            "no_transcript": "It hasn't been transcribed, so what's said in it can't confirm it",
        }.get(finding.get("verdict"), "")
    )
    if finding.get("target_file_id"):
        lines.append(
            f"{finding['to_code']} already has a file ({finding['target_quality']})"
            + (", which is that episode too" if finding.get("target_is_target") else "")
        )
    if finding.get("failed"):
        lines.append(f"Fixing it failed: {finding['failed']}")
    return [line for line in lines if line]


def dismiss(file_id) -> None:
    from .services import set_setting, setting

    stored = dict(setting(KEY, {}) or {})
    found = dict(stored.get("findings") or {})
    if str(file_id) in found:
        found[str(file_id)] = {**found[str(file_id)], "dismissed": True}
        stored["findings"] = found
        set_setting(KEY, stored)
    db.session.commit()


def fix(file_id, *, automatic=False, mode=None) -> str:
    """Move (or de-duplicate) one misfiled file in Sonarr. ``mode`` lets a
    person choose what the evidence couldn't settle ("move", "replace",
    "discard"); the slots are rechecked first either way."""
    from .filecheck_jobs import _dry_run
    from .services import audit, client, set_setting, setting

    stored = dict(setting(KEY, {}) or {})
    found = dict(stored.get("findings") or {})
    finding = found.get(str(file_id))
    if not finding or not finding.get("to_id"):
        raise ValueError("Nothing is flagged for that file any more")
    by_file = {int(k): v for k, v in found.items()}
    plan = mode or _plan(finding, by_file)
    if plan not in {"move", "replace", "discard"}:
        raise ValueError("The evidence doesn't settle this one; choose what to do")
    if plan == "move" and finding["target_file_id"]:
        plan = "replace"  # a person chose to move it onto a filled slot
    media = (
        db.session.query(MediaIdentity)
        .filter_by(sonarr_id=finding["series_id"], media_type="series")
        .first()
    )
    if media is not None and plan != "move" and _dry_run(media):
        raise ValueError(
            "dry run is on for this library, so Curatarr won't delete files"
        )
    sonarr = client("sonarr")
    steps = []
    try:
        source = sonarr.episode(finding["from_id"])
        target = sonarr.episode(finding["to_id"])
        if int(source.get("episodeFileId") or 0) != int(file_id) or int(
            target.get("episodeFileId") or 0
        ) != int(finding["target_file_id"]):
            raise _Changed("The episodes changed in Sonarr since this was found")
        if plan == "discard":
            sonarr.delete_episode_file(file_id)
            steps.append(
                f"deleted the misfiled copy; {finding['to_code']} already had one at "
                "least as good"
            )
        else:
            misfiled = sonarr.episode_file(file_id)
            if plan == "replace":
                sonarr.delete_episode_file(finding["target_file_id"])
                steps.append(
                    f"deleted the worse copy of {finding['to_code']} "
                    f"({finding['target_quality']})"
                )
            done = sonarr.command(
                "ManualImport",
                wait_seconds=COMMAND_SECONDS,
                importMode="move",
                files=[
                    {
                        "path": misfiled["path"],
                        "folderName": finding["folder"],
                        "seriesId": finding["series_id"],
                        "episodeIds": [finding["to_id"]],
                        "quality": misfiled.get("quality"),
                        "languages": misfiled.get("languages") or [],
                        "releaseGroup": misfiled.get("releaseGroup"),
                        "indexerFlags": 0,
                        "releaseType": "singleEpisode",
                    }
                ],
            )
            if str((done or {}).get("status")).lower() != "completed":
                raise ValueError(
                    f"Sonarr's import ended as {(done or {}).get('status')}"
                )
            steps.append(f"moved it to {finding['to_code']}")
            new_id = int(sonarr.episode(finding["to_id"]).get("episodeFileId") or 0)
            if any(
                r.get("episodeFileId") == new_id
                for r in sonarr.rename_candidates(finding["series_id"]) or []
            ):
                sonarr.command(
                    "RenameFiles",
                    wait_seconds=COMMAND_SECONDS,
                    seriesId=finding["series_id"],
                    files=[new_id],
                )
                steps.append("renamed it to match")
        # The vacated episode: unless another misfiled file is about to fill it.
        refilled = any(
            f.get("to_id") == finding["from_id"] and f.get("verdict") == "other"
            for f in found.values()
        )
        if not refilled and finding.get("from_monitored"):
            if finding.get("scene"):
                sonarr.set_episodes_monitored([finding["from_id"]], False)
                steps.append(
                    f"unmonitored {finding['from_code']}: a search for it would bring "
                    "the same mix-up back"
                )
            else:
                sonarr.episode_search([finding["from_id"]])
                steps.append(f"searched for the real {finding['from_code']}")
    except _Changed as exc:
        # Not a failure: look again on the next pass, with Sonarr as it is now.
        stored.pop("checked_at", None)
        set_setting(KEY, stored)
        db.session.commit()
        raise ValueError(str(exc)) from exc
    except (IntegrationError, ValueError, KeyError) as exc:
        found[str(file_id)] = {**finding, "failed": str(exc)}
        stored["findings"] = found
        set_setting(KEY, stored)
        db.session.commit()
        raise ValueError(str(exc)) from exc
    at = utcnow().isoformat()
    found[str(file_id)] = {**finding, "fixed": at}
    stored["findings"] = found
    # A fix frees an episode another misfiled file may be waiting for: look
    # again on the next pass instead of in half an hour.
    stored.pop("checked_at", None)
    set_setting(KEY, stored)
    summary = (
        f"{finding['series_title']} {finding['from_code']} was really "
        f"{finding['to_code']} “{finding['to_title']}”: " + "; ".join(steps) + "."
    )
    audit(
        "misfiled_episode_fixed",
        media.id if media else None,
        ("Automatically: " if automatic else "") + summary,
        f"misfiled:{file_id}:{at}",
        payload={"finding": finding, "plan": plan},
    )
    db.session.commit()
    return summary[0].upper() + summary[1:]
