#!/usr/bin/env python3
"""Revert Intune JSON exports that differ from baseline only in formatting or key ordering."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _run_git_show(repo_root: Path, ref: str, rel_path: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"{ref}:{rel_path}"],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def revert_formatting_only_changes(
    repo_root: Path,
    backup_root: Path,
    baseline_ref: str,
) -> tuple[list[str], list[str]]:
    reverted: list[str] = []
    kept: list[str] = []

    for file_path in sorted(backup_root.rglob("*.json")):
        rel_path = file_path.relative_to(repo_root).as_posix()
        baseline_text = _run_git_show(repo_root, baseline_ref, rel_path)
        if not baseline_text:
            # New file — nothing to revert against
            continue

        try:
            current_text = file_path.read_text(encoding="utf-8")
            current_payload = json.loads(current_text)
            baseline_payload = json.loads(baseline_text)
        except Exception:
            kept.append(rel_path)
            continue

        if current_payload == baseline_payload:
            file_path.write_text(baseline_text, encoding="utf-8")
            reverted.append(rel_path)
        else:
            kept.append(rel_path)

    return reverted, kept


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

    reverted, kept = revert_formatting_only_changes(
        repo_root=repo_root,
        backup_root=backup_root,
        baseline_ref=args.baseline_ref,
    )

    if reverted:
        print(f"Reverted {len(reverted)} formatting-only Intune JSON export(s) to baseline:")
        for path in reverted:
            print(f"  - {path}")
    else:
        print("No formatting-only Intune JSON exports detected.")

    if kept:
        print(f"Files with actual semantic changes (kept): {len(kept)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
