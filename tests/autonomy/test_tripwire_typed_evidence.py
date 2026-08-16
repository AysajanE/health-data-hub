from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from scripts.evaluate_tripwires import evidence_status, evaluate_tripwires
from scripts.tripwire_evidence import (
    BASELINE_GATE_KIND,
    BASELINE_TRIPWIRE,
    COMPLIANCE_TRIPWIRE,
    MOOD_COMPLIANCE_KIND,
    MOOD_TRANSPORT_KIND,
    TRANSPORT_TRIPWIRE,
    build_baseline_gate_report,
    build_mood_compliance_report,
    build_mood_transport_report,
    validate_typed_tripwire_report,
)


CREATED_AT = datetime(2026, 1, 1, 12, tzinfo=UTC)


def validate(kind: str, tripwire: str, payload: dict[str, object], *, as_of: date | None = None) -> dict:
    return validate_typed_tripwire_report(kind, payload, expected_tripwire=tripwire, as_of=as_of)


def compliance_rows(start: date, count: int, *, source: str = "ios_shortcut") -> list[dict[str, str]]:
    return [
        {"mood_date": (start + timedelta(days=index)).isoformat(), "source": source}
        for index in range(count)
    ]


class MoodTransportEvidenceTests(unittest.TestCase):
    def test_requires_exactly_seven_boolean_opportunities(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly seven"):
            build_mood_transport_report(
                [True] * 6,
                window_start="2026-01-01",
                window_end="2026-01-07",
            )
        with self.assertRaisesRegex(ValueError, "fallback_verified"):
            build_mood_transport_report(
                [True] * 7,
                window_start="2026-01-01",
                window_end="2026-01-07",
                fallback_verified=1,  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "exactly seven"):
            build_mood_transport_report(
                [True, True, True, True, True, True, 1],  # type: ignore[list-item]
                window_start="2026-01-01",
                window_end="2026-01-07",
            )

    def test_five_failures_do_not_fire_but_six_require_the_fallback(self) -> None:
        passing = build_mood_transport_report(
            [False] * 5 + [True] * 2,
            window_start="2026-01-01",
            window_end="2026-01-07",
            created_at=CREATED_AT,
        )
        triggered = build_mood_transport_report(
            [False] * 6 + [True],
            window_start="2026-01-01",
            window_end="2026-01-07",
            created_at=CREATED_AT,
        )
        recovered = build_mood_transport_report(
            [False] * 6 + [True],
            window_start="2026-01-01",
            window_end="2026-01-07",
            fallback_verified=True,
            created_at=CREATED_AT,
        )

        self.assertEqual(passing["status"], "ok")
        self.assertEqual(triggered["status"], "triggered")
        self.assertEqual(triggered["action"], "streamlit_mobile_form")
        self.assertEqual(recovered["status"], "fallback_accepted")
        self.assertTrue(validate(MOOD_TRANSPORT_KIND, TRANSPORT_TRIPWIRE, passing)["ok"])
        self.assertFalse(validate(MOOD_TRANSPORT_KIND, TRANSPORT_TRIPWIRE, triggered)["ok"])
        self.assertTrue(validate(MOOD_TRANSPORT_KIND, TRANSPORT_TRIPWIRE, recovered)["ok"])

    def test_cross_field_tampering_is_invalid(self) -> None:
        report = build_mood_transport_report(
            [False] * 6 + [True],
            window_start="2026-01-01",
            window_end="2026-01-07",
            created_at=CREATED_AT,
        )
        report["status"] = "ok"
        result = validate(MOOD_TRANSPORT_KIND, TRANSPORT_TRIPWIRE, report)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("status must be triggered" in error for error in result["errors"]))


