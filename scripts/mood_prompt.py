#!/usr/bin/env python3
"""Ask for today's mood with a native macOS dialog when it has not been logged.

Run by the ``com.healthhub.mood-prompt`` launchd agent at 18:20 and 21:30. Each
run checks today's logging status first and exits silently when today's mood
date already has a rating. Otherwise it shows a dialog with a text field:

    Mood and energy today? Two numbers 1 to 10, feeling then energy, e.g. 7 6.
    Optional note after.

The answer (``7 6``, ``7,6``, ``7/6``, optionally followed by a note) is saved
through ``scripts/log_mood.py``, the same locked write path the chat logger
uses (source ``grokbot``), and a notification confirms the date, the scores,
and the model-ready count. Bad input re-prompts up to three times. A busy
warehouse is retried with a short backoff before a failure notification.

Safety: the dialog text and the answer travel through ``osascript`` argv and
never into AppleScript source. Each run prints one JSON line to the launchd log
with a status only: no scores and no note text.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import log_mood  # noqa: E402


OSASCRIPT = "/usr/bin/osascript"
AFPLAY = "/usr/bin/afplay"
ALERT_SOUND = "/System/Library/Sounds/Glass.aiff"
TITLE = "Health Data Hub"
PROMPT_TEXT = (
    "Mood and energy today? Two numbers 1 to 10, feeling then energy, e.g. 7 6. Optional note after."
)
RETRY_PREFIX = "That did not look like two numbers from 1 to 10. "
DIALOG_WAIT_SECONDS = 1800
MAX_TRIES = 3
SAVE_BACKOFF_SECONDS = (5, 15, 30)
STATUS_BACKOFF_SECONDS = (5, 15)

# Values reach AppleScript only as ``argv`` items, never as source text.
DIALOG_SCRIPT = (
    "on run argv",
    "set promptText to item 1 of argv",
    "set waitSeconds to (item 2 of argv) as integer",
    "activate",
    'set r to display dialog promptText default answer "" buttons {"Later", "Log"} '
    'default button "Log" with title "Health Data Hub" with icon note giving up after waitSeconds',
    "return ((gave up of r) as text) & linefeed & (button returned of r) & linefeed & (text returned of r)",
    "end run",
)
NOTIFY_SCRIPT = (
    "on run argv",
    'display notification (item 1 of argv) with title "Health Data Hub"',
    "end run",
)

_ANSWER = re.compile(r"^\s*(\d{1,2})\s*[\s,/]\s*(\d{1,2})(?:(?:\s*[,/]\s*|\s+)(.*))?\s*$", re.DOTALL)

Runner = Callable[..., subprocess.CompletedProcess]
LogCall = Callable[[list[str]], tuple[int, dict[str, Any]]]


class ParseError(ValueError):
    """The answer was not two ratings from 1 to 10."""


@dataclass(frozen=True)
class Answer:
    feeling: int
    energy: int
    note: str | None


@dataclass(frozen=True)
class DialogResult:
    button: str
    text: str
    gave_up: bool
    error: str | None = None


def parse_answer(text: str) -> Answer:
    """Parse ``F E [note]`` with spaces, commas, or a slash between the numbers."""

    match = _ANSWER.match(text or "")
    if not match:
        raise ParseError("expected two numbers")
    feeling, energy = int(match.group(1)), int(match.group(2))
    for value in (feeling, energy):
        if not log_mood.RATING_MIN <= value <= log_mood.RATING_MAX:
            raise ParseError("rating out of range")
    note = (match.group(3) or "").strip() or None
    return Answer(feeling=feeling, energy=energy, note=note)


def _osascript(lines: Sequence[str], args: Sequence[str]) -> list[str]:
    command = [OSASCRIPT]
    for line in lines:
        command += ["-e", line]
    return [*command, *args]


def show_dialog(prompt: str, *, wait_seconds: int = DIALOG_WAIT_SECONDS, runner: Runner = subprocess.run) -> DialogResult:
    try:
        result = runner(
            _osascript(DIALOG_SCRIPT, [prompt, str(wait_seconds)]),
            check=False,
            capture_output=True,
            text=True,
            timeout=wait_seconds + 120,
        )
    except Exception:
        return DialogResult(button="", text="", gave_up=False, error="dialog could not run")
    if result.returncode != 0:
        # osascript error numbers (for example -1743 or -1713) are safe to log.
        code = re.search(r"\((-?\d+)\)\s*$", (result.stderr or "").strip())
        return DialogResult(button="", text="", gave_up=False, error=f"osascript error {code.group(1) if code else result.returncode}")
    gave_up, button, text = (result.stdout.rstrip("\n").split("\n", 2) + ["", "", ""])[:3]
    return DialogResult(button=button, text=text, gave_up=gave_up.strip() == "true")


def notify(message: str, *, runner: Runner = subprocess.run) -> None:
    try:
        runner(_osascript(NOTIFY_SCRIPT, [message]), check=False, capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def play_sound(*, popen: Callable[..., Any] = subprocess.Popen) -> None:
    try:
        popen([AFPLAY, ALERT_SOUND], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def default_log_call(args: list[str]) -> tuple[int, dict[str, Any]]:
    return log_mood.run(args)


def _short_date(value: str) -> str:
    day = date.fromisoformat(value)
    return f"{day:%b} {day.day}"


def confirmation_text(payload: dict[str, Any], answer: Answer) -> str:
    text = f"Logged {_short_date(payload['mood_date'])}: mood {answer.feeling}, energy {answer.energy}."
    ready = payload.get("model_ready_days")
    if ready is not None:
        text += f" Model-ready {ready}/{payload.get('model_ready_target', log_mood.MIN_MODEL_ROWS_FOR_GATE)}."
    return text


def _status(log_call: LogCall, sleep: Callable[[float], None]) -> tuple[int, dict[str, Any]]:
    code, payload = log_call(["--status"])
    for delay in STATUS_BACKOFF_SECONDS:
        if code == 0:
            break
        sleep(delay)
        code, payload = log_call(["--status"])
    return code, payload


def _save(answer: Answer, log_call: LogCall, sleep: Callable[[float], None]) -> tuple[int, dict[str, Any], int]:
    args = ["--feeling", str(answer.feeling), "--energy", str(answer.energy)]
    if answer.note:
        # ``--notes=`` keeps a note that starts with "-" from being read as a flag.
        args.append(f"--notes={answer.note}")
    attempts = 1
    code, payload = log_call(args)
    for delay in SAVE_BACKOFF_SECONDS:
        if code != 1:
            break
        sleep(delay)
        attempts += 1
        code, payload = log_call(args)
    return code, payload, attempts


def prompt_once(
    *,
    log_call: LogCall = default_log_call,
    dialog: Callable[[str], DialogResult] = show_dialog,
    notifier: Callable[[str], None] = notify,
    sound: Callable[[], None] = play_sound,
    sleep: Callable[[float], None] = time.sleep,
    force: bool = False,
) -> dict[str, Any]:
    """One scheduled check: ask only if today is not logged, then save and confirm."""

    status_code, status = _status(log_call, sleep)
    if status_code == 0 and status.get("today_logged") and not force:
        return {"result": "already_logged", "mood_date": status.get("today_mood_date")}
    # If the status read keeps failing, asking is safer than skipping the day.
    sound()
    prompt = PROMPT_TEXT
    for attempt in range(1, MAX_TRIES + 1):
        reply = dialog(prompt)
        if reply.error:
            return {"result": "dialog_error", "error": reply.error, "status_ok": status_code == 0}
        if reply.gave_up:
            return {"result": "timed_out", "tries": attempt}
        if reply.button != "Log":
            return {"result": "later", "tries": attempt}
        try:
            answer = parse_answer(reply.text)
        except ParseError:
            prompt = RETRY_PREFIX + PROMPT_TEXT
            continue
        code, payload, saves = _save(answer, log_call, sleep)
        if code == 0:
            notifier(confirmation_text(payload, answer))
            return {
                "result": "logged",
                "mood_date": payload.get("mood_date"),
                "model_ready_days": payload.get("model_ready_days"),
                "save_attempts": saves,
            }
        notifier("Mood not saved: the warehouse was busy or could not be written. Please log it in the mood form.")
        return {"result": "save_failed", "error": payload.get("error"), "save_attempts": saves}
    notifier("Mood not logged: the answer needs two numbers from 1 to 10, like 7 6.")
    return {"result": "invalid_input", "tries": MAX_TRIES}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ask for today's mood if it has not been logged.")
    parser.add_argument("--force", action="store_true", help="Ask even if today is already logged.")
    args = parser.parse_args(argv)
    try:
        outcome = prompt_once(force=args.force)
    except Exception:
        outcome = {"result": "error", "error": "unexpected failure"}
    outcome = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), **outcome}
    print(json.dumps(outcome, sort_keys=True, separators=(",", ":")))
    return 1 if outcome["result"] in {"save_failed", "dialog_error", "error"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
