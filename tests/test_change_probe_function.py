from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROBE_DIR = Path(__file__).resolve().parents[1] / "infra" / "change-probe"


def _stub_azure() -> None:
    """The Functions runtime and Table SDK are not installed for unit tests."""
    func = types.ModuleType("azure.functions")
    func.TimerRequest = object
    func.QueueMessage = object
    func.Out = {str: object}  # func.Out[str] is evaluated in an annotation
    func.HttpRequest = object
    func.HttpResponse = lambda body="", status_code=200, **kwargs: types.SimpleNamespace(body=body, status_code=status_code)
    tables = types.ModuleType("azure.data.tables")
    tables.TableServiceClient = MagicMock()
    for name, module in (
        ("azure", types.ModuleType("azure")),
        ("azure.functions", func),
        ("azure.data", types.ModuleType("azure.data")),
        ("azure.data.tables", tables),
    ):
        sys.modules.setdefault(name, module)


def _load(name: str):
    _stub_azure()
    spec = importlib.util.spec_from_file_location(f"cp_{name}", PROBE_DIR / name / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class ProbeTimerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load("probe_timer")

    def _run(self, *, env=None, state=None, run_result=None, run_error=None, script_exists=True):
        """Run main() with scripted collaborators; returns (queued messages, saved states, subprocess commands)."""
        queued, saved, commands = [], [], []
        msg = MagicMock()
        msg.set.side_effect = queued.append
        timer = types.SimpleNamespace(schedule_status={"Last": "t"})

        def fake_run(cmd, **kwargs):
            commands.append((cmd, kwargs))
            if run_error:
                raise run_error
            return run_result

        with tempfile.TemporaryDirectory() as td:
            if script_exists:
                (Path(td) / "scripts").mkdir()
                (Path(td) / "scripts" / "probe_tenant_changes.py").write_text("")
            base = {"REPO_ROOT": td, "AzureWebJobsStorage": "conn", "GRAPH_TOKEN": "tok"}
            with patch.dict(os.environ, {**base, **(env or {})}, clear=True), \
                    patch.object(self.m, "_load_state", return_value=state or {}), \
                    patch.object(self.m, "_save_state", side_effect=lambda conn, s: saved.append(s)), \
                    patch.object(self.m.subprocess, "run", fake_run):
                self.m.main(timer, msg)
        return queued, saved, commands

    def _probe_output(self, **kwargs) -> subprocess.CompletedProcess:
        return _completed(json.dumps({"new_state": {"s": 1}, "trigger": False, "reason": "quiet", "checked_at": "now", **kwargs}))

    def test_trigger_saves_state_and_queues_a_message(self) -> None:
        queued, saved, commands = self._run(run_result=self._probe_output(trigger=True, reason="armed"), state={"d": 1})
        self.assertEqual(saved, [{"s": 1}])
        self.assertEqual(json.loads(queued[0]), {"reason": "armed", "checked_at": "now"})
        cmd, run_kwargs = commands[0]
        self.assertNotIn("--token", cmd)
        self.assertEqual(run_kwargs["env"]["GRAPH_TOKEN"], "tok")
        self.assertEqual(cmd[cmd.index("--state-json") + 1], json.dumps({"d": 1}))
        self.assertEqual(cmd[cmd.index("--quiet-window-minutes") + 1], "15")  # defaults

    def test_no_trigger_still_persists_state_but_queues_nothing(self) -> None:
        queued, saved, _ = self._run(run_result=self._probe_output(trigger=False))
        self.assertEqual((queued, saved), ([], [{"s": 1}]))

    def test_app_credentials_are_used_when_there_is_no_token(self) -> None:
        env = {"GRAPH_TOKEN": "", "PROBE_APP_ID": "id", "PROBE_APP_SECRET": "sec", "TENANT_ID": "ten",
               "PROBE_QUIET_WINDOW_MINUTES": "5", "PROBE_COOLDOWN_MINUTES": "9"}
        _, _, commands = self._run(env=env, run_result=self._probe_output())
        cmd, run_kwargs = commands[0]
        self.assertNotIn("--client-id", cmd)
        self.assertNotIn("--client-secret", cmd)
        self.assertEqual(run_kwargs["env"]["PROBE_APP_ID"], "id")
        self.assertEqual(run_kwargs["env"]["PROBE_APP_SECRET"], "sec")
        self.assertEqual(run_kwargs["env"]["TENANT_ID"], "ten")
        self.assertNotIn("--state-json", cmd)  # no saved state yet
        self.assertEqual((cmd[cmd.index("--quiet-window-minutes") + 1], cmd[cmd.index("--cooldown-minutes") + 1]), ("5", "9"))

    def test_missing_configuration_does_nothing(self) -> None:
        for env, kwargs in (
            ({"GRAPH_TOKEN": ""}, {}),  # no auth at all
            ({"AzureWebJobsStorage": ""}, {}),  # no storage
            ({}, {"script_exists": False}),  # probe script missing
        ):
            with self.subTest(env=env, kwargs=kwargs):
                queued, saved, commands = self._run(env=env, run_result=self._probe_output(trigger=True), **kwargs)
                self.assertEqual((queued, saved, commands), ([], [], []))

    def test_probe_failures_change_nothing(self) -> None:
        for kwargs in (
            {"run_result": _completed(returncode=2, stderr="boom")},
            {"run_result": _completed(stdout="not json")},
            {"run_error": subprocess.TimeoutExpired("probe", 60)},
            {"run_error": OSError("cannot exec")},
        ):
            with self.subTest(kwargs=list(kwargs)):
                queued, saved, _ = self._run(**kwargs)
                self.assertEqual((queued, saved), ([], []))


class QueueConsumerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load("queue_consumer")

    ENV = {"ADO_ORGANIZATION": "org", "ADO_PROJECT": "proj", "ADO_PIPELINE_ID": "42", "ADO_TOKEN": "tok"}

    def _run(self, *, env=None, run_result=None, run_error=None, script_exists=True):
        commands = []
        message = MagicMock()
        message.get_body.return_value = b'{"reason": "x"}'

        def fake_run(cmd, **kwargs):
            commands.append((cmd, kwargs))
            if run_error:
                raise run_error
            return run_result

        with tempfile.TemporaryDirectory() as td:
            if script_exists:
                (Path(td) / "scripts").mkdir()
                (Path(td) / "scripts" / "trigger_backup_pipeline.py").write_text("")
            with patch.dict(os.environ, {"REPO_ROOT": td, **(self.ENV if env is None else env)}, clear=True), \
                    patch.object(self.m.subprocess, "run", fake_run):
                self.m.main(message)
        return commands

    def test_triggers_the_pipeline_with_configured_values(self) -> None:
        cmd, run_kwargs = self._run(env={**self.ENV, "ADO_BRANCH": "release"}, run_result=_completed("queued"))[0]
        self.assertEqual(cmd[cmd.index("--pipeline-id") + 1], "42")
        self.assertEqual(cmd[cmd.index("--branch") + 1], "release")
        self.assertEqual(run_kwargs["env"]["ADO_TOKEN"], "tok")
        self.assertNotIn("--token", cmd)

    def test_default_branch_is_main(self) -> None:
        cmd, _ = self._run(run_result=_completed("queued"))[0]
        self.assertEqual(cmd[cmd.index("--branch") + 1], "main")

    def test_empty_branch_is_allowed_like_before(self) -> None:
        cmd, _ = self._run(env={**self.ENV, "ADO_BRANCH": ""}, run_result=_completed("queued"))[0]
        self.assertEqual(cmd[cmd.index("--branch") + 1], "")

    def test_failures_raise_so_the_runtime_retries_the_message(self) -> None:
        with self.assertRaises(RuntimeError):
            self._run(env={"ADO_ORGANIZATION": "org"})  # incomplete configuration
        with self.assertRaises(RuntimeError):
            self._run(script_exists=False, run_result=_completed())
        with self.assertRaises(RuntimeError):
            self._run(run_result=_completed(returncode=1, stderr="nope"))
        with self.assertRaises(subprocess.TimeoutExpired):
            self._run(run_error=subprocess.TimeoutExpired("trigger", 60))


if __name__ == "__main__":
    unittest.main()


class WebhookAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load("pr_comment_webhook")

    @staticmethod
    def _req(authorization=None):
        headers = {} if authorization is None else {"Authorization": authorization}
        return types.SimpleNamespace(headers=headers)

    @staticmethod
    def _basic(user: str, password: str) -> str:
        import base64
        return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def test_any_username_with_the_secret_is_accepted(self) -> None:
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "s3cret"}):
            for user in ("", "astral", "anything"):
                self.assertTrue(self.m._authorized(self._req(self._basic(user, "s3cret"))), user)

    def test_wrong_missing_or_malformed_credentials_are_rejected(self) -> None:
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "s3cret"}):
            for header in (None, "", "Bearer s3cret", "Basic", "Basic !!!", self._basic("astral", "wrong"),
                           "Basic " + "czNjcmV0"):  # base64("s3cret") without a colon
                self.assertFalse(self.m._authorized(self._req(header)), header)

    def test_missing_secret_fails_closed(self) -> None:
        with patch.dict(os.environ, {"WEBHOOK_SECRET": ""}):
            self.assertFalse(self.m._authorized(self._req(self._basic("astral", ""))))
            self.assertFalse(self.m._authorized(self._req(None)))

    def test_unauthorized_request_gets_401_before_parsing(self) -> None:
        req = MagicMock()
        req.headers = {}
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "s3cret"}):
            response = self.m.main(req)
        self.assertEqual(response.status_code, 401)
        req.get_json.assert_not_called()
