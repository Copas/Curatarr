"""``flask --app curatarr chapters ...``: preview and maintenance from a shell."""

import json
import shlex

import click
from flask import current_app

from . import db
from .chapters import (
    Chapter,
    ChapterConfigError,
    effective_config,
    timestamp,
    validate_chapters,
)
from .enrichment import (
    analyse,
    config_for,
    dispatch,
    global_layer,
    local_path,
    maintenance_summary,
    options,
    queue_scan,
    request,
    write_operation,
)
from .mediafile import MediaToolError, can_write, probe
from .models import Library, MediaPart


@click.group("chapters")
def chapters_group():
    """Chapter generation (media enrichment)."""


def _short(seconds):
    return timestamp(seconds)[:8]


def print_report(info, analysis, config):
    echo = click.echo
    echo(f"File:      {info.path}")
    echo(f"Container: {info.format_name}   Size: {info.size / 1024**3:.2f} GiB")
    echo(f"Duration:  {_short(info.duration)} ({info.duration / 60:.1f} minutes)")
    if info.chapters:
        echo(f"Existing chapters: {len(info.chapters)} (would be preserved)")
        for chapter in info.chapters:
            echo(f"  {_short(chapter['start'])}  {chapter['name']}")
    else:
        echo("Existing chapters: none")
    minimum = config["minimum_duration_minutes"]
    echo(
        f"Minimum duration {minimum} min: "
        + ("met" if info.duration >= minimum * 60 else "NOT met (would be skipped)")
    )
    if analysis is None:
        return
    scenes = analysis["scenes"]
    echo("")
    if scenes:
        echo(
            f"Scene-change candidates: {scenes['count']} at threshold "
            f"{scenes['threshold']}{' (cached)' if scenes['cached'] else ''}"
        )
        changes = scenes["changes"]
        echo(
            "  "
            + ", ".join(f"{_short(t)} ({s:.2f})" for t, s in changes[:40])
            + (f", … {len(changes) - 40} more" if len(changes) > 40 else "")
        )
    else:
        echo(f"Scene detection: failed ({analysis['scene_error']})")
    transcript = analysis["transcript"]
    if transcript:
        echo("")
        echo(
            f"Transcript: {transcript['segments']} segments, {transcript['words']} "
            f"words, language {transcript.get('language', '?')}, "
            + (
                "from cache"
                if transcript.get("cached")
                else f"{transcript.get('model')} on {transcript.get('device')} "
                f"({transcript.get('compute_type')})"
            )
        )
        for start, text in transcript["sample"][:6]:
            echo(f"  [{_short(start)}] {text}")
    labels = {
        "semantic": "Smart (semantic)",
        "scene_aligned": "Scene-aligned fallback",
        "fixed": "Fixed-interval fallback",
    }
    for attempt in analysis["attempts"]:
        method = attempt["method"]
        echo("")
        if not attempt["ok"]:
            echo(f"{labels[method]}: FAILED - {attempt['error']}")
            continue
        plan = analysis["plans"][method]
        echo(f"{labels[method]}: {len(plan['chapters'])} chapters")
        previous = None
        for chapter in plan["chapters"]:
            gap = (
                f"  (+{(chapter['start'] - previous) / 60:.1f} min)"
                if previous is not None
                else ""
            )
            echo(f"  {_short(chapter['start'])}  {chapter['name']}{gap}")
            previous = chapter["start"]
        for note in plan["notes"]:
            echo(f"  note: {note}")
    chosen = analysis["chosen"]
    echo("")
    echo(
        f"Would use: {labels.get(chosen, 'nothing - every method failed')}"
        f"   (analysis took {analysis['elapsed_seconds']} s)"
    )
    if chosen:
        plan = analysis["plans"][chosen]
        chapters = [Chapter(c["start"], c["name"]) for c in plan["chapters"]]
        validate_chapters(chapters, info.duration)
        writable, reason = can_write(info)
        operation = write_operation(info, chapters)
        echo("")
        echo("Write operation (NOT performed):")
        echo("  " + shlex.join(operation["command"]))
        echo("  generated-chapters.txt:")
        for line in operation["chapter_file"].splitlines():
            echo(f"    {line}")
        echo(
            "  In place, no re-encode or remux; streams and duration are verified "
            "afterwards, then Jellyfin refreshes this one item."
        )
        if not writable:
            echo(f"  Curatarr could not write it now: {reason}")