class MoodComplianceEvidenceTests(unittest.TestCase):
    def test_before_activation_plus_28_days_is_not_due(self) -> None:
        activation = date(2026, 1, 1)
        as_of = activation + timedelta(days=27)
        report = build_mood_compliance_report(
            compliance_rows(activation, 27),
            activation_date=activation,
            as_of_date=as_of,
            created_at=CREATED_AT,
        )

        self.assertEqual(report["status"], "not_due")
        self.assertEqual(report["opportunities"], 27)
        self.assertTrue(validate(MOOD_COMPLIANCE_KIND, COMPLIANCE_TRIPWIRE, report, as_of=as_of)["ok"])

    def test_due_window_requires_23_unique_non_backfill_days(self) -> None:
        activation = date(2026, 1, 1)
        as_of = activation + timedelta(days=28)
        rows = compliance_rows(activation, 23)
        rows.append({"mood_date": activation.isoformat(), "source": "manual"})
        rows.append({"mood_date": (activation + timedelta(days=23)).isoformat(), "source": "backfill"})
        report = build_mood_compliance_report(
            rows,
            activation_date=activation,
            as_of_date=as_of,
            created_at=CREATED_AT,
        )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["eligible_unique_non_backfill_days"], 23)
        self.assertEqual(report["duplicate_rows_ignored"], 1)
        self.assertEqual(report["backfill_rows_excluded"], 1)
        self.assertFalse(report["contains_ratings"])
        self.assertFalse(report["contains_notes"])
        self.assertTrue(validate(MOOD_COMPLIANCE_KIND, COMPLIANCE_TRIPWIRE, report, as_of=as_of)["ok"])

        failed = build_mood_compliance_report(
            compliance_rows(activation, 22),
            activation_date=activation,
            as_of_date=as_of,
            created_at=CREATED_AT,
        )
        self.assertEqual(failed["status"], "triggered")
        self.assertEqual(failed["action"], "stop_modeling_fix_logging")
        self.assertFalse(validate(MOOD_COMPLIANCE_KIND, COMPLIANCE_TRIPWIRE, failed, as_of=as_of)["ok"])

    def test_compliance_uses_the_trailing_28_completed_local_dates(self) -> None:
        activation = date(2026, 1, 1)
        as_of = activation + timedelta(days=35)
        trailing_start = as_of - timedelta(days=28)
        report = build_mood_compliance_report(
            compliance_rows(trailing_start, 23),
            activation_date=activation,
            as_of_date=as_of,
            created_at=CREATED_AT,
        )

        self.assertEqual(report["window_start"], trailing_start.isoformat())
        self.assertEqual(report["window_end"], (as_of - timedelta(days=1)).isoformat())
        self.assertEqual(report["status"], "ok")

    def test_rows_with_ratings_notes_or_extra_health_fields_are_rejected(self) -> None:
        for forbidden in ("feeling", "rating", "notes"):
            with self.subTest(forbidden=forbidden), self.assertRaisesRegex(ValueError, "forbidden"):
                build_mood_compliance_report(
                    [{"mood_date": "2026-01-01", "source": "manual", forbidden: "redacted"}],
                    activation_date="2026-01-01",
                    as_of_date="2026-01-29",
                )

    def test_missing_or_unknown_source_cannot_be_claimed_as_non_backfill(self) -> None:
        for row in (
            {"mood_date": "2026-01-01"},
            {"mood_date": "2026-01-01", "source": None},
            {"mood_date": "2026-01-01", "source": "unknown"},
        ):
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, "source"):
                build_mood_compliance_report(
                    [row],
                    activation_date="2026-01-01",
                    as_of_date="2026-01-29",
                )

    def test_stale_or_semantically_tampered_report_is_invalid(self) -> None:
        activation = date(2026, 1, 1)
        report_as_of = activation + timedelta(days=28)
        report = build_mood_compliance_report(
            compliance_rows(activation, 23),
            activation_date=activation,
            as_of_date=report_as_of,
            created_at=CREATED_AT,
        )
        report["status"] = "triggered"
        result = validate(MOOD_COMPLIANCE_KIND, COMPLIANCE_TRIPWIRE, report, as_of=report_as_of)
        self.assertEqual(result["status"], "invalid")

        fresh_report = build_mood_compliance_report(
            compliance_rows(activation, 23),
            activation_date=activation,
            as_of_date=report_as_of,
            created_at=CREATED_AT,
        )
        stale = validate(
            MOOD_COMPLIANCE_KIND,
            COMPLIANCE_TRIPWIRE,
            fresh_report,
            as_of=report_as_of + timedelta(days=1),
        )
        self.assertEqual(stale["status"], "invalid")
        self.assertTrue(any("evaluator date" in error for error in stale["errors"]))

    def test_hidden_sensitive_fields_are_rejected_before_evaluator_metadata_is_removed(self) -> None:
        activation = date(2026, 1, 1)
        as_of = activation + timedelta(days=28)
        report = build_mood_compliance_report(
            compliance_rows(activation, 23),
            activation_date=activation,
            as_of_date=as_of,
            created_at=CREATED_AT,
        )
        report["_report_path"] = "/private/evidence/report.json"
        self.assertTrue(validate(MOOD_COMPLIANCE_KIND, COMPLIANCE_TRIPWIRE, report, as_of=as_of)["ok"])

        for hidden_field in ("_feeling", "_notes", "_raw_payload"):
            with self.subTest(hidden_field=hidden_field):
                tampered = {**report, hidden_field: "must-not-be-hidden"}
                result = validate(MOOD_COMPLIANCE_KIND, COMPLIANCE_TRIPWIRE, tampered, as_of=as_of)
                self.assertEqual(result["status"], "invalid")
                self.assertFalse(result["ok"])
                self.assertTrue(any(hidden_field in error for error in result["errors"]), result)


