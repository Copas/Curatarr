"""The worker's settings: a JSON file only its owner can read, beside the
token it signs in with."""

import json
import os
import stat
from pathlib import Path

HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
FOLDER = HOME / "curatarr-artwork-worker"
CONFIG = FOLDER / "config.json"
TOKEN = FOLDER / "token"

DEFAULTS = {
    "server": "",
    "name": "",
    "slots": 1,  # pictures made at once
    "poll_seconds": 10,
    "work_dir": str(Path.home() / ".cache" / "curatarr-artwork-worker"),
    "backends": {
        "comfyui": {
            "enabled": False,
            "url": "http://127.0.0.1:8188",
            # Started (and stopped when idle) by the worker when given:
            "folder": "",
            "python": "",
            "stop_after_idle": 300,
            "min_free_vram_mb": 6000,
        },
        "codex": {
            "enabled": False,
            "executable": "codex",
            "model": "",
            "timeout": 900,
        },
        "mock": {"enabled": False},
    },
    "checks": {
        "python": "",  # an environment with rapidocr_onnxruntime and insightface
        "insightface_root": "",
        "title_retries": 2,  # more tries when the title is misspelled
    },
}


def _merge(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged[key] = _merge(base[key], value)
        else:
            merged[key] = value
    return merged


def load(path: Path | None = None) -> dict:
    path = Path(path or os.environ.get("CURATARR_WORKER_CONFIG") or CONFIG)
    try:
        saved = json.loads(path.read_text())
    except FileNotFoundError:
        saved = {}
    config = _merge(DEFAULTS, saved)
    config["_path"] = str(path)
    return config


def save(config: dict, path: Path | None = None) -> Path:
    path = Path(path or config.get("_path") or CONFIG)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in config.items() if not k.startswith("_")}
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def token(config: dict) -> str:
    """The token, from CURATARR_WORKER_TOKEN or the token file."""
    if os.environ.get("CURATARR_WORKER_TOKEN"):
        return os.environ["CURATARR_WORKER_TOKEN"].strip()
    path = Path(config.get("token_file") or TOKEN)
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


def save_token(value: str, config: dict) -> Path:
    path = Path(config.get("token_file") or TOKEN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    path.write_text(value.strip() + "\n")
    return path
