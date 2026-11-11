from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

CI_SH = Path(__file__).resolve().parents[1] / "scripts" / "ci.sh"
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "bash not available")
class CiShTests(unittest.TestCase):
    def _bash(self, snippet: str, env: dict[str, str] | None = None, hour: str = "07") -> subprocess.CompletedProcess:
        """Run a snippet with ci.sh sourced; `date +%H` is faked so the current hour is deterministic."""
        with tempfile.TemporaryDirectory() as td:
            fake_date = Path(td, "date")
            fake_date.write_text(f'#!/bin/sh\n[ "$1" = "+%H" ] && echo {hour} || exec /bin/date "$@"\n')
            fake_date.chmod(0o755)
            full_env = {"PATH": f"{td}:{os.environ['PATH']}", "HOME": td, **(env or {})}
            return subprocess.run(
                [BASH, "-c", f'source "{CI_SH}"; {snippet}'], env=full_env, capture_output=True, text=True
            )

    def test_full_run_hour_compares_numerically(self) -> None:
        cases = [
            ({"FULL_RUN_HOUR": "7"}, True),  # unpadded
            ({"FULL_RUN_HOUR": "07"}, True),
            ({"FULL_RUN_HOUR": "08"}, False),
            ({"FULL_RUN_HOUR": ""}, False),  # empty falls back to the default 02, and it is 07
            ({"FULL_RUN_HOUR": "noon"}, False),  # non-numeric never matches (and never errors)
            ({"FULL_RUN_HOUR": "99"}, False),
            ({"FULL_RUN_HOUR": "9", "FORCE_FULL_RUN": "true"}, True),  # forced regardless of the hour
            ({"FORCE_FULL_RUN": "false"}, False),
        ]
        for env, expected in cases:
            with self.subTest(env=env):
                self.assertEqual(self._bash("ci_is_full_run", env).returncode == 0, expected)

    def test_default_full_run_hour_is_two(self) -> None:
        self.assertTrue(self._bash("ci_is_full_run", {}, hour="02").returncode == 0)
        self.assertFalse(self._bash("ci_is_full_run", {}, hour="03").returncode == 0)

    def test_ci_log_syntax_follows_the_platform(self) -> None:
        self.assertEqual(self._bash("ci_log warning 'careful'", {}).stdout.strip(), "##vso[task.logissue type=warning]careful")
        self.assertEqual(self._bash("ci_log error 'bad'", {"GITHUB_ACTIONS": "true"}).stdout.strip(), "::error::bad")
        self.assertEqual(self._bash("ci_log error 'bad'", {"ASTRAL_SCM": "gitea"}).stdout.strip(), "::error::bad")
        # ASTRAL_SCM=ado wins over a GitHub-looking runner, like common.scm_platform()
        self.assertEqual(
            self._bash("ci_log error 'bad'", {"ASTRAL_SCM": "ado", "GITHUB_ACTIONS": "true"}).stdout.strip(),
            "##vso[task.logissue type=error]bad",
        )

    def test_ci_set_env_writes_github_env_without_newline_injection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td, "env")
            self._bash('ci_set_env NAME "a\nINJECTED=1"', {"GITHUB_ACTIONS": "true", "GITHUB_ENV": str(env_file)})
            self.assertEqual(env_file.read_text(), "NAME=a INJECTED=1\n")  # one line, no extra variable
        out = self._bash("ci_set_env NAME value", {}).stdout.strip()
        self.assertEqual(out, "##vso[task.setvariable variable=NAME]value")

    def test_azure_devops_commands_cannot_be_injected_through_newlines(self) -> None:
        payload = "ok\n##vso[task.setvariable variable=EVIL]1"
        for snippet in (f'ci_set_env NAME "{payload}"', f'ci_log error "{payload}"'):
            with self.subTest(snippet=snippet.split()[0]):
                lines = self._bash(snippet, {}).stdout.strip().splitlines()
                self.assertEqual(len(lines), 1)  # one command, whatever the value contained


if __name__ == "__main__":
    unittest.main()