class BaselineGateEvidenceTests(unittest.TestCase):
    def test_missing_ineligible_and_failed_runtime_gates_use_collecting_state(self) -> None:
        for runtime_status in ("missing", "ineligible", "failed"):
            with self.subTest(runtime_status=runtime_status):
                report = build_baseline_gate_report(
                    runtime_status,
                    collecting_state_enforced=True,
                    created_at=CREATED_AT,
                )
                self.assertEqual(report["status"], "fallback_accepted")
                self.assertEqual(report["action"], "collecting_state_no_override")
                self.assertTrue(validate(BASELINE_GATE_KIND, BASELINE_TRIPWIRE, report)["ok"])

    def test_unenforced_collecting_state_is_required_and_never_a_model_pass(self) -> None:
        report = build_baseline_gate_report(
            "missing",
            collecting_state_enforced=False,
            created_at=CREATED_AT,
        )
        self.assertEqual(report["status"], "fallback_required")
        self.assertEqual(report["action"], "collecting_state_no_override")
        self.assertFalse(validate(BASELINE_GATE_KIND, BASELINE_TRIPWIRE, report)["ok"])

        report["status"] = "ok"
        tampered = validate(BASELINE_GATE_KIND, BASELINE_TRIPWIRE, report)
        self.assertEqual(tampered["status"], "invalid")

    def test_runtime_pass_is_the_only_model_pass(self) -> None:
        report = build_baseline_gate_report(
            "passed",
            collecting_state_enforced=False,
            created_at=CREATED_AT,
        )
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["action"], "none")
        self.assertFalse(report["collecting_state_enforced"])

        with self.assertRaisesRegex(ValueError, "collecting_state_enforced"):
            build_baseline_gate_report("missing", collecting_state_enforced=1)  # type: ignore[arg-type]


