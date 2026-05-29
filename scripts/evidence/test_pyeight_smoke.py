from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts.evidence.pyeight_smoke import main


class PyEightSmokeCollectorTests(unittest.TestCase):
    def test_main_accept_fallback_records_decision_and_returns_success_when_env_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stdout = StringIO()
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("network should not run")),
                redirect_stdout(stdout),
            ):
                exit_code = main(["--root", str(root), "--accept-fallback", "--json"])

            report = json.loads(stdout.getvalue())
            evidence_path = root / str(report["evidence"])
            evidence_payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence_mode = stat.S_IMODE(evidence_path.stat().st_mode)
            decisions = sorted((root / "ops/autonomy/decisions").glob("S03-pyeight-fallback-*.json"))
            self.assertEqual(len(decisions), 1)
            decision_payload = json.loads(decisions[0].read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "fallback_accepted")
        self.assertEqual(report["errors"], [])
        self.assertEqual(evidence_payload["status"], "fallback_accepted")
        self.assertEqual(evidence_payload["fallback"], "oura_only_v1")
        self.assertEqual(
            evidence_payload["decision"],
            str(decisions[0].relative_to(root)),
        )
        self.assertEqual(evidence_mode, 0o600)
        self.assertEqual(decision_payload["status"], "fallback_accepted")
        self.assertEqual(decision_payload["slice"], "S03")
        self.assertEqual(decision_payload["tripwire"], "pyeight_smoke_failure")
        self.assertEqual(decision_payload["action"], "oura_only_v1")


if __name__ == "__main__":
    unittest.main()
