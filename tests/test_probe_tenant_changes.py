from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "probe_tenant_changes.py"


def load_module():
    sys.path.insert(0, str(MODULE_PATH.parent))
    try:
        spec = importlib.util.spec_from_file_location("probe_tenant_changes", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class DebouncerCooldownTests(unittest.TestCase):
    def test_change_during_cooldown_triggers_after_cooldown(self) -> None:
        m = load_module()
        quiet, cooldown = dt.timedelta(minutes=15), dt.timedelta(minutes=30)
        t0 = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
        state = {"debouncer": {"state": "cooldown", "cooldown_until": m._format_iso(t0 + cooldown)}}
        event = {"activityDateTime": m._format_iso(t0 + dt.timedelta(minutes=5))}

        fired, state, _ = m._evaluate_debouncer(state, event, None, t0 + dt.timedelta(minutes=5), quiet, cooldown)
        self.assertFalse(fired)

        fired, state, _ = m._evaluate_debouncer(state, None, None, t0 + dt.timedelta(minutes=31), quiet, cooldown)
        self.assertTrue(fired, "change made during cooldown must not be dropped")

    def test_quiet_cooldown_returns_to_idle(self) -> None:
        m = load_module()
        quiet, cooldown = dt.timedelta(minutes=15), dt.timedelta(minutes=30)
        t0 = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
        state = {"debouncer": {"state": "cooldown", "cooldown_until": m._format_iso(t0)}}
        fired, state, _ = m._evaluate_debouncer(state, None, None, t0 + dt.timedelta(minutes=1), quiet, cooldown)
        self.assertFalse(fired)
        self.assertEqual(state["debouncer"]["state"], "idle")


class DebouncerTransitionTests(unittest.TestCase):
    """Full state table of _evaluate_debouncer (idle -> armed -> cooldown -> idle)."""

    QUIET = dt.timedelta(minutes=15)
    COOLDOWN = dt.timedelta(minutes=30)
    T0 = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def _event(self, at: dt.datetime) -> dict:
        return {"activityDateTime": self.m._format_iso(at)}

    def _run(self, deb: dict, now: dt.datetime, intune=None, entra=None):
        state = {"debouncer": dict(deb)} if deb else {}
        return self.m._evaluate_debouncer(state, intune, entra, now, self.QUIET, self.COOLDOWN)

    def test_idle_without_events_stays_idle(self) -> None:
        fired, state, reason = self._run({}, self.T0)
        self.assertFalse(fired)
        self.assertEqual(state["debouncer"].get("state", "idle"), "idle")
        self.assertEqual(reason, "No changes detected.")

    def test_idle_event_arms_with_quiet_window(self) -> None:
        fired, state, _ = self._run({}, self.T0, intune=self._event(self.T0))
        deb = state["debouncer"]
        self.assertFalse(fired)
        self.assertEqual(deb["state"], "armed")
        self.assertEqual(deb["trigger_after"], self.m._format_iso(self.T0 + self.QUIET))
        self.assertEqual(deb["first_event_at"], self.m._format_iso(self.T0))

    def test_armed_event_extends_window_and_keeps_first_event(self) -> None:
        armed = {
            "state": "armed",
            "first_event_at": self.m._format_iso(self.T0),
            "trigger_after": self.m._format_iso(self.T0 + self.QUIET),
        }
        later = self.T0 + dt.timedelta(minutes=10)
        fired, state, reason = self._run(armed, later, entra=self._event(later))
        deb = state["debouncer"]
        self.assertFalse(fired)
        self.assertEqual(deb["first_event_at"], self.m._format_iso(self.T0))
        self.assertEqual(deb["trigger_after"], self.m._format_iso(later + self.QUIET))
        self.assertIn("entra", reason)

    def test_armed_waits_then_fires_into_cooldown(self) -> None:
        trigger = self.T0 + self.QUIET
        armed = {"state": "armed", "first_event_at": self.m._format_iso(self.T0), "trigger_after": self.m._format_iso(trigger)}
        fired, state, _ = self._run(armed, trigger - dt.timedelta(minutes=1))
        self.assertFalse(fired)
        self.assertEqual(state["debouncer"]["state"], "armed")
        fired, state, _ = self._run(armed, trigger)
        self.assertTrue(fired)
        self.assertEqual(state["debouncer"]["state"], "cooldown")
        self.assertEqual(state["debouncer"]["cooldown_until"], self.m._format_iso(trigger + self.COOLDOWN))

    def test_cooldown_active_defers_and_records_pending(self) -> None:
        cooling = {"state": "cooldown", "cooldown_until": self.m._format_iso(self.T0 + self.COOLDOWN)}
        now = self.T0 + dt.timedelta(minutes=5)
        fired, state, reason = self._run(cooling, now, intune=self._event(now))
        self.assertFalse(fired)
        self.assertEqual(state["debouncer"]["pending_event_at"], self.m._format_iso(now))
        self.assertIn("1 event(s) deferred", reason)

    def test_cooldown_expired_with_pending_fires_immediately_when_quiet(self) -> None:
        cooling = {
            "state": "cooldown",
            "cooldown_until": self.m._format_iso(self.T0),
            "pending_event_at": self.m._format_iso(self.T0 - dt.timedelta(minutes=5)),
        }
        fired, state, _ = self._run(cooling, self.T0 + dt.timedelta(minutes=1))
        self.assertTrue(fired)
        self.assertEqual(state["debouncer"]["state"], "cooldown")

    def test_unknown_state_resets_to_idle(self) -> None:
        fired, state, reason = self._run({"state": "bogus"}, self.T0, intune=self._event(self.T0))
        self.assertFalse(fired)
        self.assertEqual(state["debouncer"]["state"], "idle")
        self.assertIn("bogus", reason)


class StdoutContractTests(unittest.TestCase):
    """run_probe.sh and the Function App parse the probe's stdout as JSON: nothing else may be written there."""

    def _run_cli(self, fake_request: str):
        import json as _json
        import subprocess

        code = (
            "import sys\n"
            f"sys.path.insert(0, {str(MODULE_PATH.parent)!r})\n"
            "import probe_tenant_changes as p\n"
            f"p.request_json = {fake_request}\n"
            "sys.argv = ['probe', '--token', 't', '--state-json', '{}']\n"
            "raise SystemExit(p.main())\n"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        return result, _json

    def test_stdout_is_only_the_json_result(self) -> None:
        result, _json = self._run_cli("lambda *a, **k: {'value': []}")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = _json.loads(result.stdout)  # raises if anything else is on stdout
        self.assertIn("trigger", output)
        self.assertIn("new_state", output)

    def test_graph_failures_are_reported_on_stderr_not_stdout(self) -> None:
        result, _json = self._run_cli("lambda *a, **k: (_ for _ in ()).throw(RuntimeError('graph down'))")
        self.assertEqual(result.returncode, 0, result.stderr)
        _json.loads(result.stdout)
        self.assertIn("graph down", result.stderr)
        self.assertNotIn("graph down", result.stdout)


if __name__ == "__main__":
    unittest.main()
