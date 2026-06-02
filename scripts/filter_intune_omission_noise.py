#!/usr/bin/env python3
"""Restore Intune export files that are intermittently omitted by IntuneCD/Graph API.

Some categories (e.g. macOS shell scripts) are known to occasionally disappear
from exports even though the tenant object still exists.  When a file that is
present in the baseline is missing from the current export, and it belongs to a
known-flaky category, this script restores it from the baseline so that the PR
does not show a spurious deletion.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


# Path fragments (relative to backup root) that are known to be intermittently
# omitted by IntuneCD.  Checked with simple "in" containment against the
# relative POSIX path.
KNOWN_FLAKY_FRAGMENTS: tuple[str, ...] = (
    "Scripts/Shell/",
)


def _run_git_ls_tree(repo_root: Path, ref: str, path: str) -> list[str]:
    """Return list of blob paths under *path* at *ref*."""
    proc = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref, path],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
    )
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.decode("utf-8").splitlines() if line]


def _run_git_show(repo_root: Path, ref: str, rel_path: str) -> bytes | None:
    proc = subprocess.run(
        ["git", "show", f"{ref}:{rel_path}"],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def _is_flaky_path(rel_path: str) -> bool:
    return any(fragment in rel_path for fragment in KNOWN_FLAKY_FRAGMENTS)


def restore_omitted_files(
    repo_root: Path,
    backup_root: Path,
    baseline_ref: str,
) -> tuple[list[str], list[str]]:
    """Restore files from baseline that are missing from export in flaky categories."""
    restored: list[str] = []
    skipped: list[str] = []

    # Determine the prefix inside the repo so we can query git for baseline files
    backup_rel = backup_root.relative_to(repo_root).as_posix()

    baseline_paths = _run_git_ls_tree(repo_root, baseline_ref, backup_rel)

    for baseline_path in baseline_paths:
        # Only consider known-flaky categories
        if not _is_flaky_path(baseline_path):
            continue

        current_file = repo_root / baseline_path
        if current_file.exists():
            # File already present — nothing to restore
            continue

        # File is missing from export but existed in baseline — restore it
        data = _run_git_show(repo_root, baseline_ref, baseline_path)
        if data is None:
            skipped.append(baseline_path)
            continue

        current_file.parent.mkdir(parents=True, exist_ok=True)
        current_file.write_bytes(data)
        restored.append(baseline_path)

    return restored, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument(
        "--backup-root",
        default="tenant-state/intune",
        help="Path to Intune backup root (default: tenant-state/intune).",
    )
    parser.add_argument(
        "--baseline-ref",
        default="HEAD",
        help="Git ref used as baseline for comparison (default: HEAD).",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    backup_root = Path(args.backup_root)
    if not backup_root.is_absolute():
        backup_root = repo_root / backup_root
    backup_root = backup_root.resolve()

    if not backup_root.exists():
        print(f"Backup root not found: {backup_root}")
        return 0

    restored, skipped = restore_omitted_files(
        repo_root=repo_root,
        backup_root=backup_root,
        baseline_ref=args.baseline_ref,
    )

    if restored:
        print(f"Restored {len(restored)} intermittently omitted file(s) from baseline:")
        for path in restored:
            print(f"  + {path}")
    else:
        print("No intermittently omitted files detected.")

    if skipped:
        print(f"Files that could not be restored (skipped): {len(skipped)}")
        for path in skipped:
            print(f"  ! {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
