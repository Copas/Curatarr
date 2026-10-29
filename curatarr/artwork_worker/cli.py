"""curatarr-artwork-worker: setup, check, run, make-test-people."""

import argparse
import getpass
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config as settings
from .backends import BACKENDS, Reference
from .client import Unauthorised
from .runner import Worker

COMFY_PLACES = ("~/ComfyUI", "~/comfyui", "/opt/ComfyUI")
CHECKS_ENV = Path.home() / ".local" / "share" / "curatarr-artwork-worker" / "checks"
CHECKS_PACKAGES = ("rapidocr_onnxruntime", "insightface", "opencv-python-headless")


def _find_comfy(given: str | None) -> Path | None:
    for place in [given] if given else COMFY_PLACES:
        folder = Path(place).expanduser()
        if (folder / "main.py").is_file():
            return folder
    return None


def _comfy_python(folder: Path) -> str:
    for sub in (".venv/bin/python", "venv/bin/python"):
        if (folder / sub).is_file():
            return str(folder / sub)
    return sys.executable


def _install_checks(env: Path) -> str:
    """A small environment for the checks (text recognition and face
    recognition), apart from ComfyUI's and Curatarr's."""
    python = env / "bin" / "python"
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(env)], check=True)
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", *CHECKS_PACKAGES], check=True
    )
    return str(python)


def setup(args) -> int:
    config = settings.load(args.config)
    if args.server:
        config["server"] = args.server.rstrip("/")
    if not config["server"]:
        print("Give Curatarr's address with --server (as you open it in a browser).")
        return 2
    token = os.environ.get("CURATARR_WORKER_TOKEN") or (
        sys.stdin.readline().strip()
        if args.token_stdin
        else getpass.getpass("The worker's token (from Posters → Workers): ")
    )
    if not token.startswith("cwk_"):
        print("That isn't a worker token: copy it from Posters → Workers.")
        return 2
    comfy = _find_comfy(args.comfyui)
    backends = config["backends"]
    if comfy:
        backends["comfyui"] |= {
            "enabled": True,
            "folder": str(comfy),
            "python": _comfy_python(comfy),
        }
        print(f"ComfyUI: {comfy}")
        root = comfy / "models" / "insightface"
        if root.is_dir() and not config["checks"].get("insightface_root"):
            config["checks"]["insightface_root"] = str(root)
    else:
        print("ComfyUI: not found (give its folder with --comfyui to use local models)")
    if shutil.which("codex") and not args.no_codex:
        backends["codex"]["enabled"] = True
        # Its full path: a service doesn't get the PATH of a terminal.
        backends["codex"]["executable"] = shutil.which("codex")
        print("Codex: found (it sends prompts and their pictures to OpenAI)")
    if args.install_checks:
        print("Installing the checks (text and face recognition, about 600 MB)…")
        config["checks"]["python"] = _install_checks(Path(args.install_checks))
    elif (CHECKS_ENV / "bin" / "python").is_file():
        config["checks"]["python"] = str(CHECKS_ENV / "bin" / "python")
    if not config["checks"].get("python"):
        print(
            "Checks: not installed, so titles and faces aren't checked (add "
            f"--install-checks {CHECKS_ENV} to install them)."
        )
    settings.save_token(token, config)
    path = settings.save(config)
    print(f"Saved {path} (the token beside it, readable only by you).")
    return check(args)


