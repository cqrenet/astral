from __future__ import annotations

import base64
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "apply_reviewer_rejections.py"
sys.path.insert(0, str(MODULE_PATH.parent))  # script imports its sibling common.py

ALLOWED = {"reviewer@contoso.com"}
REVIEWER = {"uniqueName": "reviewer@contoso.com"}


def load_module():
    spec = importlib.util.spec_from_file_location("apply_reviewer_rejections", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _marker(path: str) -> str:
    encoded = base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")
    return f"Automation marker: AUTO-CHANGE-TICKET:{encoded}"


def _thread(thread_id: int, path: str, *decisions: str) -> dict:
    comments = [{"id": 1, "parentCommentId": 0, "content": _marker(path)}]
    comments += [
        {"id": 2 + i, "parentCommentId": 0, "content": d, "author": REVIEWER} for i, d in enumerate(decisions)
    ]
    return {"id": thread_id, "comments": comments}


class FindRejectionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def test_only_changed_paths_with_latest_reject_are_returned(self) -> None:
        a, b, c = "tenant-state/intune/Scripts/a.json", "tenant-state/intune/Scripts/b.json", "tenant-state/intune/Scripts/c.json"
        threads = [
            _thread(10, a, "/reject"),
            _thread(11, b, "/reject", "/accept"),  # latest decision wins
            _thread(12, c, "/reject"),  # not changed in the PR
            {"id": 13, "comments": [{"id": 1, "content": "plain comment"}]},  # not a ticket thread
        ]
        rejections, examined = self.m._find_rejections(threads, {a, b}, ALLOWED)
        self.assertEqual(rejections, [(a, 10)])
        self.assertEqual(examined, 3)

    def test_untrusted_author_is_ignored(self) -> None:
        path = "tenant-state/intune/Scripts/a.json"
        thread = _thread(10, path)
        thread["comments"].append({"id": 5, "content": "/reject", "author": {"uniqueName": "mallory@evil.test"}})
        self.assertEqual(self.m._find_rejections([thread], {path}, ALLOWED), ([], 1))


class ResetPathsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def _git(self, cwd: str, *args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=cwd, check=True, capture_output=True, text=True,
        ).stdout

    def test_reset_restores_baseline_and_removes_new_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._git(td, "init", "-q", "-b", "main")
            (Path(td) / "kept.json").write_text("baseline\n")
            self._git(td, "add", ".")
            self._git(td, "commit", "-q", "-m", "base")
            (Path(td) / "kept.json").write_text("drifted\n")
            (Path(td) / "new.json").write_text("added by drift\n")
            self._git(td, "add", ".")

            changed = self.m._reset_paths_to_baseline(td, "HEAD", ["kept.json", "new.json", "missing.json"])

            self.assertEqual(changed, 2)
            self.assertEqual((Path(td) / "kept.json").read_text(), "baseline\n")
            self.assertFalse((Path(td) / "new.json").exists())


class MainEndToEndTests(unittest.TestCase):
    """main() against real local repositories: origin (bare), and a working clone."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def _git(self, cwd, *args):
        return subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    def _setup_repos(self, td):
        origin, seed, work = (str(Path(td, n)) for n in ("origin.git", "seed", "work"))
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
        subprocess.run(["git", "clone", "-q", origin, seed], check=True, capture_output=True)
        folder = Path(seed, "tenant-state", "intune", "Scripts")
        folder.mkdir(parents=True)
        (folder / "a.json").write_text("baseline-a\n")
        (folder / "b.json").write_text("baseline-b\n")
        self._git(seed, "checkout", "-q", "-b", "main")
        self._git(seed, "add", ".")
        self._git(seed, "commit", "-q", "-m", "baseline")
        self._git(seed, "push", "-q", "origin", "main")
        self._git(seed, "checkout", "-q", "-b", "drift/intune")
        (folder / "a.json").write_text("drifted-a\n")
        (folder / "b.json").write_text("drifted-b\n")
        self._git(seed, "commit", "-q", "-am", "drift")
        self._git(seed, "push", "-q", "origin", "drift/intune")
        subprocess.run(["git", "clone", "-q", origin, work], check=True, capture_output=True)
        return origin, work

    def _run_main(self, work, threads, posted):
        env = {
            "SYSTEM_ACCESSTOKEN": "tok", "SYSTEM_COLLECTIONURI": "https://dev.azure.com/org", "SYSTEM_TEAMPROJECT": "p",
            "BUILD_REPOSITORY_ID": "r", "PR_DECISION_ALLOWED_REVIEWERS": "reviewer@contoso.com",
        }

        def fake_request(url, token=None, **kwargs):
            return {"value": threads} if "/threads" in url else {"value": [{"pullRequestId": 9}]}

        argv = ["x", "--repo-root", work, "--workload", "intune", "--drift-branch", "drift/intune", "--baseline-branch", "main"]
        with patch.dict(os.environ, env), patch.object(sys, "argv", argv), \
                patch.object(self.m, "_request_json", side_effect=fake_request), \
                patch.object(self.m, "_post_thread_comment", side_effect=lambda **kw: posted.append(kw["thread_id"])):
            return self.m.main()

    def test_rejected_path_is_reset_on_origin_and_thread_is_notified(self) -> None:
        a = "tenant-state/intune/Scripts/a.json"
        b = "tenant-state/intune/Scripts/b.json"
        threads = [_thread(10, a, "/reject"), _thread(11, b, "/accept")]
        posted: list[int] = []
        with tempfile.TemporaryDirectory() as td:
            origin, work = self._setup_repos(td)
            self.assertEqual(self._run_main(work, threads, posted), 0)

            def on_origin(ref, path):
                return self._git(origin, "show", f"{ref}:{path}")

            self.assertEqual(on_origin("drift/intune", a), "baseline-a")  # rejected: back to baseline
            self.assertEqual(on_origin("drift/intune", b), "drifted-b")  # accepted: untouched
            self.assertIn("Apply reviewer /reject decisions (intune)", self._git(origin, "log", "-1", "--format=%s", "drift/intune"))
        self.assertEqual(posted, [10])

    def test_no_reject_decisions_leaves_origin_untouched(self) -> None:
        threads = [_thread(10, "tenant-state/intune/Scripts/a.json", "/accept")]
        posted: list[int] = []
        with tempfile.TemporaryDirectory() as td:
            origin, work = self._setup_repos(td)
            before = self._git(origin, "rev-parse", "drift/intune")
            self.assertEqual(self._run_main(work, threads, posted), 0)
            self.assertEqual(self._git(origin, "rev-parse", "drift/intune"), before)
        self.assertEqual(posted, [])


if __name__ == "__main__":
    unittest.main()
