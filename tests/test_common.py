from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "common.py"


def load_module():
    spec = importlib.util.spec_from_file_location("common_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["common_under_test"] = module
    spec.loader.exec_module(module)
    return module


common = load_module()


class LoggerTests(unittest.TestCase):
    def _log_all(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):  # handlers follow the current streams
            common.log.info("hello")
            common.log.warning("careful")
            common.log.error("broken")
            common.log.debug("hidden at the default level")
        return out.getvalue(), err.getvalue()

    def test_warnings_stay_on_stdout_so_they_cannot_fail_failonstderr_steps(self) -> None:
        out, err = self._log_all()
        self.assertEqual(out, "hello\nWARNING: careful\n")
        self.assertEqual(err, "ERROR: broken\n")

    def test_reserving_stdout_for_data_moves_every_record_to_stderr(self) -> None:
        with patch.object(common, "_stdout_reserved_for_data", False):
            common.reserve_stdout_for_data()
            out, err = self._log_all()
        self.assertEqual(out, "")
        self.assertEqual(err, "hello\nWARNING: careful\nERROR: broken\n")


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


class _Resp:
    def __init__(self, payload) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


def _http_error(code: int, body: str = "", retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("https://x", code, "reason", headers, io.BytesIO(body.encode()))


class RequestJsonTests(unittest.TestCase):
    @contextlib.contextmanager
    def _server(self, responses):
        """Patch urlopen and sleep; yields (requests seen, sleeps made)."""
        calls, sleeps = [], []

        def fake_urlopen(request, timeout):
            calls.append(request)
            outcome = responses[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return _Resp(outcome)

        with patch.dict(os.environ, {"ASTRAL_SCM": "ado"}), \
                patch.object(common.urllib.request, "urlopen", fake_urlopen), \
                patch.object(common.time, "sleep", sleeps.append):
            yield calls, sleeps

    def test_sends_auth_json_body_and_returns_parsed_response(self) -> None:
        with self._server([{"ok": True}]) as (calls, _):
            result = common.request_json("https://x/api", method="POST", body={"a": 1}, token="tok", headers={"X-Y": "z"})
        self.assertEqual(result, {"ok": True})
        request = calls[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("X-y"), "z")
        self.assertEqual(json.loads(request.data), {"a": 1})

    def test_retries_transient_errors_with_backoff_and_retry_after(self) -> None:
        responses = [_http_error(503), _http_error(429, retry_after="7"), urllib.error.URLError("dns"), {"ok": 1}]
        with self._server(responses) as (calls, sleeps):
            result = common.request_json("https://x/api", max_retries=3)
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(len(calls), 4)
        self.assertEqual(sleeps, [1, 7.0, 4])  # 2**0, Retry-After, 2**2

    def test_non_retryable_error_includes_response_body(self) -> None:
        with self._server([_http_error(400, body="bad field")]) as (_, sleeps):
            with self.assertRaises(RuntimeError) as ctx:
                common.request_json("https://x/api", max_retries=3)
        self.assertIn("HTTP Error 400", str(ctx.exception))
        self.assertIn("bad field", str(ctx.exception))
        self.assertEqual(sleeps, [])

    def test_exhausted_retries_raise_the_last_error(self) -> None:
        with self._server([_http_error(503, body="down"), _http_error(503, body="still down")]):
            with self.assertRaises(RuntimeError) as ctx:
                common.request_json("https://x/api", max_retries=1)
        self.assertIn("still down", str(ctx.exception))
        with self._server([urllib.error.URLError("offline")] * 2):
            with self.assertRaises(urllib.error.URLError):
                common.request_json("https://x/api", max_retries=1)

    def test_error_without_body_is_reraised_as_http_error(self) -> None:
        with self._server([_http_error(404)]):
            with self.assertRaises(urllib.error.HTTPError):
                common.request_json("https://x/api")


if __name__ == "__main__":
    unittest.main()
