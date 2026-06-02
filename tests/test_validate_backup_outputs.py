from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate_backup_outputs.py"


def run_validator(*args: str) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(SCRIPT_PATH), *args]
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


class ValidateBackupOutputsTests(unittest.TestCase):
    def test_intune_validation_passes_with_required_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "tenant-state" / "intune"
            reports = base / "tenant-state" / "reports" / "intune"
            (root / "Device Configurations").mkdir(parents=True, exist_ok=True)
            reports.mkdir(parents=True, exist_ok=True)

            (root / "Device Configurations" / "policy__id.json").write_text(
                json.dumps({"id": "id-1", "displayName": "Policy"}) + "\n",
                encoding="utf-8",
            )
            (reports / "policy-assignments.md").write_text("# report\n", encoding="utf-8")
            (reports / "policy-assignments.csv").write_text("a,b\n", encoding="utf-8")
            (reports / "object-inventory-all.csv").write_text("a,b\n", encoding="utf-8")

            result = run_validator(
                "--workload",
                "intune",
                "--mode",
                "light",
                "--root",
                str(root),
                "--reports-root",
                str(reports),
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    def test_intune_validation_fails_when_assignment_csv_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "tenant-state" / "intune"
            reports = base / "tenant-state" / "reports" / "intune"
            (root / "Device Configurations").mkdir(parents=True, exist_ok=True)
            reports.mkdir(parents=True, exist_ok=True)

            (root / "Device Configurations" / "policy__id.json").write_text("{}", encoding="utf-8")
            (reports / "policy-assignments.md").write_text("# report\n", encoding="utf-8")
            (reports / "object-inventory-all.csv").write_text("a,b\n", encoding="utf-8")

            result = run_validator(
                "--workload",
                "intune",
                "--mode",
                "full",
                "--root",
                str(root),
                "--reports-root",
                str(reports),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Missing Intune assignment CSV report", result.stdout)

    def test_entra_light_validation_allows_non_effective_enterprise_apps(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "tenant-state" / "entra"
            reports = base / "tenant-state" / "reports" / "entra"
            (root / "Named Locations").mkdir(parents=True, exist_ok=True)
            reports.mkdir(parents=True, exist_ok=True)

            (root / "Named Locations" / "Named Locations.md").write_text("# named\n", encoding="utf-8")
            (reports / "object-inventory-all.csv").write_text("a,b\n", encoding="utf-8")

            result = run_validator(
                "--workload",
                "entra",
                "--mode",
                "light",
                "--root",
                str(root),
                "--reports-root",
                str(reports),
                "--include-named-locations",
                "true",
                "--include-enterprise-applications",
                "true",
                "--include-enterprise-applications-effective",
                "false",
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    def test_entra_light_validation_allows_non_effective_app_registrations(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "tenant-state" / "entra"
            reports = base / "tenant-state" / "reports" / "entra"
            (root / "Named Locations").mkdir(parents=True, exist_ok=True)
            reports.mkdir(parents=True, exist_ok=True)

            (root / "Named Locations" / "Named Locations.md").write_text("# named\n", encoding="utf-8")
            (reports / "object-inventory-all.csv").write_text("a,b\n", encoding="utf-8")

            result = run_validator(
                "--workload",
                "entra",
                "--mode",
                "light",
                "--root",
                str(root),
                "--reports-root",
                str(reports),
                "--include-named-locations",
                "true",
                "--include-app-registrations",
                "true",
                "--include-app-registrations-effective",
                "false",
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    def test_entra_validation_fails_when_required_index_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "tenant-state" / "entra"
            reports = base / "tenant-state" / "reports" / "entra"
            root.mkdir(parents=True, exist_ok=True)
            reports.mkdir(parents=True, exist_ok=True)
            (reports / "object-inventory-all.csv").write_text("a,b\n", encoding="utf-8")

            result = run_validator(
                "--workload",
                "entra",
                "--mode",
                "full",
                "--root",
                str(root),
                "--reports-root",
                str(reports),
                "--include-named-locations",
                "true",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Missing Entra export index for 'Named Locations'", result.stdout)


if __name__ == "__main__":
    unittest.main()
