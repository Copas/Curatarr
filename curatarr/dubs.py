"""Checking that films made in another language really have English audio.

Radarr can grab an English dub of a foreign-language film, but uploads are
sometimes labelled English when the audio is the original. For films whose
original language isn't English, each audio track is sampled at a few points
mid-film (mostly dialogue) and Whisper says which language it hears. That
decides the fix: make a real English track the default, correct a track's
wrong language label, or flag the file as having no English audio so it can
be replaced.

Only films made in another language are checked: running this on everything
mostly measures music, which Whisper often mistakes for another language.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from . import gpu, throttle
from .mediafile import MediaInfo, MediaToolError, extract_audio

CLIPS = 5  # samples per track, spread over the middle of the film
CLIP_SECONDS = 20
POSITIONS = (0.2, 0.35, 0.5, 0.65, 0.8)
CONFIDENT = 0.6  # a sample counts only when Whisper is at least this sure
MIN_CONFIDENT = 3  # and a track's language needs this many such samples
AGREEMENT = 0.6  # of which this share must agree
MAX_TRACKS = 4  # commentary and extra dubs past this aren't worth the time
# A commentary talks over the whole film, quiet scenes and music included; the
# film's own soundtrack, dubbed or not, has stretches with little said. An
# English track counts as a commentary when there's speech in at least
# COMMENTARY_EVERY of every clip, and it says COMMENTARY_MORE more on average
# than the film's other audio. (Measured: commentaries 0.88 to 1.0 in every
# clip; films, talky ones too, under 0.5 in at least one.)
COMMENTARY_EVERY = 0.5
COMMENTARY_MORE = 0.15
# Named or flagged as a commentary or an audio description.
EXTRA_NAME = re.compile(
    r"comment|descri|narrat|visually impaired|\bad\b|\bdvs\b", re.IGNORECASE
)

# Matroska language codes (ISO 639-2/B) and Radarr's names, by ISO 639-1.
LANGUAGES = {
    "en": ("eng", "English"),
    "fr": ("fre", "French"),
    "de": ("ger", "German"),
    "es": ("spa", "Spanish"),
    "it": ("ita", "Italian"),
    "pt": ("por", "Portuguese"),
    "nl": ("dut", "Dutch"),
    "da": ("dan", "Danish"),
    "sv": ("swe", "Swedish"),
    "no": ("nor", "Norwegian"),
    "fi": ("fin", "Finnish"),
    "is": ("ice", "Icelandic"),
    "pl": ("pol", "Polish"),
    "cs": ("cze", "Czech"),
    "hu": ("hun", "Hungarian"),
    "ro": ("rum", "Romanian"),
    "el": ("gre", "Greek"),
    "ru": ("rus", "Russian"),
    "uk": ("ukr", "Ukrainian"),
    "tr": ("tur", "Turkish"),
    "ar": ("ara", "Arabic"),
    "he": ("heb", "Hebrew"),
    "fa": ("per", "Persian"),
    "hi": ("hin", "Hindi"),
    "ta": ("tam", "Tamil"),
    "te": ("tel", "Telugu"),
    "th": ("tha", "Thai"),
    "vi": ("vie", "Vietnamese"),
    "id": ("ind", "Indonesian"),
    "ja": ("jpn", "Japanese"),
    "ko": ("kor", "Korean"),
    "zh": ("chi", "Chinese"),
    "sr": ("srp", "Serbian"),
    "hr": ("hrv", "Croatian"),
    "bs": ("bos", "Bosnian"),
    "bg": ("bul", "Bulgarian"),
    "sk": ("slo", "Slovak"),
    "sl": ("slv", "Slovenian"),
    "mk": ("mac", "Macedonian"),
    "et": ("est", "Estonian"),
    "lv": ("lav", "Latvian"),
    "lt": ("lit", "Lithuanian"),
    "ms": ("may", "Malay"),
    "tl": ("tgl", "Tagalog"),
}
# Other spellings found in files' language tags (639-2/T codes, names).
_ALIASES = {
    "slk": "sk",
    "mkd": "mk",
    "msa": "ms",
    "scc": "sr",
    "scr": "hr",
    "fra": "fr",
    "deu": "de",
    "nld": "nl",
    "ces": "cs",
    "ron": "ro",
    "ell": "el",
    "fas": "fa",
    "isl": "is",
    "zho": "zh",
    "cmn": "zh",
    "yue": "zh",
    "nob": "no",
    "nno": "no",
}


def code(value) -> str | None:
    """A language tag, code or name as ISO 639-1, or None if unknown/undetermined."""
    text = str(value or "").strip().lower()
    if not text or text in {"und", "unk", "mul", "zxx", "mis"}:
        return None
    text = text.split("-")[0]  # IETF tags such as en-US
    if text in LANGUAGES:
        return text
    if text in _ALIASES:
        return _ALIASES[text]
    for short, (three, name) in LANGUAGES.items():
        if text in (three, name.lower()):
            return short
    return None


def name(short: str | None) -> str:
    return LANGUAGES[short][1] if short in LANGUAGES else (short or "unknown")


def heard(samples: list) -> str | None:
    """The language a track is in, from (language, probability) samples.

    None when the samples are too few or too unsure to say (silence, music,
    or a mix).
    """
    confident = [lang for lang, probability in samples if probability >= CONFIDENT]
    if len(confident) < MIN_CONFIDENT:
        return None
    language, count = Counter(confident).most_common(1)[0]
    return language if count / len(confident) >= AGREEMENT else None


def _mean(values) -> float:
    return sum(values) / len(values) if values else 0.0


def commentary(speech: dict, track: int, others) -> bool:
    """Whether ``track`` talks like a commentary next to the ``others``:
    ``speech`` is {track: [share of each clip with speech in it]}."""
    mine = speech.get(track) or []
    theirs = [speech.get(o) for o in others if speech.get(o)]
    if len(mine) < 3 or not theirs:
        return False
    if min(mine) < COMMENTARY_EVERY:
        return False
    return _mean(mine) >= max(_mean(t) for t in theirs) + COMMENTARY_MORE


def _unanimous(samples, language) -> bool:
    """No sample confidently in a language other than ``language``."""
    return not any(
        lang != language and chance >= CONFIDENT for lang, chance in samples or []
    )


def plan(streams: list[dict], heard_by_track: dict, speech=None, samples=None) -> dict:
    """What to do about a file's audio, given what each track sounded like.

    ``verdict`` is "english" (a real English track exists), "no_english"
    (every track was heard clearly and none is English) or "unsure".
    ``actions`` are ("label", track, language) for a wrong or missing label
    and ("default", track) to make a real English track the default. An
    English track that talks like a commentary (``speech``, see commentary())
    isn't a real English track. With ``samples``, a track labelled as one
    language is taken to be in another, where English is one of them, only
    when no clip was confidently in a third: Whisper heard two clips of a
    Serbian soundtrack as English and one as Croatian, and the Serbian track
    was relabelled English.
    """
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    tracks, actions = [], []
    for stream in audio:
        index = stream["index"]
        label, sounds = code(stream.get("language")), heard_by_track.get(index)
        if (
            samples is not None
            and label
            and sounds
            and sounds != label
            and "en" in (label, sounds)
            and not _unanimous(samples.get(index), sounds)
        ):
            sounds = None  # not clear enough to say the label is wrong
        tracks.append(
            {
                "index": index,
                "label": label,
                "heard": sounds,
                "default": bool(stream.get("default")),
            }
        )
        # Relabel only where English is involved (the case that matters) or
        # the label is missing; one foreign label for another isn't worth
        # trusting a short sample over.
        if (
            sounds
            and sounds in LANGUAGES
            and sounds != label
            and (label is None or "en" in (label, sounds))
        ):
            actions.append(("label", index, sounds))
    speech = speech or {}
    named = {
        s["index"]
        for s in audio
        if s.get("extra") or EXTRA_NAME.search(s.get("title") or "")
    }
    for track in tracks:
        others = [t["index"] for t in tracks if t["heard"] and t["heard"] != "en"]
        if track["heard"] == "en" and (
            track["index"] in named or commentary(speech, track["index"], others)
        ):
            track["commentary"] = True
    english = [
        t["index"] for t in tracks if t["heard"] == "en" and not t.get("commentary")
    ]
    if english:
        verdict = "english"
        default = next((t for t in tracks if t["default"]), None)
        # Switch only away from a default clearly in another language: one
        # that was just unclear may be the main English mix, and the other
        # English track a commentary.
        if default is None or (default["heard"] and default["heard"] != "en"):
            actions.append(("default", english[0]))
    elif tracks and all(t["heard"] for t in tracks):
        verdict = "no_english"
    else:
        verdict = "unsure"
    return {"verdict": verdict, "tracks": tracks, "actions": actions}


def removable(result: dict, streams: list[dict]) -> dict:
    """Tracks in other languages that can go, with a real English track kept.

    Audio goes only when it was heard clearly in another language; a track
    that couldn't be made out stays. Subtitles go by their label (they can't
    be listened to); English and unlabelled ones stay.
    """
    if result["verdict"] != "english":
        return {"audio": [], "subtitles": []}
    audio = [t["index"] for t in result["tracks"] if t["heard"] and t["heard"] != "en"]
    subtitles = [
        s["index"]
        for s in streams
        if s.get("codec_type") == "subtitle"
        and code(s.get("language")) not in (None, "en")
    ]
    return {"audio": audio, "subtitles": subtitles}


def sample_times(duration: float) -> list[float]:
    return [round(duration * p, 1) for p in POSITIONS[:CLIPS]]


def listen(
    info: MediaInfo,
    tracks: list[int],
    device: str,
    model: str,
    make_room=None,
    speech=None,
):
    """{track: [(language, probability), ...]} for each audio track. With a
    ``speech`` dict, it's filled with {track: [share of each clip spoken]}."""
    times = sample_times(info.duration)
    with tempfile.TemporaryDirectory(prefix="curatarr-dubs-") as directory:
        clips = {}
        for track in tracks:
            clips[track] = []
            for number, start in enumerate(times):
                target = str(Path(directory) / f"{track}-{number}.wav")
                extract_audio(
                    info.path,
                    target,
                    track,
                    timeout=300,
                    seconds=CLIP_SECONDS,
                    start=start,
                )
                clips[track].append(target)
        listing = Path(directory) / "clips.json"
        listing.write_text(json.dumps({str(k): v for k, v in clips.items()}))
        from .transcribe import _cuda_library_path

        env = os.environ | {"LD_LIBRARY_PATH": _cuda_library_path()}
        try:
            with gpu.exclusive():
                if make_room and device != "cpu":
                    make_room()
                result = throttle.run(
                    [
                        sys.executable,
                        "-m",
                        "curatarr.dubs",
                        "--clips",
                        str(listing),
                        "--model",
                        model,
                        "--device",
                        device,
                    ],
                    env=env,
                    timeout=900,
                )
        except subprocess.TimeoutExpired as exc:
            raise MediaToolError("Listening to the audio tracks timed out") from exc
    if result.returncode != 0:
        message = (result.stderr.strip().splitlines() or ["unknown error"])[-1]
        raise MediaToolError(f"Language detection failed: {message[:200]}")
    try:
        found = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise MediaToolError("Language detection produced no readable output") from exc
    if "samples" in found:
        if speech is not None:
            speech.update({int(k): v for k, v in found["speech"].items()})
        found = found["samples"]
    return {int(k): [tuple(s) for s in v] for k, v in found.items()}


