"""The local language-model step of Smart Chapters (Ollama).

Ollama sees only short transcript excerpts: the stretch around each allowed
boundary window, and then the opening lines of each chapter for naming. It
never sees file paths or anything outside the transcript.
"""

import json
import re
import time

import requests

from . import gpu, throttle
from .chapters import Candidate, Window, timestamp


class LanguageModelError(RuntimeError):
    pass


class LanguageModelUnavailable(LanguageModelError):
    """Ollama did not answer (down, overloaded, or out of GPU memory).

    Unlike a bad answer, this is usually temporary: the job waits and tries
    again rather than settling for Navigation chapters.
    """


# Waits before the second and third try of a request that failed.
RETRY_DELAYS = (10, 30)


SYSTEM = (
    "You help divide films and TV episodes into chapters for viewers. "
    "Answer only with the JSON requested. Do not invent events that are not in "
    "the transcript."
)


class Ollama:
    def __init__(self, base_url: str, model: str, session=None, timeout=180):
        if not model:
            raise LanguageModelError("No Ollama model is configured")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.session = session or requests.Session()
        self.timeout = timeout
        self.calls = 0

    def models(self) -> list[str]:
        try:
            response = self.session.get(f"{self.base_url}/api/tags", timeout=10)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise LanguageModelError("Ollama is not reachable") from exc
        return [m.get("name") for m in response.json().get("models", [])]

    def unload(self):
        """Free the model's GPU memory now instead of after keep_alive."""
        try:
            self.session.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model, "keep_alive": 0},
                timeout=30,
            )
        except requests.RequestException:
            pass  # If Ollama is down its memory is free anyway.

    def ask(self, prompt: str, schema: dict) -> dict:
        for delay in (*RETRY_DELAYS, None):
            try:
                return self._ask_once(prompt, schema)
            except LanguageModelUnavailable:
                if delay is None:
                    raise
                time.sleep(delay)
        raise AssertionError("unreachable")

    def _ask_once(self, prompt: str, schema: dict) -> dict:
        throttle.wait_while_paused()
        self.calls += 1
        try:
            with gpu.shared():
                response = self.session.post(
                    f"{self.base_url}/api/chat",
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": SYSTEM},
                            {"role": "user", "content": prompt},
                        ],
                        "format": schema,
                        "stream": False,
                        "think": False,
                        "keep_alive": "2m",
                        "options": {"temperature": 0.2},
                    },
                    timeout=self.timeout,
                )
            if 400 <= response.status_code < 500:
                # A missing model or a bad request will not fix itself.
                raise LanguageModelError(
                    f"Ollama refused the request ({response.status_code})"
                )
            response.raise_for_status()
            content = response.json()["message"]["content"]
        except requests.RequestException as exc:
            raise LanguageModelUnavailable("Ollama request failed") from exc
        except (KeyError, ValueError) as exc:
            raise LanguageModelError("Ollama returned an unexpected response") from exc
        # Some models still wrap JSON in reasoning text or code fences.
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if not match:
            raise LanguageModelError("Ollama returned no JSON")
        try:
            return json.loads(match.group(0))
        except ValueError as exc:
            raise LanguageModelError("Ollama returned invalid JSON") from exc


CHOICE_SCHEMA = {
    "type": "object",
    "properties": {"choice": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["choice"],
}


def letter(index: int) -> str:
    return chr(ord("A") + index)


def choose_boundary(llm: Ollama, window: Window, candidates: list[Candidate], excerpt):
    """Index of the candidate where the story or topic most clearly moves on."""
    options = "\n".join(
        f"{letter(i)}: {timestamp(c.time)[:8]} ({c.label})"
        for i, c in enumerate(candidates)
    )
    prompt = (
        "Below is part of a transcript with timestamps, and candidate times where "
        "a new chapter could begin. Pick the candidate where the scene, story or "
        "topic most clearly changes (a new location, new characters, a time jump, "
        "or a new subject). Prefer a candidate near "
        f"{timestamp(window.target)[:8]} when the choices are equally good.\n\n"
        f"Candidates:\n{options}\n\nTranscript:\n{excerpt}\n\n"
        'Reply as {"choice": "<letter>", "reason": "<a few words>"}.'
    )
    answer = llm.ask(prompt, CHOICE_SCHEMA)
    choice = str(answer.get("choice", "")).strip().upper()[:1]
    if not choice or not "A" <= choice <= letter(len(candidates) - 1):
        return None
    return ord(choice) - ord("A")


NAME_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "name": {"type": "string"}},
    "required": ["summary", "name"],
}


def name_chapters(
    llm: Ollama, excerpts: list[tuple[float, str]], context=""
) -> list[str]:
    """One concise name per chapter, from what is said in it.

    Each chapter is named on its own, after a one-sentence summary: naming them
    all in one request produced little more than character names.
    """
    names: list[str] = []
    about = f" of {context}" if context else ""
    for start, text in excerpts:
        earlier = (
            f" Earlier chapters are named: {', '.join(names)}; do not repeat them."
            if names
            else ""
        )
        prompt = (
            f"This is the dialogue from one chapter{about}, starting at "
            f"{timestamp(start)[:8]}.\n"
            "First write one sentence summarizing what happens. Then name the "
            "chapter in 2 to 5 words, title case, describing the event or "
            'situation like a DVD chapter menu ("Arrival at the House", "The '
            "Investigation Begins\"). The name must not be only a person's name, "
            "and must not reveal how the story ends."
            f"{earlier}\n\nDialogue:\n{text or '(no dialogue)'}\n\n"
            'Reply as {"summary": "...", "name": "..."}.'
        )
        try:
            answer = llm.ask(prompt, NAME_SCHEMA)
            names.append(str(answer.get("name", "")))
        except LanguageModelError:
            names.append("")  # this chapter falls back to its numbered name
    return names


RECAP_LINE_SCHEMA = {
    "type": "object",
    "properties": {"last_recap_line": {"type": "integer"}},
    "required": ["last_recap_line"],
}


def choose_recap_line(llm: Ollama, lines) -> int | None:
    """Index of the recap's last line among lines that may run into the episode.

    The first line is the last clip the recap quotes from an earlier episode, so
    it's always part of the recap; the narrator's closing words usually follow.
    """
    numbered = "\n".join(
        f"{i}. [{timestamp(s.start)[3:8]}] {s.text.strip()}"
        for i, s in enumerate(lines)
    )
    prompt = (
        'These lines come from the end of a TV episode\'s "previously on" recap and '
        "may continue into the new episode. The first line is still part of the "
        "recap. The recap ends with the narrator's closing words; after that, people "
        "talk in a new scene with no narrator summarizing. Give the number of the "
        f"LAST line that is part of the recap.\n\n{numbered}\n\n"
        'Reply as {"last_recap_line": <number>}.'
    )
    answer = llm.ask(prompt, RECAP_LINE_SCHEMA)
    index = answer.get("last_recap_line")
    return index if isinstance(index, int) and 0 <= index < len(lines) else None
