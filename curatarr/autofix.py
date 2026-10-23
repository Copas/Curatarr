"""Automatic fixes: do the safe thing instead of asking, once.

Each kind of fix can be switched off in Settings → Automatic. All are on by
default: each only acts when it can be confident (a better copy is checked to
exist first, or two independent pieces of evidence agree). When it's on, Curatarr starts the fix itself as soon as it finds the
problem, and also for problems found before it was switched on:

- **files**: replace problem files (filecheck_jobs);
- **dubs**: replace films with no English audio (dub_jobs);
- **wrong_show**: replace files of another show with the same name (wrong_version);
- **missing**: search once for always-keep episodes that never arrived (missing_keep);
- **misfiled**: move episodes Sonarr filed under the wrong episode (misfiled).

Replacing is safe to do unattended because it checks a better copy exists
before anything is deleted (better_copy). A fix is tried once: when nothing
better exists, or the episodes are still missing after the search, the item is
dropped quietly and History says so. Overview then shows one line about what
was handled instead of a row per item.
"""

from datetime import timedelta

from . import db

KEY = "auto_fixes"
ON_BY_DEFAULT = True
KINDS = {
    "files": "Replace problem files (cut short, broken, burned-in subtitles)",
    "dubs": "Replace films with no English audio",
    "wrong_show": "Replace files that are another show with the same name",
    "missing": "Search once for always-keep episodes that never arrived",
    "misfiled": "Move episodes Sonarr filed under the wrong episode, when what's "
    "said in them agrees with their download's name",
}
# The first two were Enhance options before this page existed; they stay there.
ENHANCE_OPTION = {
    "files": "file_replace_automatically",
    "dubs": "dub_replace_automatically",
}
SEARCHES_PER_RUN = 3  # missing-episode searches started per worker cycle
SUMMARY_DAYS = 7


def enabled() -> dict:
    from .chapters import ChapterConfigError
    from .enrichment import options
    from .services import setting

    stored = setting(KEY, {}) or {}
    try:
        enhance = options()
    except ChapterConfigError:  # an unreadable Enhance config turns nothing on
        enhance = {}
    return {
        kind: bool(enhance.get(ENHANCE_OPTION[kind]))
        if kind in ENHANCE_OPTION
        else bool(stored.get(kind, ON_BY_DEFAULT))
        for kind in KINDS
    }


def save(form) -> None:
    from .services import set_setting, setting

    chosen = {kind: form.get(kind) == "on" for kind in KINDS}
    enhance = dict(setting("chapter_options", {}) or {})
    for kind, option in ENHANCE_OPTION.items():
        enhance[option] = chosen[kind]
    set_setting("chapter_options", enhance)
    set_setting(KEY, {k: v for k, v in chosen.items() if k not in ENHANCE_OPTION})
    db.session.commit()


def automatic(kind) -> bool:
    return enabled().get(kind, False)


def sweep() -> int:
    """Start fixes for what's waiting, and drop what's been tried (worker cycle)."""
    on = enabled()
    started = 0
    if on["files"] or on["dubs"] or on["wrong_show"]:
        started += _queue_replacements(on)
    if on["missing"]:
        started += _search_missing()
    return started


def _queue_replacements(on) -> int:
    from . import better_copy, dub_jobs, filecheck_jobs, wrong_version

    started = 0
    targets = []
    if on["files"]:
        targets += [(s, "file") for s in filecheck_jobs.review()["to_act"]]
    if on["dubs"]:
        targets += [(s, "dub") for s in dub_jobs.review()["to_act"]]
    if on["wrong_show"]:
        targets += [(row["media"], "version") for row in wrong_version.rows()]
    for target, kind in targets:
        media = target if kind == "version" else target.media
        if filecheck_jobs._dry_run(media):
            continue  # dry run never deletes, so never replaces either
        status = better_copy.status(target, kind)
        if status is None:
            better_copy.request(target, kind, auto=True)
            started += 1
        elif status == "no_better" and better_copy.was_automatic(target, kind):
            better_copy.give_up(target, kind)
    return started


def _search_missing() -> int:
    from . import missing_keep
    from .integrations import IntegrationError
    from .services import audit, utcnow

    started = 0
    for row in missing_keep.rows():
        media = row["media"]
        if row["state"] == "still_missing":
            missing_keep.ignore(media)
            audit(
                "auto_fix_gave_up",
                media.id,
                f"Still missing {row['codes']} after searching; stopped trying.",
                f"auto-gave-up:missing:{media.id}:{row['codes']}",
                payload={"kind": "missing", "codes": row["codes"]},
            )
            db.session.commit()
        elif row["state"] == "new" and started < SEARCHES_PER_RUN:
            try:
                missing_keep.search_once(media)
            except (IntegrationError, ValueError):
                db.session.rollback()
                continue
            audit(
                "auto_fix_started",
                media.id,
                f"Searched for {row['codes']} automatically.",
                f"auto-search:{media.id}:{utcnow().date().isoformat()}",
                payload={"kind": "missing", "codes": row["codes"]},
            )
            db.session.commit()
            started += 1
    return started


def summary(days=SUMMARY_DAYS) -> dict | None:
    """What was handled lately, for Overview's one line (None: nothing)."""
    from sqlalchemy import func

    from .models import LifecycleAction, utcnow

    since = utcnow() - timedelta(days=days)
    counts = dict(
        db.session.query(LifecycleAction.action_type, func.count())
        .filter(
            LifecycleAction.created_at >= since,
            LifecycleAction.action_type.in_(
                [
                    "file_replaced",
                    "dub_replaced",
                    "wrong_version_replaced",
                    "auto_fix_started",
                    "auto_fix_gave_up",
                ]
            ),
        )
        .group_by(LifecycleAction.action_type)
        .all()
    )
    result = {
        "replaced": counts.get("file_replaced", 0)
        + counts.get("dub_replaced", 0)
        + counts.get("wrong_version_replaced", 0),
        "searched": counts.get("auto_fix_started", 0),
        "gave_up": counts.get("auto_fix_gave_up", 0),
        "days": days,
    }
    if not (result["replaced"] or result["searched"] or result["gave_up"]):
        return None
    return result