@chapters_group.command("preview")
@click.argument("target")
@click.option("--library", "library_name", help="Use this library's chapter settings")
@click.option("--mode", type=click.Choice(["smart", "navigation"]))
@click.option("--llm-model", help="Ollama model for Smart Chapters")
@click.option("--whisper-model", help="faster-whisper model (default: medium)")
@click.option("--json", "as_json", is_flag=True, help="Print the raw analysis as JSON")
def preview_command(target, library_name, mode, llm_model, whisper_model, as_json):
    """Show what chapters would be generated for a file. Never modifies it.

    TARGET is a file path (as this host sees it) or a Curatarr media part id.
    Every method is attempted, so the fallbacks can be compared.
    """
    part = db.session.get(MediaPart, target)
    if part:
        path = local_path(part.file_path)
        library_id = part.media.library_id
    else:
        path, library_id = target, None
        if library_name:
            library = db.session.query(Library).filter_by(name=library_name).first()
            if not library:
                raise click.ClickException(f"No library named {library_name}")
            library_id = library.id
    try:
        config = (
            config_for(library_id) if library_id else effective_config(global_layer())
        )
        overrides = {}
        if mode:
            overrides["mode"] = mode
        semantic = {}
        if llm_model:
            semantic["llm_model"] = llm_model
        if whisper_model:
            semantic["model"] = whisper_model
        if semantic:
            overrides["semantic"] = semantic
        config = effective_config(config, overrides)
        opts = options()
    except ChapterConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        info = probe(path)
    except (MediaToolError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    analysis = None
    if info.duration >= config["minimum_duration_minutes"] * 60:
        analysis = analyse(info, config, opts, every_method=True)
    if as_json:
        click.echo(
            json.dumps(
                {"file": info.as_dict(), "analysis": analysis}, indent=2, default=str
            )
        )
        return
    print_report(info, analysis, config)


@chapters_group.command("scan")
@click.option("--library", "library_name", help="Only this library")
@click.option("--now", is_flag=True, help="Run the scan here instead of the worker")
@click.option("--all", "everything", is_flag=True, help="Every file, not only new ones")
def scan_command(library_name, now, everything):
    """Inspect files for existing chapters (read-only; nothing is generated)."""
    library_id = None
    if library_name:
        library = db.session.query(Library).filter_by(name=library_name).first()
        if not library:
            raise click.ClickException(f"No library named {library_name}")
        library_id = library.id
    count = queue_scan(library_id, everything=everything)
    click.echo(f"Queued {count} files for a chapter scan")
    if now:
        while dispatch(synchronous=True):
            pass
    click.echo(json.dumps(maintenance_summary(library_id), indent=2))


@chapters_group.command("run")
@click.argument("part_id")
@click.argument(
    "action", type=click.Choice(["preview", "generate", "regenerate", "remove"])
)
def run_command(part_id, action):
    """Run one chapter action for one media part now (in this process)."""
    part = db.session.get(MediaPart, part_id)
    if not part:
        raise click.ClickException("No media part with that id")
    try:
        state = request(part, action)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    dispatch(synchronous=True)
    db.session.refresh(state)
    click.echo(
        f"{state.chapter_status}"
        + (f": {state.chapter_error}" if state.chapter_error else "")
    )
    current_app.logger.info("Chapter %s for part %s finished", action, part_id)


@chapters_group.command("status")
def status_command():
    """Counts of files by chapter status."""
    click.echo(json.dumps(maintenance_summary(), indent=2))
