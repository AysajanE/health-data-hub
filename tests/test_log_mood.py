from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts import log_mood
from scripts.log_mood import CliSettings, UsageError, read_status, run
from scripts.retrain_model import load_verified_feature_rows
from src.warehouse.warehouse import connect_duckdb, persist_mood_entry_locked


HOME_TIMEZONE = "America/Toronto"
# 6:07 PM in Toronto on 2026-10-08 (EDT, UTC-4).
EVENING_UTC = datetime(2026, 10, 8, 22, 7, tzinfo=UTC)
FAKE_FORM_TOKEN = "fake-form-tok"


def _settings(tmp_path: Path) -> CliSettings:
    return CliSettings(home_timezone=ZoneInfo(HOME_TIMEZONE), database_path=tmp_path / "data" / "warehouse.duckdb")


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME_TIMEZONE": HOME_TIMEZONE,
        "HEALTH_HUB_DATABASE_PATH": str(tmp_path / "data" / "warehouse.duckdb"),
    }


def _cli(tmp_path: Path, *args: str, now_utc: datetime = EVENING_UTC) -> tuple[int, dict]:
    missing_env_file = tmp_path / "no.env"
    return run([*args, "--env-file", str(missing_env_file)], env=_env(tmp_path), now_utc=now_utc)


def _current_rows(database_path: Path) -> list[tuple]:
    conn = connect_duckdb(database_path, read_only=True)
    try:
        return conn.execute(
            """
            SELECT CAST(c.mood_date AS VARCHAR), e.feeling, e.energy, e.notes, e.source,
                   CAST(e.log_id AS VARCHAR), CAST(e.supersedes_log_id AS VARCHAR)
            FROM mood_current c JOIN mood_entries e ON e.log_id = c.log_id
            ORDER BY c.mood_date
            """
        ).fetchall()
    finally:
        conn.close()


def _entry_count(database_path: Path) -> int:
    conn = connect_duckdb(database_path, read_only=True)
    try:
        return conn.execute("SELECT count(*) FROM mood_entries").fetchone()[0]
    finally:
        conn.close()


def test_log_writes_grokbot_entry_that_becomes_current(tmp_path: Path) -> None:
    code, payload = _cli(tmp_path, "--feeling", "7", "--energy", "6", "--notes", "long day")

    assert code == 0
    assert payload == {
        "status": "ok",
        "mode": "log",
        "mood_date": "2026-10-08",
        "feeling": 7,
        "energy": 6,
        "replaced_previous": False,
        "model_ready_days": 0,
        "model_ready_target": 37,
    }
    rows = _current_rows(_settings(tmp_path).database_path)
    assert [(r[0], r[1], r[2], r[3], r[4], r[6]) for r in rows] == [
        ("2026-10-08", 7, 6, "long day", "grokbot", None)
    ]


