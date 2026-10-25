"""A small JSON API for remotes, Home Assistant and the like: a title's state
and flags by its Jellyfin id, the household's review choices and the flags,
and the list of files a transcoder must leave alone.

Reading takes the API key (X-Api-Key) or the webhook token
(X-Curatarr-Token); changing anything takes the webhook token, which the
Curatarr plugin for Jellyfin already holds.
"""

import hmac

from flask import Response, abort, jsonify, request

from . import db, flags
from .integrations import IntegrationError
from .models import MediaIdentity, MediaPart
from .routes import PUBLIC_ENDPOINTS, TOKEN_PATH_PREFIXES, bp

PUBLIC_ENDPOINTS.update(
    {
        "main.api_title",
        "main.api_title_change",
        "main.api_protected",
        "main.api_compression",
        "main.api_compression_report",
    }
)
TOKEN_PATH_PREFIXES.extend(["/api/v1/titles/", "/api/v1/compression"])
REVIEW_CHOICES = {"keep", "snooze_30", "snooze_90"}


def _token_ok() -> bool:
    from .services import setting

    token = setting("webhook_token")
    supplied = request.headers.get("X-Curatarr-Token", "")
    return bool(token) and hmac.compare_digest(supplied, token)


def _may_read() -> bool:
    from flask import current_app

    from .auth import current_user
    from .services import api_key_valid

    return (
        current_app.config["ALLOW_UNAUTHENTICATED"]
        or _token_ok()
        or api_key_valid(request.headers.get("X-Api-Key"))
        or bool(current_user())
    )


def _bare(value) -> str:
    return str(value or "").replace("-", "").lower()


def resolve(item_id):
    """(title, season, episode part) for a Jellyfin film, series, season or
    episode id; raises LookupError when Curatarr doesn't know it."""
    from .services import client

    wanted = _bare(item_id)
    if not wanted:
        raise LookupError
    for media in db.session.query(MediaIdentity).filter(
        MediaIdentity.jellyfin_id.isnot(None)
    ):
        if _bare(media.jellyfin_id) == wanted:
            return media, None, None
    for part in db.session.query(MediaPart).filter(MediaPart.jellyfin_id.isnot(None)):
        if _bare(part.jellyfin_id) == wanted:
            return db.session.get(MediaIdentity, part.media_identity_id), None, part
    try:
        item = client("jellyfin").item(item_id)
    except IntegrationError as exc:
        raise LookupError from exc
    if item.get("Type") == "Season" and item.get("SeriesId"):
        show, _season, _part = resolve(item["SeriesId"])
        return show, item.get("IndexNumber"), None
    raise LookupError


def _view(media, season, part) -> dict:
    from .lifecycle import _active_candidate
    from .services import resolved_policy

    candidate = _active_candidate(media.id)
    policy, _ = resolved_policy(media)
    level = "episode" if part else "season" if season is not None else "title"
    return {
        "id": media.id,
        "title": media.title,
        "type": "film" if media.media_type == "movie" else "series",
        "level": level,
        "season": part.season_number if part else season,
        "episode": part.episode_number if part else None,
        "state": candidate.state if candidate else None,
        "leaving_on": candidate.scheduled_delete_at.isoformat()
        if candidate and candidate.scheduled_delete_at
        else None,
        "never_purge": bool(policy["never_purge"]),
        "flags": [
            {
                "flag": row.flag,
                "season": row.season_number,
                "part": row.media_part_id,
                "user": row.jellyfin_user_id,
                "since": row.created_at.isoformat(),
                "says": flags.describe(row),
            }
            for row in flags.flags_for(media)
        ],
    }


@bp.get("/api/v1/titles/by-jellyfin/<item_id>")
def api_title(item_id):
    if not _may_read():
        abort(401)
    try:
        media, season, part = resolve(item_id)
    except LookupError:
        return jsonify({"error": "Curatarr doesn't know that item."}), 404
    return jsonify(_view(media, season, part))


@bp.post("/api/v1/titles/by-jellyfin/<item_id>")
def api_title_change(item_id):
    """{"action": keep | snooze_30 | snooze_90 | never_purge |
    leave_after_watched | never_compress | clear, "flag": (for clear),
    "user": (leave_after_watched: whose watch counts; omitted: anyone's),
    "mode": (leave_after_watched on a show or season: "all" once its last
    episode is watched, the default, or "each" episode as it's watched)}.
    A season's or episode's review choices apply to its show."""
    from .lifecycle import _active_candidate, review_candidate
    from .services import save_title_override

    try:
        media, season, part = resolve(item_id)
    except LookupError:
        return jsonify({"error": "Curatarr doesn't know that item."}), 404
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        abort(400)
    action = body.get("action")
    try:
        if action in REVIEW_CHOICES:
            candidate = _active_candidate(media.id)
            if candidate is None:
                raise ValueError(f"{media.title} isn't up for removal.")
            review_candidate(candidate.id, action)
        elif action == "never_purge":
            save_title_override(media, {"never_purge": "true"})
        elif action in flags.FLAGS:
            flags.set_flag(
                media,
                action,
                season=season,
                part=part,
                user_id=body.get("user") or None,
                set_by="API",
                mode=body.get("mode"),
            )
        elif action == "clear" and body.get("flag") in flags.FLAGS:
            flags.clear_flag(media, body["flag"], season=season, part=part)
        elif action == "clear" and body.get("flag") == "never_purge":
            save_title_override(media, {"never_purge": "false"})
        else:
            return jsonify({"error": "Unknown action."}), 400
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 409
    if action in REVIEW_CHOICES | {"never_purge"}:
        from .artwork import reconcile_artwork

        reconcile_artwork()  # Leaving Soon's banner comes off now
    return jsonify(_view(media, season, part))


@bp.get("/api/v1/protected-paths")
def api_protected():
    """Never compress: one path per line, as Sonarr and Radarr report them."""
    if not _may_read():
        abort(401)
    if request.args.get("kind", "compress") != "compress":
        abort(404)
    text = "".join(f"{path}\n" for path in flags.protected_paths())
    return Response(text, mimetype="text/plain")


def _compression_view() -> dict:
    from . import compression

    current = compression.signal()
    held = compression.holding()
    return {
        "enabled": compression.settings()["enabled"],
        "pending": current["pending"] if current else 0,
        "source": current["source"] if current else None,
        "at": current["at"].isoformat() if current else None,
        "holding": bool(held),
    }


@bp.get("/api/v1/compression")
def api_compression():
    """Compression first: what the transcoder last said, and whether
    low-space cleanup is on hold."""
    if not _may_read():
        abort(401)
    return jsonify(_compression_view())


@bp.post("/api/v1/compression")
def api_compression_report():
    """{"pending": files still to compress (or true/false), "source": name}."""
    from . import compression

    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict) or "pending" not in body:
        return jsonify({"error": 'Send {"pending": <files waiting>}.'}), 400
    try:
        compression.report(body["pending"], body.get("source"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(_compression_view())
