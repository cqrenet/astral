from __future__ import annotations

import importlib.util
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "common.py"


def load_module():
    spec = importlib.util.spec_from_file_location("common_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


common = load_module()


class LoggerTests(unittest.TestCase):
    def test_info_is_plain_on_stdout_and_warnings_are_prefixed_on_stderr(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        # The handlers hold the streams captured at import time, so swap them for this check.
        stdout_handler = next(h for h in common.log.handlers if h.level == 0)
        stderr_handler = next(h for h in common.log.handlers if h.level > 0)
        old = (stdout_handler.stream, stderr_handler.stream)
        stdout_handler.stream, stderr_handler.stream = out, err
        try:
            common.log.info("hello")
            common.log.warning("careful")
            common.log.error("broken")
        finally:
            stdout_handler.stream, stderr_handler.stream = old
        self.assertEqual(out.getvalue(), "hello\n")
        self.assertEqual(err.getvalue(), "WARNING: careful\nERROR: broken\n")


class HelperTests(unittest.TestCase):
    def test_to_bool_and_env_helpers(self) -> None:
        self.assertTrue(all(common.to_bool(v) for v in ("1", "true", " YES ", "On", "y")))
        self.assertFalse(any(common.to_bool(v) for v in ("", "0", "false", "no", "maybe")))
        with patch.dict(os.environ, {"A": "$(UNRESOLVED)", "B": "7", "C": "x"}):
            self.assertEqual(common.env_text("A", "dflt"), "dflt")  # unresolved ADO macro counts as unset
            self.assertEqual(common.env_int("B", 1), 7)
            self.assertEqual(common.env_int("C", 1), 1)
            self.assertEqual(common.env_int("MISSING", 3), 3)

    def test_normalize_branch_and_ref(self) -> None:
        for raw in ("refs/heads/main", "origin/main", "refs/remotes/origin/main", " main ", "origin/refs/heads/main"):
            self.assertEqual(common.normalize_branch(raw), "main", raw)
        self.assertEqual(common.normalize_branch("$(DRIFT_BRANCH)"), "")
        self.assertEqual(common.normalize_branch("drift/intune"), "drift/intune")
        self.assertEqual(common.ref_from_branch("origin/drift/entra"), "refs/heads/drift/entra")

    def test_doc_and_report_paths(self) -> None:
        for path in ("README.md", "tenant-state/reports/intune/a.csv", "docs/x.json", "tenant-state/intune/Object Inventory/a.json"):
            self.assertTrue(common.is_doc_like(path), path)
        self.assertFalse(common.is_doc_like("tenant-state/intune/Scripts/a.json"))
        self.assertTrue(common.is_report_like("tenant-state/reports/intune/x.json"))
        self.assertFalse(common.is_report_like("tenant-state/intune/Scripts/a.json"))

    def test_thread_status(self) -> None:
        self.assertEqual(common.thread_status_code({"status": 4}), common.THREAD_STATUS_CLOSED)
        self.assertEqual(common.thread_status_code({"status": "ByDesign"}), common.THREAD_STATUS_BY_DESIGN)
        self.assertEqual(common.thread_status_code({}), common.THREAD_STATUS_ACTIVE)
        self.assertTrue(common.is_thread_resolved({"status": "fixed"}))
        self.assertFalse(common.is_thread_resolved({"status": "pending"}))

    def test_build_web_url_prefers_link_then_build_id(self) -> None:
        self.assertEqual(common.build_web_url({"_links": {"web": {"href": "https://w"}}}, "https://c", "p"), "https://w")
        self.assertEqual(common.build_web_url({"id": 9}, "https://c", "p"), "https://c/p/_build/results?buildId=9")
        self.assertEqual(common.build_web_url({}, "https://c", "p"), "")

    def test_restore_request_parameters(self) -> None:
        params = common.RestoreRequest(
            definition_id=1, baseline_branch="main", restore_mode="selective", restore_paths_csv=" a.json ", exclude_csv="none"
        ).template_parameters()
        self.assertEqual(params["restorePathsCsv"], "a.json")
        self.assertNotIn("excludeCsv", params)  # sentinel "none" means no exclusions
        self.assertTrue(params["dryRun"])  # fail safe default
        self.assertNotIn("restorePathsCsv", common.RestoreRequest(1, "main").template_parameters())


if __name__ == "__main__":
    unittest.main()
