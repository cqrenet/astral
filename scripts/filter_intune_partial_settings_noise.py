#!/usr/bin/env python3
"""Revert Intune Settings Catalog partial exports where settings payload is missing."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def _to_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


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


def _is_settings_catalog_json(file_path: Path, backup_root: Path) -> bool:
    if file_path.suffix.lower() != ".json":
        return False
    rel = file_path.relative_to(backup_root).as_posix().lower()
    return rel.startswith("settings catalog/")


def _is_partial_settings_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    setting_count = payload.get("settingCount")
    if not isinstance(setting_count, int) or setting_count <= 0:
        return False
    settings = payload.get("settings")
    if not isinstance(settings, list):
        return True
    return len(settings) == 0


def restore_partial_settings_from_baseline(
    repo_root: Path,
    backup_root: Path,
    baseline_ref: str,
) -> tuple[list[str], list[str]]:
    restored: list[str] = []
    unresolved: list[str] = []

    for file_path in sorted(backup_root.rglob("*.json")):
        if not _is_settings_catalog_json(file_path, backup_root):
            continue

        try:
            current_payload = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if not _is_partial_settings_payload(current_payload):
            continue

        rel_path = file_path.relative_to(repo_root).as_posix()
        baseline_text = _run_git_show(repo_root, baseline_ref, rel_path)
        if not baseline_text:
            unresolved.append(rel_path)
            continue

        try:
            baseline_payload = json.loads(baseline_text)
        except Exception:
            unresolved.append(rel_path)
            continue

        baseline_settings = baseline_payload.get("settings")
        if not isinstance(baseline_settings, list) or len(baseline_settings) == 0:
            unresolved.append(rel_path)
            continue

        current_payload["settings"] = baseline_settings
        file_path.write_text(json.dumps(current_payload, indent=5, ensure_ascii=False), encoding="utf-8")
        restored.append(rel_path)

    return restored, unresolved


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, help="Repository root path.")
    parser.add_argument(
        "--backup-root",
        default="tenant-state/intune",
        help="Path to Intune backup root (default: tenant-state/intune).",
    )
    parser.add_argument(
        "--baseline-ref",
        default="HEAD",
        help="Git ref used as baseline for restoration (default: HEAD).",
    )
    parser.add_argument(
        "--fail-on-unresolved-partial-exports",
        default="true",
        help="Exit non-zero when partial exports cannot be restored from baseline (true/false).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    backup_root_arg = Path(args.backup_root)
    backup_root = backup_root_arg if backup_root_arg.is_absolute() else repo_root / backup_root_arg
    backup_root = backup_root.resolve()

    restored, unresolved = restore_partial_settings_from_baseline(
        repo_root=repo_root,
        backup_root=backup_root,
        baseline_ref=args.baseline_ref,
    )

    if restored:
        print(f"Restored partial Intune Settings Catalog exports from baseline: {len(restored)}")
        for path in restored:
            print(f" - {path}")
    else:
        print("No partial Intune Settings Catalog exports detected.")

    if unresolved:
        print(f"Unresolved partial Intune Settings Catalog exports: {len(unresolved)}")
        for path in unresolved:
            print(f" - {path}")
        if _to_bool(args.fail_on_unresolved_partial_exports):
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
