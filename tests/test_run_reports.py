from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_reports.py"

_YAML_AVAILABLE = importlib.util.find_spec("yaml") is not None


def load_module():
    spec = importlib.util.spec_from_file_location("run_reports", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Pre-built definitions dict used in dispatch tests to avoid PyYAML dependency.
_SAMPLE_DEFINITIONS = {
    "version": 1,
    "reports": [
        {
            "name": "always-report",
            "script": "fake_report.py",
            "workloads": ["intune", "entra"],
            "args": ["--root", "{backup_dir}", "--output-dir", "{reports_dir}"],
            "outputs": ["out.csv"],
        },
        {
            "name": "full-only-report",
            "script": "fake_full.py",
            "workloads": ["intune"],
            "condition": "full_run",
            "args": ["--root", "{backup_dir}"],
            "outputs": ["full.csv"],
        },
        {
            "name": "entra-ca-report",
            "script": "fake_ca.py",
            "workloads": ["entra"],
            "condition": {"entra": "ENTRA_INCLUDE_CA"},
            "args": ["--root", "{backup_dir}"],
            "outputs": ["ca.csv"],
        },
    ],
    "documentation": [],
}


class ConditionEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def _eval(self, condition, workload="intune", mode="light"):
        return self.m._eval_condition(condition, workload, mode)

    def test_none_always_true(self):
        self.assertTrue(self._eval(None))

    def test_full_run_condition(self):
        self.assertFalse(self._eval("full_run", mode="light"))
        self.assertTrue(self._eval("full_run", mode="full"))

    def test_env_var_condition(self):
        with patch.dict(os.environ, {"MY_FLAG": "true"}):
            self.assertTrue(self._eval("MY_FLAG"))
        with patch.dict(os.environ, {"MY_FLAG": "false"}):
            self.assertFalse(self._eval("MY_FLAG"))
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(self._eval("UNSET_FLAG"))

    def test_or_condition(self):
        with patch.dict(os.environ, {"A": "true", "B": "false"}):
            self.assertTrue(self._eval("A or B"))
        with patch.dict(os.environ, {"A": "false", "B": "false"}):
            self.assertFalse(self._eval("A or B"))

    def test_and_condition(self):
        with patch.dict(os.environ, {"A": "true", "B": "true"}):
            self.assertTrue(self._eval("A and B"))
        with patch.dict(os.environ, {"A": "true", "B": "false"}):
            self.assertFalse(self._eval("A and B"))

    def test_per_workload_condition_present(self):
        condition = {"entra": "ENTRA_CA"}
        with patch.dict(os.environ, {"ENTRA_CA": "true"}):
            self.assertTrue(self._eval(condition, workload="entra"))
        with patch.dict(os.environ, {"ENTRA_CA": "false"}):
            self.assertFalse(self._eval(condition, workload="entra"))

    def test_per_workload_condition_absent_key(self):
        # No condition defined for intune means always run.
        condition = {"entra": "ENTRA_CA"}
        self.assertTrue(self._eval(condition, workload="intune"))

    def test_env_bool_truthy_values(self):
        for val in ("1", "true", "True", "TRUE", "yes", "YES", "y", "on", "ON"):
            with patch.dict(os.environ, {"X": val}):
                self.assertTrue(self.m._env_bool("X"), msg=f"Expected truthy for {val!r}")

    def test_env_bool_falsy_values(self):
        for val in ("0", "false", "no", "off", "", "nope"):
            with patch.dict(os.environ, {"X": val}):
                self.assertFalse(self.m._env_bool("X"), msg=f"Expected falsy for {val!r}")


class MergeDefinitionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def _report(self, name: str, script: str = "x.py", workloads=None) -> dict:
        return {"name": name, "script": script, "workloads": workloads or ["intune"]}

    def test_base_only_all_marked_builtin(self):
        base = {"version": 1, "reports": [self._report("r1")], "documentation": []}
        result = self.m._merge_definitions(base, {})
        reports = result["reports"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["source"], "builtin")

    def test_local_only_entry_appended_as_local(self):
        base = {"version": 1, "reports": [self._report("r1")], "documentation": []}
        local = {"reports": [self._report("r2")], "documentation": []}
        result = self.m._merge_definitions(base, local)
        names = {r["name"]: r["source"] for r in result["reports"]}
        self.assertEqual(names, {"r1": "builtin", "r2": "local"})

    def test_local_overrides_builtin_by_name(self):
        base = {"version": 1, "reports": [self._report("r1", "old.py")], "documentation": []}
        local = {"reports": [self._report("r1", "new.py")], "documentation": []}
        result = self.m._merge_definitions(base, local)
        self.assertEqual(len(result["reports"]), 1)
        self.assertEqual(result["reports"][0]["script"], "new.py")
        self.assertEqual(result["reports"][0]["source"], "local")

    def test_empty_local_returns_base_unchanged(self):
        base = {"version": 1, "reports": [self._report("r1")], "documentation": []}
        result = self.m._merge_definitions(base, {})
        self.assertEqual(len(result["reports"]), 1)

    def test_documentation_section_also_merged(self):
        base = {"version": 1, "reports": [], "documentation": [{"name": "d1", "workloads": ["intune"]}]}
        local = {"reports": [], "documentation": [{"name": "d2", "workloads": ["entra"]}]}
        result = self.m._merge_definitions(base, local)
        names = {d["name"] for d in result["documentation"]}
        self.assertEqual(names, {"d1", "d2"})

    def test_entries_without_name_are_ignored(self):
        base = {"version": 1, "reports": [{"script": "x.py"}], "documentation": []}
        result = self.m._merge_definitions(base, {})
        self.assertEqual(result["reports"], [])


class RenderArgsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def test_placeholder_substitution(self):
        ctx = {"backup_dir": "/data/intune", "reports_dir": "/data/reports/intune"}
        result = self.m._render(
            ["--root", "{backup_dir}", "--output-dir", "{reports_dir}"], ctx
        )
        self.assertEqual(
            result, ["--root", "/data/intune", "--output-dir", "/data/reports/intune"]
        )

    def test_no_placeholders(self):
        self.assertEqual(self.m._render(["--flag"], {"backup_dir": "/x"}), ["--flag"])


@unittest.skipUnless(_YAML_AVAILABLE, "PyYAML not installed — skipping YAML loading tests")
class LoadYamlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def test_load_valid_yaml(self):
        import textwrap

        content = textwrap.dedent("""\
            version: 1
            reports:
              - name: test-report
                script: test.py
                workloads: [intune]
                args: [--root, "{backup_dir}"]
                outputs: [out.csv]
        """)
        with tempfile.NamedTemporaryFile(
            suffix=".yml", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            fname = f.name
        try:
            defs = self.m._load_yaml(Path(fname))
            self.assertEqual(defs["version"], 1)
            self.assertEqual(defs["reports"][0]["name"], "test-report")
        finally:
            os.unlink(fname)

    def test_empty_yaml_returns_empty_dict(self):
        with tempfile.NamedTemporaryFile(
            suffix=".yml", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write("")
            fname = f.name
        try:
            defs = self.m._load_yaml(Path(fname))
            self.assertEqual(defs, {})
        finally:
            os.unlink(fname)


class MainDispatchTests(unittest.TestCase):
    """Dispatch logic tests — _load_yaml is mocked to avoid PyYAML dependency."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def _run_main(self, argv_tail: list[str], extra_env: dict | None = None):
        """Run main() with a fake definitions file and mocked _load_yaml."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defs_dir = root / "reports"
            defs_dir.mkdir()
            defs_path = defs_dir / "definitions.yml"
            defs_path.write_text("# placeholder\n", encoding="utf-8")

            ran: list[str] = []

            def fake_run_report(defn, ctx, script_root):
                ran.append(defn["name"])
                return True

            argv = [
                "run_reports.py",
                "--backup-dir", str(root / "tenant-state" / "intune"),
                "--reports-dir", str(root / "tenant-state" / "reports" / "intune"),
                "--repo-root", str(root),
            ] + argv_tail

            env_patch = patch.dict(os.environ, extra_env or {})
            load_patch = patch.object(self.m, "_load_yaml", return_value=_SAMPLE_DEFINITIONS)
            report_patch = patch.object(self.m, "_run_report", side_effect=fake_run_report)

            saved = sys.argv
            sys.argv = argv
            try:
                with env_patch, load_patch, report_patch:
                    rc = self.m.main()
            finally:
                sys.argv = saved

            return rc, ran

    def test_light_run_skips_full_only(self):
        rc, ran = self._run_main(
            ["--workload", "intune", "--mode", "light", "--type", "reports"]
        )
        self.assertEqual(rc, 0)
        self.assertIn("always-report", ran)
        self.assertNotIn("full-only-report", ran)

    def test_full_run_includes_full_only(self):
        rc, ran = self._run_main(
            ["--workload", "intune", "--mode", "full", "--type", "reports"]
        )
        self.assertEqual(rc, 0)
        self.assertIn("always-report", ran)
        self.assertIn("full-only-report", ran)

    def test_mode_1_treated_as_full(self):
        rc, ran = self._run_main(
            ["--workload", "intune", "--mode", "1", "--type", "reports"]
        )
        self.assertIn("full-only-report", ran)

    def test_mode_0_treated_as_light(self):
        rc, ran = self._run_main(
            ["--workload", "intune", "--mode", "0", "--type", "reports"]
        )
        self.assertNotIn("full-only-report", ran)

    def test_entra_workload_filters_correctly(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defs_dir = root / "reports"
            defs_dir.mkdir()
            (defs_dir / "definitions.yml").write_text("# placeholder\n", encoding="utf-8")

            ran: list[str] = []

            def fake_run_report(defn, ctx, script_root):
                ran.append(defn["name"])
                return True

            # Override backup-dir to use entra path
            argv = [
                "run_reports.py",
                "--workload", "entra",
                "--backup-dir", str(root / "tenant-state" / "entra"),
                "--reports-dir", str(root / "tenant-state" / "reports" / "entra"),
                "--repo-root", str(root),
                "--mode", "light",
                "--type", "reports",
            ]
            saved = sys.argv
            sys.argv = argv
            try:
                with patch.dict(os.environ, {"ENTRA_INCLUDE_CA": "true"}), \
                     patch.object(self.m, "_load_yaml", return_value=_SAMPLE_DEFINITIONS), \
                     patch.object(self.m, "_run_report", side_effect=fake_run_report):
                    rc = self.m.main()
            finally:
                sys.argv = saved

        self.assertEqual(rc, 0)
        self.assertIn("always-report", ran)
        self.assertIn("entra-ca-report", ran)
        self.assertNotIn("full-only-report", ran)  # intune-only

    def test_entra_ca_skipped_when_condition_false(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "reports").mkdir()
            (root / "reports" / "definitions.yml").write_text("# placeholder\n", encoding="utf-8")

            ran: list[str] = []

            def fake_run_report(defn, ctx, script_root):
                ran.append(defn["name"])
                return True

            argv = [
                "run_reports.py",
                "--workload", "entra",
                "--backup-dir", str(root / "tenant-state" / "entra"),
                "--reports-dir", str(root / "tenant-state" / "reports" / "entra"),
                "--repo-root", str(root),
                "--mode", "light",
                "--type", "reports",
            ]
            saved = sys.argv
            sys.argv = argv
            try:
                with patch.dict(os.environ, {"ENTRA_INCLUDE_CA": "false"}), \
                     patch.object(self.m, "_load_yaml", return_value=_SAMPLE_DEFINITIONS), \
                     patch.object(self.m, "_run_report", side_effect=fake_run_report):
                    self.m.main()
            finally:
                sys.argv = saved

        self.assertNotIn("entra-ca-report", ran)

    def test_failed_report_returns_nonzero(self):
        rc, _ = self._run_main(["--workload", "intune", "--mode", "light", "--type", "reports"])
        # Patch _run_report to always fail
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "reports").mkdir()
            (root / "reports" / "definitions.yml").write_text("# placeholder\n", encoding="utf-8")
            argv = [
                "run_reports.py",
                "--workload", "intune",
                "--backup-dir", str(root / "tenant-state" / "intune"),
                "--reports-dir", str(root / "tenant-state" / "reports" / "intune"),
                "--repo-root", str(root),
                "--mode", "light",
                "--type", "reports",
            ]
            saved = sys.argv
            sys.argv = argv
            try:
                with patch.object(self.m, "_load_yaml", return_value=_SAMPLE_DEFINITIONS), \
                     patch.object(self.m, "_run_report", return_value=False):
                    rc = self.m.main()
            finally:
                sys.argv = saved
        self.assertNotEqual(rc, 0)

    def test_missing_definitions_file_returns_nonzero(self):
        saved = sys.argv
        sys.argv = [
            "run_reports.py",
            "--workload", "intune",
            "--backup-dir", "/nonexistent/intune",
            "--reports-dir", "/nonexistent/reports",
            "--repo-root", "/nonexistent",
            "--mode", "light",
        ]
        try:
            rc = self.m.main()
        finally:
            sys.argv = saved
        self.assertNotEqual(rc, 0)

    def test_type_reports_skips_documentation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "reports").mkdir()
            (root / "reports" / "definitions.yml").write_text("# placeholder\n", encoding="utf-8")

            doc_defs = {
                "version": 1,
                "reports": [],
                "documentation": [
                    {
                        "name": "intune-as-built",
                        "workloads": ["intune"],
                        "condition": "full_run",
                        "backup_path": "{backup_dir}",
                        "output_path": "{repo_root}/prod-as-built.md",
                        "enrich": True,
                    }
                ],
            }
            doc_ran: list[str] = []

            def fake_doc(defn, ctx):
                doc_ran.append(defn["name"])
                return True

            argv = [
                "run_reports.py",
                "--workload", "intune",
                "--backup-dir", str(root / "tenant-state" / "intune"),
                "--reports-dir", str(root / "tenant-state" / "reports" / "intune"),
                "--repo-root", str(root),
                "--mode", "full",
                "--type", "reports",
            ]
            saved = sys.argv
            sys.argv = argv
            try:
                with patch.object(self.m, "_load_yaml", return_value=doc_defs), \
                     patch.object(self.m, "_run_documentation", side_effect=fake_doc):
                    self.m.main()
            finally:
                sys.argv = saved

        self.assertNotIn("intune-as-built", doc_ran)


if __name__ == "__main__":
    unittest.main()
