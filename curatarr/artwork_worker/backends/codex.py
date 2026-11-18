"""Codex CLI's image tool, signed in with the computer's own ChatGPT login.

Codex is an agent, so it's run locked down: its shell, browser, apps and
plugins are turned off, its sandbox is read-only, its working folder is an
empty one, the user's Codex settings and rules aren't loaded, and the prompt
goes in on stdin with a fixed argument list (never through a shell). All it
can do is use its image tool, whose pictures Codex itself saves under
``$CODEX_HOME/generated_images/<thread>/``, where the worker collects them.
The prompt and its reference pictures go to OpenAI: Curatarr only sends
reference photos of people to this engine when that's been allowed.

Codex's image allowance is part of the ChatGPT plan. When it's used up Codex
still exits normally, so the worker reads the error, says when it resets, and
Curatarr waits until then rather than counting a failed try.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .base import Backend, Failed, Made

KINDS = ["Primary", "Thumb", "Backdrop", "Logo", "Banner"]
# Everything but the image tool, turned off.
DISABLED = (
    "shell_tool",
    "unified_exec",
    "apps",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "in_app_browser",
    "plugins",
    "remote_plugin",
    "hooks",
)
# The image tool's safety system turning a picture down (the server tries an
# original take once, rather than the same prompt again).
SAFETY = re.compile(
    r"safety system|content polic|moderation|policy violation|"
    r"rejected the generation",
    re.IGNORECASE,
)
LIMIT = re.compile(
    r'"type"\s*:\s*"usage_limit_reached".*?"resets_in_seconds"\s*:\s*(\d+)'
)
# The plan's limit as Codex words it, in an error event or its own reply:
# "You've hit your usage limit ... try again at 6:09 PM", "its usage limit
# was reached. It resets in about 14 minutes."
LIMIT_SAID = re.compile(r"usage limit|rate limit", re.IGNORECASE)
LIMIT_AT = re.compile(r"try again at (\d{1,2}):(\d{2})\s*([AP]M)?", re.IGNORECASE)
# …or with the day, when it's not today: "try again at Nov 18th, 2026 2:31 PM".
LIMIT_ON = re.compile(
    r"try again at ([A-Za-z]{3,9})\.? (\d{1,2})(?:st|nd|rd|th)?,? (?:(\d{4}),? )?"
    r"(\d{1,2}):(\d{2})\s*([AP]M)?",
    re.IGNORECASE,
)
MONTHS = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)
LIMIT_IN = re.compile(
    r"(?:resets|try again) in (?:about )?(\d+)\s*(minute|hour|day)", re.IGNORECASE
)
LIMIT_GUESS = 3600  # seconds to wait when it doesn't say when
LIMIT_SOON = 120  # the least wait, when the time it named has just passed
LIMIT_GRACE = 3 * 3600  # a time named this far in the past means tomorrow's


def _hour(hour: int, half: str | None) -> int:
    """A clock hour, 12-hour ("2" with "PM") or 24-hour, as 0-23."""
    hour %= 24
    half = (half or "").upper()
    if half == "PM" and hour < 12:
        return hour + 12
    if half == "AM" and hour == 12:
        return 0
    return hour


def _reset_in(said: str, now: float | None = None) -> int:
    """Seconds until a usage limit Codex described resets."""
    now = time.time() if now is None else now
    found = LIMIT_IN.search(said)
    if found:
        unit = {"minute": 60, "hour": 3600, "day": 86400}[found.group(2).lower()]
        return int(found.group(1)) * unit
    found = LIMIT_ON.search(said)
    if found and found.group(1)[:3].lower() in MONTHS:
        local = time.localtime(now)
        month = MONTHS.index(found.group(1)[:3].lower()) + 1
        year = int(found.group(3) or local.tm_year)
        hour = _hour(int(found.group(4)), found.group(6))
        at = time.mktime(
            (year, month, int(found.group(2)), hour, int(found.group(5)), 0, 0, 0, -1)
        )
        return max(LIMIT_SOON, int(at - now))
    found = LIMIT_AT.search(said)
    if found:
        hour, minute = _hour(int(found.group(1)), found.group(3)), int(found.group(2))
        local = time.localtime(now)
        at = time.mktime(
            (*local[:3], hour, minute, 0, local.tm_wday, local.tm_yday, -1)
        )
        if at <= now - LIMIT_GRACE:  # well past: it's tomorrow's
            at += 86400
        # Just past: asked again in the minute it named, before it reset.
        return max(LIMIT_SOON, int(at - now))
    return LIMIT_GUESS


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


class CodexBackend(Backend):
    name = "codex"

    def __init__(self, options, work_dir):
        super().__init__(options, work_dir)
        self._status = None
        self._status_at = 0.0
        self._limit_file = self.work_dir / "codex-limit.json"
        try:
            self._limited = float(json.loads(self._limit_file.read_text())["until"])
        except (OSError, ValueError, KeyError, TypeError):
            self._limited = 0.0

    @property
    def limited_until(self) -> float:
        return self._limited

    @limited_until.setter
    def limited_until(self, value: float) -> None:
        """Kept on disk, so a restarted worker still waits for the reset."""
        self._limited = value
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            self._limit_file.write_text(json.dumps({"until": value}))
        except OSError:
            pass

    @property
    def executable(self) -> str | None:
        return shutil.which(self.options.get("executable") or "codex")

    def _run(self, *args, timeout=30) -> str:
        done = subprocess.run(
            [self.executable, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        return (done.stdout or "") + (done.stderr or "")

    def status(self) -> tuple[bool, str]:
        """Whether Codex can make pictures now, and why not. Signed-in state
        is read from ``codex login status``, which never shows the token."""
        now = time.time()
        if self.limited_until > now:
            hours = (self.limited_until - now) / 3600
            return False, (
                f"The ChatGPT plan's usage limit was reached; it resets in about "
                f"{hours:.1f} hours."
            )
        if self._status and now - self._status_at < 300:
            return self._status
        if not self.executable:
            result = (False, "Codex CLI isn't installed (or not on the PATH).")
        else:
            try:
                login = self._run("login", "status")
                features = self._run("features", "list")
            except (OSError, subprocess.SubprocessError) as exc:
                result = (False, f"Codex didn't answer: {exc}")
            else:
                if "Logged in" not in login:
                    result = (False, "Codex isn't signed in: run `codex login` here.")
                elif not re.search(
                    r"^image_generation\s+\S+\s+true", features, re.MULTILINE
                ):
                    result = (
                        False,
                        (
                            "This Codex has no image tool (its image_generation "
                            "feature is off or missing)."
                        ),
                    )
                else:
                    result = (True, "")
        self._status, self._status_at = result, now
        return result

    def engines(self):
        ready, why = self.status()
        return [
            {
                "engine": "codex/image",
                "label": "Codex image tool (ChatGPT)",
                "kinds": KINDS,
                "references": True,
                "external": True,
                "ready": ready,
                "why": why,
                # So Curatarr can say when it resets, and count down to it.
                "limited_until": self.limited_until
                if self.limited_until > time.time()
                else None,
            }
        ]

    def command(self, folder: Path, images: list[Path]) -> list[str]:
        """The fixed argument list: nothing from the prompt goes in it."""
        args = [self.executable, "exec"]
        for image in images:
            args += ["-i", str(image)]
        args += [
            "--json",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "-s",
            "read-only",
            "-C",
            str(folder),
        ]
        for feature in DISABLED:
            args += ["--disable", feature]
        if self.options.get("model"):
            args += ["-m", str(self.options["model"])]
        return args

    def make(self, task, references, progress):
        ready, why = self.status()
        if not ready:
            code = "usage_limit" if self.limited_until > time.time() else "unavailable"
            raise Failed(why, code=code, retry_after=self._wait())
        folder = self.work_dir / "codex" / task["image"]
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        images = []
        for ref in references:
            copy = folder / f"image-{ref.number}{ref.path.suffix.lower() or '.png'}"
            shutil.copyfile(ref.path, copy)
            images.append(copy)
        started = time.time()
        timeout = float(self.options.get("timeout") or 900)
        progress(10, "Asking Codex")
        process = subprocess.Popen(
            self.command(folder, images),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=folder,
        )
        errors: list[str] = []
        reader = threading.Thread(
            target=lambda: errors.append(process.stderr.read()), daemon=True
        )
        reader.start()
        # Reading its output only notices the time when a line arrives; a
        # Codex that hangs without a word would never time out. The timer
        # ends it, which ends the read below.
        timed_out = threading.Event()

        def give_up():
            timed_out.set()
            process.kill()

        timer = threading.Timer(timeout, give_up)
        timer.daemon = True
        timer.start()
        thread_id, last_message, failure = None, "", ""
        try:
            process.stdin.write(task["prompt"])
            process.stdin.close()
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "thread.started":
                    thread_id = event.get("thread_id")
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    last_message = item.get("text") or last_message
                if event.get("type") == "error":  # it says why it stopped
                    failure = str(event.get("message") or "") or failure
                elif event.get("type") == "turn.failed":
                    failure = str((event.get("error") or {}).get("message") or "")
                    failure = failure or "its turn failed"
                elapsed = time.time() - started
                progress(min(90, 10 + int(elapsed / 3)), "Codex is making it")
                if elapsed > timeout:
                    raise Failed(f"Codex took longer than {timeout:.0f} seconds.")
            process.wait(timeout=max(1, timeout - (time.time() - started)))
            if timed_out.is_set():
                raise Failed(f"Codex took longer than {timeout:.0f} seconds.")
        except subprocess.TimeoutExpired as exc:
            raise Failed(f"Codex took longer than {timeout:.0f} seconds.") from exc
        except BrokenPipeError as exc:  # it ended before reading the prompt
            if timed_out.is_set():
                raise Failed(f"Codex took longer than {timeout:.0f} seconds.") from exc
        finally:
            timer.cancel()
            if process.poll() is None:  # cancelled, failed or timed out
                process.kill()
            process.wait()
            reader.join(timeout=5)
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()
            shutil.rmtree(folder, ignore_errors=True)
        stderr = "".join(errors)
        limit = LIMIT.search(stderr)
        made = self._collect(thread_id, started)
        if made is None:
            said_limit = f"{failure} {last_message}"
            if limit or LIMIT_SAID.search(said_limit):
                wait = int(limit.group(1)) if limit else _reset_in(said_limit)
                self.limited_until = time.time() + wait
                self._status = None
                raise Failed(
                    "The ChatGPT plan's usage limit was reached; Curatarr will "
                    "try again when it resets.",
                    code="usage_limit",
                    retry_after=wait + 60,
                )
            if "401" in stderr or "Unauthorized" in stderr:
                self._status = None
                raise Failed(
                    "Codex's sign-in was refused: run `codex login`.",
                    code="unavailable",
                    retry_after=3600,
                )
            said = (
                failure.strip()[:300]
                or last_message.strip()[:300]
                or (stderr.strip().splitlines() or [""])[-1][:300]
            )
            code = "safety" if SAFETY.search(f"{said} {stderr}") else None
            raise Failed(
                f"Codex finished without making a picture. {said}".strip(), code=code
            )
        return Made(
            made.read_bytes(),
            {
                "engine": "codex/image",
                "model": self.options.get("model") or "Codex default",
                "thread": thread_id,
                "seconds": round(time.time() - started, 1),
            },
        )

    def _wait(self) -> int:
        return max(600, int(self.limited_until - time.time()) + 60)

    def _collect(self, thread_id: str | None, started: float) -> Path | None:
        """The newest picture Codex saved for this run."""
        if not thread_id:
            return None
        folder = _codex_home() / "generated_images" / thread_id
        try:
            found = [
                p
                for p in folder.iterdir()
                if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                and p.stat().st_mtime >= started - 5
            ]
        except OSError:
            return None
        return max(found, key=lambda p: p.stat().st_mtime) if found else None
