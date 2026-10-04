"""Sign-in with Jellyfin administrator accounts (docs/decisions/007-jellyfin-sign-in.md).

Curatarr never stores passwords or Jellyfin user tokens. A sign-in verifies
the credentials against Jellyfin, requires the Administrator policy, ends the
Jellyfin session it just created, and keeps only the user ID and name in the
signed Flask session. The account is re-checked with Curatarr's own API key
every few minutes so removed or demoted administrators lose access.
"""

import os
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from uuid import uuid4

from flask import current_app, session

from . import db
from .integrations import IntegrationError, JellyfinClient, normalized_url
from .models import Integration, utcnow
from .observability import log_operation
from .services import _as_datetime, audit, client, set_setting, setting

REVALIDATE_SECONDS = 300
OUTAGE_GRACE = timedelta(hours=1)
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 900
ADMIN_REQUIRED = "Curatarr requires a Jellyfin administrator account."

_failures: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


class SignInError(Exception):
    pass


def reset_rate_limits():
    with _lock:
        _failures.clear()


def _rate_limited(key):
    cutoff = time.monotonic() - FAILURE_WINDOW_SECONDS
    with _lock:
        attempts = _failures[key]
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        return len(attempts) >= MAX_FAILURES


def _record_failure(key):
    with _lock:
        _failures[key].append(time.monotonic())


def jellyfin_url():
    row = db.session.query(Integration).filter_by(kind="jellyfin").first()
    if row:
        return row.base_url
    return os.getenv("CURATARR_JELLYFIN_URL") or None


def needs_server_url():
    """True only on first run, before any Jellyfin server is known."""
    return not current_app.config["DEMO_MODE"] and jellyfin_url() is None


def _device_id():
    device = setting("auth_device_id")
    if not device:
        device = str(uuid4())
        set_setting("auth_device_id", device)
        db.session.commit()
    return device


def _is_admin(user):
    policy = user.get("Policy") if isinstance(user, dict) else None
    return (
        isinstance(policy, dict)
        and policy.get("IsAdministrator") is True
        and not policy.get("IsDisabled")
    )


def sign_in(username, password, remote_addr, server_url=None):
    if _rate_limited(remote_addr):
        raise SignInError("Too many failed sign-in attempts. Try again in 15 minutes.")
    demo = current_app.config["DEMO_MODE"]
    url = jellyfin_url()
    if url is None and not demo:
        try:
            url = normalized_url(server_url or "")
        except ValueError as exc:
            raise SignInError("Enter your Jellyfin server URL.") from exc
    if demo:
        from .demo import demo_client

        jellyfin = demo_client("jellyfin")
    else:
        # Sign-in uses the person's own credentials, never the stored API key.
        jellyfin = JellyfinClient(url, "")
    device = _device_id()
    try:
        result = jellyfin.authenticate(username, password, device)
    except IntegrationError as exc:
        _record_failure(remote_addr)
        log_operation("sign_in", "rejected", status_code=exc.status_code)
        if exc.status_code in {400, 401, 403}:
            raise SignInError(
                "Jellyfin did not accept that username and password."
            ) from exc
        raise SignInError("Jellyfin could not be reached.") from exc
    user = result.get("User") if isinstance(result, dict) else None
    token = result.get("AccessToken") if isinstance(result, dict) else None
    if token:
        try:
            jellyfin.end_session(token, device)
        except IntegrationError:
            log_operation("sign_in_end_session", "error")
    if not isinstance(user, dict) or not user.get("Id"):
        raise SignInError("Jellyfin returned an invalid sign-in response.")
    if not _is_admin(user):
        log_operation("sign_in", "not_administrator")
        raise SignInError(ADMIN_REQUIRED)
    if (
        not demo
        and not db.session.query(Integration).filter_by(kind="jellyfin").first()
    ):
        # First run: bind Curatarr to the server this administrator used.
        db.session.add(
            Integration(kind="jellyfin", base_url=url, health_state="unconfigured")
        )
    name = str(user.get("Name") or "Jellyfin administrator")
    audit(
        "admin_signed_in",
        None,
        f"{name} signed in with a Jellyfin administrator account.",
        f"sign-in:{user['Id']}:{utcnow().isoformat()}",
    )
    db.session.commit()
    with _lock:
        _failures.pop(remote_addr, None)
    session.clear()
    session.permanent = True
    session["user"] = {
        "id": str(user["Id"]),
        "name": name,
        "verified_at": utcnow().isoformat(),
    }
    log_operation("sign_in", "ok")
    return session["user"]


def sign_out():
    session.clear()


def current_user():
    """The signed-in administrator, re-checked with Jellyfin periodically."""
    data = session.get("user")
    if not isinstance(data, dict) or not data.get("id"):
        return None
    now = utcnow()
    verified = _as_datetime(data.get("verified_at"))
    if verified and (now - verified).total_seconds() < REVALIDATE_SECONDS:
        return data
    try:
        user = client("jellyfin").user(data["id"])
    except IntegrationError as exc:
        if exc.status_code != 404 and verified and now - verified < OUTAGE_GRACE:
            return data
        log_operation("session_revalidate", "signed_out", status_code=exc.status_code)
        sign_out()
        return None
    if not _is_admin(user):
        log_operation("session_revalidate", "not_administrator")
        sign_out()
        return None
    data = data | {"name": str(user.get("Name") or data["name"])}
    data["verified_at"] = now.isoformat()
    session["user"] = data
    return data
