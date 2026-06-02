#!/usr/bin/env python3
"""Revert Entra JSON file edits when only enrichment metadata changed."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any


ENRICHMENT_KEY_NAMES = {
    "ownersresolved",
    "approleassignmentsresolved",
    "requiredresourceaccessresolved",
    "appownerorganizationresolved",
    "resolutionstatus",
}


def _to_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _run_git(repo_root: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
    )
    if check and proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed ({proc.returncode}): {stderr}")
    return proc


def _strip_enrichment(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, child in value.items():
            if str(key).strip().lower() in ENRICHMENT_KEY_NAMES:
                continue
            cleaned[key] = _strip_enrichment(child)
        return cleaned
    if isinstance(value, list):
        return [_strip_enrichment(item) for item in value]
    return value


def _is_enrichment_only_change(old_text: str, new_text: str) -> bool:
    if not old_text or not new_text:
        return False
    try:
        old_payload = json.loads(old_text)
        new_payload = json.loads(new_text)
    except Exception:
        return False
    if not isinstance(old_payload, dict) or not isinstance(new_payload, dict):
        return False

    old_stripped = _strip_enrichment(old_payload)
    new_stripped = _strip_enrichment(new_payload)
    if old_stripped != new_stripped:
        return False
    return old_payload != new_payload


def _modified_paths(repo_root: Path, workload_root: str) -> list[str]:
    proc = _run_git(
        repo_root,
        ["diff", "--name-only", "-z", "--diff-filter=M", "--", workload_root],
        check=True,
    )
    raw = proc.stdout.split(b"\x00")
    paths: list[str] = []
    for chunk in raw:
        text = chunk.decode("utf-8", errors="replace").strip()
        if text:
            paths.append(text)
    return paths


def _is_json_path(path: str) -> bool:
    return PurePosixPath(path.replace("\\", "/")).suffix.lower() == ".json"


def filter_enrichment_only_files(repo_root: Path, workload_root: str) -> list[str]:
    reverted: list[str] = []
    for rel_path in _modified_paths(repo_root, workload_root):
        if not _is_json_path(rel_path):
            continue

        head_proc = _run_git(repo_root, ["show", f"HEAD:{rel_path}"], check=False)
        if head_proc.returncode != 0:
            continue
        old_text = head_proc.stdout.decode("utf-8", errors="replace")

        abs_path = repo_root / rel_path
        if not abs_path.is_file():
            continue
        new_text = abs_path.read_text(encoding="utf-8")

        if _is_enrichment_only_change(old_text, new_text):
            _run_git(repo_root, ["checkout", "--quiet", "--", rel_path], check=True)
            reverted.append(rel_path)
    return reverted


def find_enrichment_only_modified_files(repo_root: Path, workload_root: str) -> list[str]:
    matches: list[str] = []
    for rel_path in _modified_paths(repo_root, workload_root):
        if not _is_json_path(rel_path):
            continue

        head_proc = _run_git(repo_root, ["show", f"HEAD:{rel_path}"], check=False)
        if head_proc.returncode != 0:
            continue
        old_text = head_proc.stdout.decode("utf-8", errors="replace")

        abs_path = repo_root / rel_path
        if not abs_path.is_file():
            continue
        new_text = abs_path.read_text(encoding="utf-8")

        if _is_enrichment_only_change(old_text, new_text):
            matches.append(rel_path)
    return matches


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, help="Repository root path.")
    parser.add_argument(
        "--workload-root",
        default="tenant-state/entra",
        help="Path scope inside repo to inspect (default: tenant-state/entra).",
    )
    parser.add_argument(
        "--fail-on-residual-enrichment-drift",
        default="true",
        help="Exit non-zero when enrichment-only modified files remain after filtering (true/false).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    reverted = filter_enrichment_only_files(repo_root=repo_root, workload_root=args.workload_root)
    if reverted:
        print(f"Reverted enrichment-only Entra file changes: {len(reverted)}")
        for path in reverted:
            print(f" - {path}")
    else:
        print("No enrichment-only Entra file changes detected.")

    residual = find_enrichment_only_modified_files(repo_root=repo_root, workload_root=args.workload_root)
    if residual:
        print(f"Residual enrichment-only Entra file changes still present: {len(residual)}")
        for path in residual:
            print(f" - {path}")
        if _to_bool(args.fail_on_residual_enrichment_drift):
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
