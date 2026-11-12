from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate_assignment_report.py"
sys.path.insert(0, str(MODULE_PATH.parent))  # script imports its sibling report_utils.py


def load_module():
    spec = importlib.util.spec_from_file_location("generate_assignment_report", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_assignment_report"] = module
    spec.loader.exec_module(module)
    return module


def _write(root: Path, rel: str, payload: dict) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class IterAssignmentRowsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def test_row_states(self) -> None:
        group = "#microsoft.graph.groupAssignmentTarget"
        exclusion = "#microsoft.graph.exclusionGroupAssignmentTarget"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "Scripts/assigned.json", {"displayName": "A", "assignments": [
                {"target": {"@odata.type": group, "groupDisplayName": "Admins"}},
                {"target": {"@odata.type": exclusion, "groupId": "g2"}},
            ]})
            _write(root, "Scripts/none.json", {"displayName": "N"})
            _write(root, "Scripts/empty__id.json", {"assignments": []})
            _write(root, "Scripts/junk.json", {"displayName": "J", "assignments": ["x", 3]})
            _write(root, "App Registrations/app.json", {"assignments": []})  # excluded category
            _write(root, "reports/r.json", {"assignments": []})  # reports are skipped

            rows = {(r.object_name, r.assignment_state, r.intent, r.assignment_target)
                    for r in self.m.iter_assignment_rows(root, set(), set())}

        self.assertEqual(rows, {
            ("A", "Assigned", "Include", "Admins"),
            ("A", "Assigned", "Exclude", "g2"),
            ("N", "NotExported", "None", "Not exported in backup"),
            ("empty", "Unassigned", "None", "No assignments"),
            ("J", "Unassigned", "None", "No assignments"),
        })

    def test_policy_type_filter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "Scripts/a.json", {"displayName": "A", "assignments": []})
            _write(root, "Filters/b.json", {"displayName": "B", "assignments": []})
            rows = list(self.m.iter_assignment_rows(root, {"filters"}, set()))
        self.assertEqual([r.object_name for r in rows], ["B"])


if __name__ == "__main__":
    unittest.main()
