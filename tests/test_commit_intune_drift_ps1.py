from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "commit_intune_drift.ps1"
PWSH = shutil.which("pwsh")


def _extract_function(source: str, name: str) -> str:
    """Source text of a (possibly nested) PowerShell function, found by brace matching."""
    start = source.index(f"function {name}")
    depth, index = 0, source.index("{", start)
    for position in range(index, len(source)):
        depth += {"{": 1, "}": -1}.get(source[position], 0)
        if depth == 0:
            return source[start : position + 1]
    raise AssertionError(f"unterminated function {name}")


@unittest.skipUnless(PWSH, "pwsh not installed")
class CommitIntuneDriftPs1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def _pwsh(self, body: str, *, functions: tuple[str, ...], env=None, timeout=60) -> subprocess.CompletedProcess:
        defs = "\n".join(_extract_function(self.source, name) for name in functions)
        return subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-Command", f"$ErrorActionPreference='Stop'\n{defs}\n{body}"],
            capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})},
        )

    def test_script_parses(self) -> None:
        result = subprocess.run(
            [PWSH, "-NoProfile", "-Command",
             f"$e=$null;$t=$null;[System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}',[ref]$t,[ref]$e)|Out-Null;$e.Count"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.stdout.strip(), "0", result.stdout + result.stderr)

    def test_start_process_does_not_deadlock_on_large_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            noisy = Path(td, "noisy.sh")
            noisy.write_text("#!/bin/sh\nhead -c 300000 /dev/zero | tr '\\0' x\nhead -c 300000 /dev/zero | tr '\\0' y >&2\n")
            noisy.chmod(0o755)
            result = self._pwsh(
                f"$r = _startProcess '{noisy}' -outputErr2Std; Write-Output (($r -join '').Length)",
                functions=("_startProcess",),
            )
        self.assertEqual(result.stdout.strip(), "600000", result.stdout + result.stderr)

    def test_start_process_returns_stdout_and_raises_on_stderr(self) -> None:
        result = self._pwsh(
            "$o = _startProcess 'echo' -argumentList 'hello'; Write-Output \"out=$($o.Trim())\"\n"
            "try { _startProcess 'sh' -argumentList '-c \"echo oops >&2\"' } catch { Write-Output \"err=$($_.Exception.Message.Trim())\" }",
            functions=("_startProcess",),
        )
        self.assertIn("out=hello", result.stdout)
        self.assertIn("err=oops", result.stdout)

    def test_ci_output_follows_the_platform(self) -> None:
        funcs = ("Test-ActionsRunner", "Set-CiOutput")
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td, "out")
            self._pwsh("Set-CiOutput 'NAME' \"a`nb\"", functions=funcs, env={"GITHUB_ACTIONS": "true", "GITHUB_OUTPUT": str(out_file)})
            self.assertEqual(out_file.read_text().strip(), "NAME=a b")  # one line: no injected second entry
        ado = self._pwsh("Set-CiOutput 'NAME' 'v'", functions=funcs, env={"ASTRAL_SCM": "ado", "GITHUB_ACTIONS": "true"})
        self.assertIn("##vso[task.setVariable variable=NAME]v", ado.stdout)  # ASTRAL_SCM wins over GITHUB_ACTIONS
        self.assertIn("variable=NAME;isOutput=true]v", ado.stdout)


if __name__ == "__main__":
    unittest.main()