class TypedTripwireEvaluatorTests(unittest.TestCase):
    @staticmethod
    def write_policy(root: Path, *, tripwire: str, evidence: str, deadline: date) -> None:
        policy = root / "ops/autonomy/policy.yaml"
        policy.parent.mkdir(parents=True)
        policy.write_text(
            "tripwires:\n"
            "  apply_design_doc_tripwires: true\n"
            f"  {tripwire}: configured_action\n"
            "tripwire_deadlines:\n"
            f"  {tripwire}:\n"
            f"    date: {deadline.isoformat()}\n"
            "    action: configured_action\n"
            f"    evidence: {evidence}\n",
            encoding="utf-8",
        )

    def test_generic_provider_status_contract_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_dir = root / "private/evidence/provider"
            report_dir.mkdir(parents=True)
            report_file = report_dir / "latest.json"
            report_file.write_text(json.dumps({"status": "fallback_accepted"}), encoding="utf-8")

            result = evidence_status(root, "private/evidence/provider")
            self.assertEqual(result["status"], "fallback_accepted")
            self.assertTrue(result["ok"])

    def test_markdown_marker_cannot_satisfy_typed_compliance_gate(self) -> None:
        as_of = date(2026, 2, 1)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence = "docs/reviews/proxy.md"
            self.write_policy(root, tripwire=COMPLIANCE_TRIPWIRE, evidence=evidence, deadline=as_of)
            proxy = root / evidence
            proxy.parent.mkdir(parents=True)
            proxy.write_text("autonomous_gate_review\nVerdict: pass\n", encoding="utf-8")

            report = evaluate_tripwires(root, as_of=as_of)

            self.assertEqual(report["status"], "error")
            evidence_result = report["fired"][0]["evidence_status"]
            self.assertEqual(evidence_result["kind"], MOOD_COMPLIANCE_KIND)
            self.assertEqual(evidence_result["status"], "missing")
            self.assertFalse(evidence_result["ok"])

    def test_valid_not_due_compliance_evidence_does_not_fire(self) -> None:
        activation = date(2026, 1, 10)
        as_of = activation + timedelta(days=20)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence = "docs/evidence/mood_compliance"
            self.write_policy(root, tripwire=COMPLIANCE_TRIPWIRE, evidence=evidence, deadline=as_of)
            report_dir = root / evidence
            report_dir.mkdir(parents=True)
            payload = build_mood_compliance_report(
                compliance_rows(activation, 20),
                activation_date=activation,
                as_of_date=as_of,
                created_at=CREATED_AT,
            )
            (report_dir / "report.json").write_text(json.dumps(payload), encoding="utf-8")

            report = evaluate_tripwires(root, as_of=as_of)

            self.assertEqual(report["status"], "ok", report)
            self.assertEqual(report["fired"], [])

    def test_missing_baseline_evidence_explicitly_requires_collecting_state(self) -> None:
        as_of = date(2026, 2, 1)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence = "docs/evidence/baseline_gate"
            self.write_policy(root, tripwire=BASELINE_TRIPWIRE, evidence=evidence, deadline=as_of)

            report = evaluate_tripwires(root, as_of=as_of)

            self.assertEqual(report["status"], "error")
            evidence_result = report["fired"][0]["evidence_status"]
            self.assertEqual(evidence_result["status"], "fallback_required")
            self.assertEqual(evidence_result["runtime_gate_status"], "missing")
            self.assertEqual(evidence_result["action"], "collecting_state_no_override")
            self.assertFalse(evidence_result["ok"])

    def test_independent_typed_baseline_evidence_can_verify_collecting_state(self) -> None:
        as_of = date(2026, 2, 1)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence = "private/evidence/S05/baseline_gate"
            self.write_policy(root, tripwire=BASELINE_TRIPWIRE, evidence=evidence, deadline=as_of)
            report_dir = root / evidence
            report_dir.mkdir(parents=True)
            payload = build_baseline_gate_report(
                "missing",
                collecting_state_enforced=True,
                created_at=CREATED_AT,
            )
            (report_dir / "externally-verified.json").write_text(json.dumps(payload), encoding="utf-8")

            report = evaluate_tripwires(root, as_of=as_of)

            self.assertEqual(report["status"], "ok", report)
            self.assertEqual(report["fired"], [])


if __name__ == "__main__":
    unittest.main()