def _detect(clips: dict, model_name: str, device: str) -> dict:
    from faster_whisper import WhisperModel, decode_audio

    attempts = (
        [("cuda", "float16"), ("cpu", "int8")]
        if device == "auto"
        else [(device, "float16" if device == "cuda" else "int8")]
    )
    last_error = None
    for name_, compute in attempts:
        try:
            model = WhisperModel(model_name, device=name_, compute_type=compute)
            break
        except (RuntimeError, ValueError, OSError) as exc:
            last_error = exc
    else:
        raise RuntimeError(f"faster-whisper could not run: {last_error}")
    from faster_whisper.vad import get_speech_timestamps

    found, speech = {}, {}
    for track, paths in clips.items():
        found[track], speech[track] = [], []
        for path in paths:
            try:
                audio = decode_audio(path)
                spoken = sum(
                    t["end"] - t["start"] for t in get_speech_timestamps(audio)
                )
                speech[track].append(round(spoken / max(len(audio), 1), 2))
            except (RuntimeError, ValueError, OSError):
                pass
            try:
                # The voice filter drops music and silence, which is what
                # Whisper most often mistakes for another language.
                language, probability, _all = model.detect_language(
                    audio=decode_audio(path), vad_filter=True
                )
            except (RuntimeError, ValueError, IndexError):
                continue  # nothing spoken in this clip
            found[track].append([language, round(float(probability), 3)])
    return {"samples": found, "speech": speech}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clips", required=True)
    parser.add_argument("--model", default="medium")
    parser.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    args = parser.parse_args(argv)
    clips = json.loads(Path(args.clips).read_text())
    print(json.dumps(_detect(clips, args.model, args.device)))


if __name__ == "__main__":
    main()