def check(args) -> int:
    config = settings.load(args.config)
    token = settings.token(config)
    if not config["server"] or not token:
        print("Run `curatarr-artwork-worker setup` first.")
        return 2
    worker = Worker(config, token)
    for engine in worker.engines():
        state = "ready" if engine.get("ready") else f"not ready: {engine.get('why')}"
        place = "sends to an outside service" if engine.get("external") else "local"
        print(f"  {engine['label']} [{engine['engine']}] ({place}): {state}")
    if not worker.backends:
        print("  No backends are turned on.")
    try:
        worker.hello()
    except Unauthorised as exc:
        print(f"Curatarr: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001  any failure is reported
        print(f"Curatarr: couldn't reach {config['server']}: {exc}")
        return 1
    print(f"Curatarr: connected to {config['server']}.")
    return 0


def run(args) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    config = settings.load(args.config)
    token = settings.token(config)
    if not config["server"] or not token:
        print("Run `curatarr-artwork-worker setup` first.")
        return 2
    Worker(config, token).run()
    return 0


TEST_PEOPLE = {
    "Alex": "a fictional 45-year-old man with a long narrow face, deep-set grey "
    "eyes, a crooked nose, a short salt-and-pepper beard and receding curly dark hair",
    "Sam": "a fictional 30-year-old woman with a round face, wide-set brown eyes, "
    "freckles, a short platinum-blonde pixie cut and a small gold nose ring",
}
TEST_SHOTS = (
    (
        "Passport-style colour photograph of {who}. Neutral expression, plain "
        "light grey background, even studio lighting, sharp focus, realistic skin."
    ),
    (
        "The same person as in image 1, photographed outdoors in warm "
        "late-afternoon sun, three-quarter view, slight smile. Keep the face "
        "exactly the same."
    ),
    (
        "The same person as in image 1, photographed at night by a window lit by "
        "blue neon, looking up and to the side, laughing. Keep the face exactly "
        "the same."
    ),
)


def make_test_people(args) -> int:
    """Photos of made-up people, for benchmarks without anyone's real photos."""
    config = settings.load(args.config)
    options = config["backends"]["comfyui"]
    if not options.get("enabled"):
        print("This needs ComfyUI: run setup with --comfyui first.")
        return 2
    folder = Path(args.folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    work = Path(config["work_dir"]).expanduser()
    work.mkdir(parents=True, exist_ok=True)
    backend = BACKENDS["comfyui"](options, work)

    def quiet(*_args, **_kwargs):
        return None

    try:
        for number, (name, who) in enumerate(TEST_PEOPLE.items()):
            first = None
            for shot, words in enumerate(TEST_SHOTS, 1):
                task = {
                    "image": f"test-{name.lower()}-{shot}",
                    "engine": "comfyui/flux2-klein-4b",
                    "kind": "Primary",
                    "prompt": words.format(who=who),
                    "seed": 1000 + number * 10 + shot,
                }
                refs = [Reference(1, first, "person", name)] if first else []
                made = backend.make(task, refs, quiet)
                path = folder / f"{name} {shot}.png"
                path.write_bytes(made.data)
                first = first or path
                print(f"  {path}")
    finally:
        backend.free()
        backend.close()
    print(json.dumps({"folder": str(folder), "people": list(TEST_PEOPLE)}))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="curatarr-artwork-worker",
        description="Makes the pictures Curatarr's Posters → Generate asks for.",
    )
    parser.add_argument("--config", help="settings file (default: ~/.config/…)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("setup", help="connect to Curatarr and find ComfyUI and Codex")
    p.add_argument("--server", help="Curatarr's address, e.g. http://192.0.2.10:8787")
    p.add_argument("--name", help="(the name is set in Curatarr)")
    p.add_argument("--comfyui", help="ComfyUI's folder")
    p.add_argument("--no-codex", action="store_true", help="don't offer Codex")
    p.add_argument(
        "--token-stdin", action="store_true", help="read the token from stdin"
    )
    p.add_argument(
        "--install-checks",
        metavar="FOLDER",
        nargs="?",
        const=str(CHECKS_ENV),
        help="install text and face recognition for the checks",
    )
    p.set_defaults(func=setup)
    p = sub.add_parser("check", help="show what this worker can make")
    p.set_defaults(func=check)
    p = sub.add_parser("run", help="make pictures until stopped")
    p.set_defaults(func=run)
    p = sub.add_parser(
        "make-test-people", help="photos of made-up people, for benchmarks"
    )
    p.add_argument("folder")
    p.set_defaults(func=make_test_people)
    args = parser.parse_args(argv)
    return args.func(args)