def test_second_rating_replaces_form_entry_like_a_resubmission(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    form_row = persist_mood_entry_locked(
        settings.database_path,
        {
            "log_id": "00000000-0000-4000-8000-000000000001",
            "logged_at_utc": EVENING_UTC - timedelta(hours=1),
            "mood_date": date(2026, 10, 8),
            "feeling": 4,
            "energy": 3,
            "notes": None,
            "context_chips": (),
            "source": "manual",
            "supersedes_log_id": None,
        },
    )

    code, payload = _cli(tmp_path, "--feeling", "8", "--energy", "7")

    assert code == 0
    assert payload["replaced_previous"] is True
    rows = _current_rows(settings.database_path)
    assert len(rows) == 1
    mood_date, feeling, energy, _notes, source, _log_id, supersedes = rows[0]
    assert (mood_date, feeling, energy, source) == ("2026-10-08", 8, 7, "grokbot")
    assert supersedes == str(form_row.log_id)
    assert _entry_count(settings.database_path) == 2


@pytest.mark.parametrize(
    "args",
    [
        ("--feeling", "11", "--energy", "5"),
        ("--feeling", "0", "--energy", "5"),
        ("--feeling", "5", "--energy", "11"),
        ("--feeling", "5", "--energy", "0"),
        ("--energy", "5"),
    ],
)
def test_out_of_range_or_missing_feeling_is_rejected_without_writing(tmp_path: Path, args: tuple[str, ...]) -> None:
    code, payload = _cli(tmp_path, *args)

    assert code == 2
    assert payload["status"] == "error"
    assert not _settings(tmp_path).database_path.exists()


def test_energy_is_optional(tmp_path: Path) -> None:
    code, payload = _cli(tmp_path, "--feeling", "6")

    assert code == 0
    assert payload["energy"] is None


@pytest.mark.parametrize(
    ("now_utc", "expected_date"),
    [
        # 03:30 EDT on Oct 9 still counts for Oct 8.
        (datetime(2026, 10, 9, 7, 30, tzinfo=UTC), "2026-10-08"),
        # 04:30 EDT on Oct 9 counts for Oct 9.
        (datetime(2026, 10, 9, 8, 30, tzinfo=UTC), "2026-10-09"),
    ],
)
def test_mood_date_follows_the_4_am_rule(tmp_path: Path, now_utc: datetime, expected_date: str) -> None:
    code, payload = _cli(tmp_path, "--feeling", "5", "--energy", "5", now_utc=now_utc)

    assert code == 0
    assert payload["mood_date"] == expected_date
    assert _current_rows(_settings(tmp_path).database_path)[0][0] == expected_date

    status_code, status = _cli(tmp_path, "--status", now_utc=now_utc)
    assert status_code == 0
    assert status["today_mood_date"] == expected_date
    assert status["today_logged"] is True


def test_backfill_date_window(tmp_path: Path) -> None:
    code, payload = _cli(tmp_path, "--feeling", "6", "--energy", "5", "--date", "2026-10-06")
    assert code == 0
    assert payload["mood_date"] == "2026-10-06"

    for bad in ("2026-10-09", "2026-09-30", "10/06/2026"):
        code, payload = _cli(tmp_path, "--feeling", "6", "--date", bad)
        assert code == 2, bad
        assert payload["status"] == "error"
    assert _entry_count(_settings(tmp_path).database_path) == 1


def test_status_without_a_warehouse_reports_nothing_logged(tmp_path: Path) -> None:
    code, payload = _cli(tmp_path, "--status")

    assert code == 0
    assert payload == {
        "status": "ok",
        "mode": "status",
        "today_mood_date": "2026-10-08",
        "today_logged": False,
        "today_feeling": None,
        "today_energy": None,
        "latest_mood_date": None,
        "latest_feeling": None,
        "latest_energy": None,
        "days_logged": 0,
        "model_ready_days": 0,
        "model_ready_target": 37,
    }
    assert not _settings(tmp_path).database_path.exists()


def test_status_on_empty_day_then_filled_day(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert _cli(tmp_path, "--feeling", "6", "--energy", "4", "--date", "2026-10-07")[0] == 0

    code, empty = _cli(tmp_path, "--status")
    assert code == 0
    assert empty["today_logged"] is False
    assert empty["latest_mood_date"] == "2026-10-07"
    assert (empty["latest_feeling"], empty["latest_energy"]) == (6, 4)
    assert empty["days_logged"] == 1
    entries_before = _entry_count(settings.database_path)

    assert _cli(tmp_path, "--feeling", "8", "--energy", "7")[0] == 0
    code, filled = _cli(tmp_path, "--status")
    assert code == 0
    assert filled["today_logged"] is True
    assert (filled["today_feeling"], filled["today_energy"]) == (8, 7)
    assert filled["latest_mood_date"] == "2026-10-08"
    assert filled["days_logged"] == 2
    assert _entry_count(settings.database_path) == entries_before + 1


def test_status_rejects_rating_arguments(tmp_path: Path) -> None:
    code, payload = _cli(tmp_path, "--status", "--feeling", "5")
    assert code == 2
    assert payload["status"] == "error"


def test_model_ready_count_uses_the_retrain_loader(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert _cli(tmp_path, "--feeling", "6", "--energy", "5", "--date", "2026-10-07")[0] == 0
    computed = datetime(2026, 10, 8, 12, 0)
    conn = connect_duckdb(settings.database_path)
    try:
        for day in ("2026-10-07", "2026-10-08"):
            conn.execute(
                """
                INSERT INTO daily_features (
                    feature_date, total_sleep_min, hrv_z, deep_sleep_pct, prior_day_feeling,
                    hrv_avg_ms, hrv_z_method, feature_version, prior_day_feeling_imputed,
                    sleep_source_count, computed_at_utc
                ) VALUES (?, 420, 0.1, 0.2, 5, 40.0, 'prior_only', 'v1.0', FALSE, 1, ?)
                """,
                [day, computed],
            )
            conn.execute(
                """
                INSERT INTO sleep_merge_diagnostics (
                    sleep_date, oura_present, eight_present, hrv_merge_method, stage_source, computed_at_utc
                ) VALUES (?, TRUE, FALSE, 'oura_primary', 'oura', ?)
                """,
                [day, computed],
            )
    finally:
        conn.close()

    # Only Oct 7 has a rating, so only Oct 7 is model-ready.
    assert len(load_verified_feature_rows(settings.database_path)) == 1
    assert read_status(settings, now_utc=EVENING_UTC)["model_ready_days"] == 1

    code, payload = _cli(tmp_path, "--feeling", "7", "--energy", "6")
    assert code == 0
    assert payload["model_ready_days"] == 2


def test_settings_come_from_env_file_without_reading_the_form_token(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    monkeypatch.delenv("HOME_TIMEZONE", raising=False)
    monkeypatch.delenv("HEALTH_HUB_DATABASE_PATH", raising=False)
    database_path = tmp_path / "data" / "warehouse.duckdb"
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        f"MOOD_FORM_TOKEN={FAKE_FORM_TOKEN}\n"
        f"HOME_TIMEZONE={HOME_TIMEZONE}\n"
        f"HEALTH_HUB_DATABASE_PATH={database_path}\n",
        encoding="utf-8",
    )

    exit_code = log_mood.main(["--status", "--env-file", str(env_file)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert FAKE_FORM_TOKEN not in output
    lines = output.strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["mode"] == "status"
    settings = log_mood.load_settings({}, env_file)
    assert settings.database_path == database_path
    assert not hasattr(settings, "token")


def test_invalid_timezone_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        log_mood.load_settings({"HOME_TIMEZONE": "Not/AZone"}, tmp_path / "no.env")
