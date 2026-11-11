from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "restore_checkout_ref.sh"
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "bash not available")
class RestoreCheckoutRefTests(unittest.TestCase):
    """The guard that keeps unreviewed refs from ever being restored to the tenant."""

    def _git(self, cwd, *args):
        return subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    def _repos(self, td):
        origin, work = str(Path(td, "origin.git")), str(Path(td, "work"))
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
        subprocess.run(["git", "clone", "-q", origin, work], check=True, capture_output=True)
        self._git(work, "checkout", "-q", "-b", "main")
        (Path(work) / "a.txt").write_text("v1\n")
        self._git(work, "add", ".")
        self._git(work, "commit", "-q", "-m", "v1")
        self.v1 = self._git(work, "rev-parse", "HEAD")
        self._git(work, "tag", "approved-v1")
        (Path(work) / "a.txt").write_text("v2\n")
        self._git(work, "commit", "-q", "-am", "v2")
        self._git(work, "push", "-q", "origin", "main", "--tags")
        # an unreviewed branch that never reached main
        self._git(work, "checkout", "-q", "-b", "rogue")
        (Path(work) / "a.txt").write_text("rogue\n")
        self._git(work, "commit", "-q", "-am", "rogue")
        self.rogue = self._git(work, "rev-parse", "HEAD")
        self._git(work, "push", "-q", "origin", "rogue")
        self._git(work, "checkout", "-q", "main")
        return work

    def _run(self, work, **env):
        return subprocess.run(
            [BASH, str(SCRIPT)], cwd=work, capture_output=True, text=True,
            env={**os.environ, "APPROVED_BRANCH": "main", "ASTRAL_SCM": "ado", **env},
        )

    def test_tag_and_commit_in_approved_history_are_checked_out(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = self._repos(td)
            for ref in ("approved-v1", self.v1):
                with self.subTest(ref=ref):
                    self._git(work, "checkout", "-q", "--force", "main")
                    result = self._run(work, TARGET_REF_RAW=ref)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(self._git(work, "rev-parse", "HEAD"), self.v1)
                    self.assertEqual((Path(work) / "a.txt").read_text(), "v1\n")
                    self.assertIn(f"variable=RESTORE_BASELINE_COMMIT]{self.v1}", result.stdout)

    def test_branch_name_resolves_and_blank_or_none_falls_back_to_the_default(self) -> None:
        for raw in ("main", "", "none", "  NONE ", "$(RESTORE_REF)"):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as td:
                work = self._repos(td)
                result = self._run(work, TARGET_REF_RAW=raw, BASELINE_BRANCH_PARAM="main")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual((Path(work) / "a.txt").read_text(), "v2\n")

    def test_commit_outside_the_approved_branch_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = self._repos(td)
            for ref in ("rogue", self.rogue):
                result = self._run(work, TARGET_REF_RAW=ref)
                self.assertEqual(result.returncode, 1, ref)
                self.assertIn("not in approved branch", result.stdout)
            self.assertEqual((Path(work) / "a.txt").read_text(), "v2\n")  # the work tree was never touched

    def test_option_injection_and_malformed_refs_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = self._repos(td)
            for ref in ("--upload-pack=touch /tmp/pwned", "-x", "a b", "main;id", "$(id)x", "../x"):
                with self.subTest(ref=ref):
                    result = self._run(work, TARGET_REF_RAW=ref)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("Invalid git ref", result.stdout)

    def test_unknown_ref_and_missing_configuration_fail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = self._repos(td)
            self.assertEqual(self._run(work, TARGET_REF_RAW="no-such-branch").returncode, 1)
            env = {k: v for k, v in os.environ.items() if k != "APPROVED_BRANCH"}
            result = subprocess.run([BASH, str(SCRIPT)], cwd=work, capture_output=True, text=True, env={**env, "TARGET_REF_RAW": "main"})
            self.assertNotEqual(result.returncode, 0)  # APPROVED_BRANCH is mandatory

    def test_empty_target_without_a_default_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = self._repos(td)
            result = self._run(work, TARGET_REF_RAW="")
            self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
