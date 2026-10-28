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

from .base import Backend, Cancelled, Failed, Made

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
LIMIT = re.compile(
    r'"type"\s*:\s*"usage_limit_reached".*?"resets_in_seconds"\s*:\s*(\d+)'
)


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
                f"The ChatGPT plan's image allowance is used up; it resets in about "
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
        process.stdin.write(task["prompt"])
        process.stdin.close()
        thread_id, last_message = None, ""
        try:
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
                elapsed = time.time() - started
                progress(min(90, 10 + int(elapsed / 3)), "Codex is making it")
                if elapsed > timeout:
                    raise Failed(f"Codex took longer than {timeout:.0f} seconds.")
            process.wait(timeout=max(1, timeout - (time.time() - started)))
        except (Cancelled, Failed, subprocess.TimeoutExpired) as exc:
            process.kill()
            process.wait()
            if isinstance(exc, subprocess.TimeoutExpired):
                raise Failed(f"Codex took longer than {timeout:.0f} seconds.") from exc
            raise
        finally:
            reader.join(timeout=5)
            shutil.rmtree(folder, ignore_errors=True)
        stderr = "".join(errors)
        limit = LIMIT.search(stderr)
        made = self._collect(thread_id, started)
        if made is None:
            if limit:
                self.limited_until = time.time() + int(limit.group(1))
                self._status = None
                raise Failed(
                    "The ChatGPT plan's image allowance is used up; Curatarr will "
                    "try again when it resets.",
                    code="usage_limit",
                    retry_after=int(limit.group(1)) + 60,
                )
            if "401" in stderr or "Unauthorized" in stderr:
                self._status = None
                raise Failed(
                    "Codex's sign-in was refused: run `codex login`.",
                    code="unavailable",
                    retry_after=3600,
                )
            said = (
                last_message.strip()[:300]
                or (stderr.strip().splitlines() or [""])[-1][:300]
            )
            raise Failed(f"Codex finished without making a picture. {said}".strip())
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
