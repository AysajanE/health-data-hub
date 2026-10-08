from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from scripts import mood_prompt
from scripts.log_mood import WAREHOUSE_BUSY, run
from scripts.mood_prompt import (
    DIALOG_SCRIPT,
    NOTIFY_SCRIPT,
    PROMPT_TEXT,
    RETRY_PREFIX,
    Answer,
    DialogResult,
    ParseError,
    notify,
    parse_answer,
    prompt_once,
    show_dialog,
)
from src.warehouse.warehouse import connect_duckdb


# 6:20 PM in Toronto on 2026-10-08 (EDT, UTC-4).
EVENING_UTC = datetime(2026, 10, 8, 22, 20, tzinfo=UTC)


def _log_call(tmp_path: Path):
    env = {"HOME_TIMEZONE": "America/Toronto", "HEALTH_HUB_DATABASE_PATH": str(tmp_path / "data" / "warehouse.duckdb")}
    calls: list[list[str]] = []

    def call(args: list[str]):
        calls.append(list(args))
        return run([*args, "--env-file", str(tmp_path / "no.env")], env=env, now_utc=EVENING_UTC)

    call.calls = calls  # type: ignore[attr-defined]
    return call


def _rows(tmp_path: Path) -> list[tuple]:
    conn = connect_duckdb(tmp_path / "data" / "warehouse.duckdb", read_only=True)
    try:
        return conn.execute(
            """
            SELECT CAST(c.mood_date AS VARCHAR), e.feeling, e.energy, e.notes, e.source
            FROM mood_current c JOIN mood_entries e ON e.log_id = c.log_id ORDER BY c.mood_date
            """
        ).fetchall()
    finally:
        conn.close()


class Dialogs:
    def __init__(self, *replies: DialogResult) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> DialogResult:
        self.prompts.append(prompt)
        return self.replies.pop(0)


def _quiet(**overrides):
    defaults = {"notifier": overrides.pop("notifier", lambda message: None), "sound": lambda: None, "sleep": lambda s: None}
    return {**defaults, **overrides}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7 6", Answer(7, 6, None)),
        ("7,6", Answer(7, 6, None)),
        ("7/6", Answer(7, 6, None)),
        ("  7 , 6  ", Answer(7, 6, None)),
        ("10 1 long day", Answer(10, 1, "long day")),
        ("7, 6, slept badly", Answer(7, 6, "slept badly")),
        ("7 6 -tired", Answer(7, 6, "-tired")),
    ],
)
def test_parse_accepts_two_ratings_and_an_optional_note(text: str, expected: Answer) -> None:
    assert parse_answer(text) == expected


@pytest.mark.parametrize("text", ["", "7", "76", "0 5", "7 11", "a b", "7 6x", "seven six"])
def test_parse_rejects_anything_but_two_ratings_from_1_to_10(text: str) -> None:
    with pytest.raises(ParseError):
        parse_answer(text)


def test_already_logged_day_exits_silently_without_a_dialog(tmp_path: Path) -> None:
    log_call = _log_call(tmp_path)
    assert log_call(["--feeling", "5", "--energy", "5"])[0] == 0

    def no_dialog(prompt: str) -> DialogResult:
        raise AssertionError("dialog must not open")

    sounds: list[int] = []
    outcome = prompt_once(log_call=log_call, dialog=no_dialog, sound=lambda: sounds.append(1), sleep=lambda s: None)

    assert outcome == {"result": "already_logged", "mood_date": "2026-10-08"}
    assert sounds == []
    assert len(_rows(tmp_path)) == 1


def test_answer_is_saved_through_log_mood_and_confirmed(tmp_path: Path) -> None:
    log_call = _log_call(tmp_path)
    messages: list[str] = []
    dialogs = Dialogs(DialogResult(button="Log", text="7 6 long day", gave_up=False))

    outcome = prompt_once(log_call=log_call, **_quiet(dialog=dialogs, notifier=messages.append))

    assert outcome == {"result": "logged", "mood_date": "2026-10-08", "model_ready_days": 0, "save_attempts": 1}
    assert dialogs.prompts == [PROMPT_TEXT]
    assert _rows(tmp_path) == [("2026-10-08", 7, 6, "long day", "grokbot")]
    assert messages == ["Logged Oct 8: mood 7, energy 6. Model-ready 0/37."]
    assert "long day" not in json.dumps(outcome)


def test_bad_input_reprompts_then_saves(tmp_path: Path) -> None:
    dialogs = Dialogs(
        DialogResult(button="Log", text="seven", gave_up=False),
        DialogResult(button="Log", text="8/4", gave_up=False),
    )
    outcome = prompt_once(log_call=_log_call(tmp_path), **_quiet(dialog=dialogs))

    assert outcome["result"] == "logged"
    assert dialogs.prompts == [PROMPT_TEXT, RETRY_PREFIX + PROMPT_TEXT]
    assert _rows(tmp_path) == [("2026-10-08", 8, 4, None, "grokbot")]


