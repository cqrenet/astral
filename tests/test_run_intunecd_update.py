from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_intunecd_update.sh"
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "bash not available")
class RunIntuneCdUpdateTests(unittest.TestCase):
    """Restore wrapper, with a fake IntuneCD-startupdate that records its arguments."""

    def _run(self, *, env=None, fake_exit=0, fake_output="all good"):
        with tempfile.TemporaryDirectory() as td:
            bindir, source = Path(td, "bin"), Path(td, "source")
            bindir.mkdir()
            source.mkdir()
            (source / "policy.json").write_text('{"displayName": "p"}\n')
            args_file = Path(td, "args.txt")
            fake = bindir / "IntuneCD-startupdate"
            fake.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{args_file}"\necho "{fake_output}"\nexit {fake_exit}\n')
            fake.chmod(0o755)
            full_env = {
                **os.environ,
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "RESTORE_SOURCE_PATH": str(source),
                "GRAPH_TOKEN": "tok",
                "ASTRAL_SCM": "ado",
                "RUNNER_TEMP": td,
                **(env or {}),
            }
            result = subprocess.run([BASH, str(SCRIPT)], capture_output=True, text=True, env=full_env)
            args = args_file.read_text().splitlines() if args_file.exists() else None
        return result, args

    def test_defaults_are_a_dry_run_that_excludes_device_management_settings(self) -> None:
        result, args = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--report", args)  # dry run is the default
        self.assertNotIn("--remove", args)
        self.assertNotIn("--entraupdate", args)
        self.assertIn("--update-assignments", args)
        self.assertEqual(args[args.index("--exclude") + 1 :], ["DeviceManagementSettings"])
        self.assertEqual(args[args.index("--token") + 1], "tok")

    def test_real_run_with_all_options(self) -> None:
        env = {"DRY_RUN": "FALSE", "REMOVE_UNMANAGED": "True", "ENTRA_UPDATE": "true", "UPDATE_ASSIGNMENTS": "false"}
        result, args = self._run(env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("--report", args)
        self.assertNotIn("--update-assignments", args)
        self.assertIn("--remove", args)
        self.assertIn("--entraupdate", args)

    def test_exclude_list_is_trimmed_and_sentinels_mean_none(self) -> None:
        _, args = self._run(env={"EXCLUDE_CSV": " Scripts , Filters ,, "})
        self.assertEqual(args[args.index("--exclude") + 1 :], ["Scripts", "Filters", "DeviceManagementSettings"])
        for sentinel in ("none", "N/A", "-", "_none_"):
            with self.subTest(sentinel=sentinel):
                _, args = self._run(env={"EXCLUDE_CSV": sentinel})
                self.assertEqual(args[args.index("--exclude") + 1 :], ["DeviceManagementSettings"])

    def test_user_supplied_device_management_settings_is_not_duplicated(self) -> None:
        _, args = self._run(env={"EXCLUDE_CSV": "devicemanagementsettings,Scripts"})
        self.assertEqual(args[args.index("--exclude") + 1 :], ["devicemanagementsettings", "Scripts"])

    def test_failure_with_error_markers_fails_the_step(self) -> None:
        result, _ = self._run(fake_exit=3, fake_output="[ERROR] Request failed with status 403")
        self.assertEqual(result.returncode, 3)

    def test_failure_without_markers_preserves_the_failure(self) -> None:
        result, _ = self._run(fake_exit=1, fake_output="nothing to do")
        self.assertEqual(result.returncode, 1)
        self.assertIn("preserving the non-zero exit status", result.stdout)

    def test_required_inputs(self) -> None:
        for missing in ("RESTORE_SOURCE_PATH", "GRAPH_TOKEN"):
            with self.subTest(missing=missing):
                env = {missing: ""}
                result, _ = self._run(env=env)
                self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