def test_three_bad_answers_give_up_without_writing(tmp_path: Path) -> None:
    messages: list[str] = []
    dialogs = Dialogs(*[DialogResult(button="Log", text="11 0", gave_up=False)] * 3)
    outcome = prompt_once(log_call=_log_call(tmp_path), **_quiet(dialog=dialogs, notifier=messages.append))

    assert outcome == {"result": "invalid_input", "tries": 3}
    assert len(dialogs.prompts) == 3
    assert len(messages) == 1
    assert not (tmp_path / "data" / "warehouse.duckdb").exists() or _rows(tmp_path) == []


@pytest.mark.parametrize(
    ("reply", "result"),
    [
        (DialogResult(button="Later", text="7 6", gave_up=False), "later"),
        (DialogResult(button="", text="", gave_up=True), "timed_out"),
        (DialogResult(button="", text="", gave_up=False, error="osascript error -1743"), "dialog_error"),
    ],
)
def test_later_timeout_or_dialog_error_writes_nothing(tmp_path: Path, reply: DialogResult, result: str) -> None:
    log_call = _log_call(tmp_path)
    outcome = prompt_once(log_call=log_call, **_quiet(dialog=Dialogs(reply)))

    assert outcome["result"] == result
    assert all(call == ["--status"] for call in log_call.calls)


def test_busy_warehouse_is_retried_with_backoff_then_saves(tmp_path: Path) -> None:
    real = _log_call(tmp_path)
    busy_left = [2]

    def flaky(args: list[str]):
        if args[0] != "--status" and busy_left[0]:
            busy_left[0] -= 1
            return 1, {"status": "error", "mode": "log", "error": WAREHOUSE_BUSY}
        return real(args)

    sleeps: list[float] = []
    outcome = prompt_once(
        log_call=flaky,
        dialog=Dialogs(DialogResult(button="Log", text="6 5", gave_up=False)),
        notifier=lambda message: None,
        sound=lambda: None,
        sleep=sleeps.append,
    )

    assert outcome["result"] == "logged"
    assert outcome["save_attempts"] == 3
    assert sleeps == [5, 15]
    assert _rows(tmp_path) == [("2026-10-08", 6, 5, None, "grokbot")]


def test_busy_warehouse_that_never_frees_notifies_failure() -> None:
    def always_busy(args: list[str]):
        if args[0] == "--status":
            return 0, {"status": "ok", "today_logged": False, "today_mood_date": "2026-10-08"}
        return 1, {"status": "error", "mode": "log", "error": WAREHOUSE_BUSY}

    messages: list[str] = []
    sleeps: list[float] = []
    outcome = prompt_once(
        log_call=always_busy,
        dialog=Dialogs(DialogResult(button="Log", text="6 5", gave_up=False)),
        notifier=messages.append,
        sound=lambda: None,
        sleep=sleeps.append,
    )

    assert outcome == {"result": "save_failed", "error": WAREHOUSE_BUSY, "save_attempts": 4}
    assert sleeps == [5, 15, 30]
    assert len(messages) == 1 and messages[0].startswith("Mood not saved")


def test_status_failure_still_asks() -> None:
    def status_busy(args: list[str]):
        if args[0] == "--status":
            return 1, {"status": "error", "error": WAREHOUSE_BUSY}
        return 0, {"status": "ok", "mood_date": "2026-10-08", "model_ready_days": None}

    messages: list[str] = []
    outcome = prompt_once(
        log_call=status_busy,
        **_quiet(dialog=Dialogs(DialogResult(button="Log", text="7 7", gave_up=False)), notifier=messages.append),
    )

    assert outcome["result"] == "logged"
    assert messages == ["Logged Oct 8: mood 7, energy 7."]


def test_dialog_and_notification_pass_text_only_through_argv() -> None:
    seen: list[list[str]] = []
    tricky = 'quote " and \\ and "& do shell script "echo pwned" &"'

    def runner(command, **kwargs):
        seen.append(command)
        return CompletedProcess(command, 0, stdout="false\nLog\n" + tricky + "\n", stderr="")

    reply = show_dialog(tricky, wait_seconds=5, runner=runner)
    notify(tricky, runner=runner)

    dialog_cmd, notify_cmd = seen
    assert dialog_cmd[0] == "/usr/bin/osascript"
    assert dialog_cmd[-2:] == [tricky, "5"]
    assert notify_cmd[-1] == tricky
    script_lines = [arg for i, arg in enumerate(dialog_cmd) if i > 0 and dialog_cmd[i - 1] == "-e"]
    assert script_lines == list(DIALOG_SCRIPT)
    assert all(tricky not in line for line in (*DIALOG_SCRIPT, *NOTIFY_SCRIPT))
    assert reply == DialogResult(button="Log", text=tricky, gave_up=False)


def test_osascript_failure_is_reported_with_its_error_number_only() -> None:
    def runner(command, **kwargs):
        return CompletedProcess(command, 1, stdout="", stderr="execution error: Not authorized to send Apple events. (-1743)\n")

    assert show_dialog("x", runner=runner).error == "osascript error -1743"


def test_user_facing_text_has_no_em_dashes() -> None:
    source = Path(mood_prompt.__file__).read_text()
    assert "\u2014" not in source
