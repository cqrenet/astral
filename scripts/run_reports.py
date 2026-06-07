#!/usr/bin/env python3
"""Dispatch report and documentation generation from reports/definitions.yml.

Reads the definitions file, evaluates per-entry conditions against environment
variables, and runs each applicable script or documentation command for the
requested workload.  Replaces the individual hardcoded task blocks in the
pipeline YAML — add new reports to definitions.yml, not to the pipeline.

Usage (pipeline):
    python3 run_reports.py --workload intune  \\
        --backup-dir  .../tenant-state/intune \\
        --reports-dir .../tenant-state/reports/intune \\
        --repo-root   ... \\
        --mode "$(FULL_RUN)"

Usage (local):
    python3 scripts/run_reports.py --workload entra \\
        --backup-dir  ./tenant-state/entra \\
        --reports-dir ./tenant-state/reports/entra \\
        --repo-root   . \\
        --mode full \\
        --type reports
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# YAML loading — PyYAML is required; fail early with a clear message.
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        raise SystemExit(
            "PyYAML is required to read report definitions.\n"
            "Install it with:  pip3 install PyYAML"
        )
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _merge_definitions(base: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    """Merge local definitions on top of base definitions.

    Entries with the same name in local override the matching base entry.
    Entries only in local are appended.  The ``source`` field is injected to
    track where each entry came from (``builtin`` vs ``local``).
    """
    merged: dict[str, Any] = {"version": base.get("version", 1)}
    for section in ("reports", "documentation"):
        combined: dict[str, Any] = {}
        for entry in base.get(section, []):
            if "name" in entry:
                combined[entry["name"]] = {**entry, "source": "builtin"}
        for entry in local.get(section, []):
            if "name" in entry:
                combined[entry["name"]] = {**entry, "source": "local"}
        merged[section] = list(combined.values())
    return merged


def _load_definitions(definitions_path: Path) -> dict[str, Any]:
    """Load base definitions and overlay local extensions if present."""
    base = _load_yaml(definitions_path)
    local_path = definitions_path.parent / "definitions.local.yml"
    if local_path.is_file():
        local = _load_yaml(local_path)
        return _merge_definitions(base, local)
    return _merge_definitions(base, {})


# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------

def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _eval_condition(condition: Any, workload: str, mode: str) -> bool:
    """Return True when the condition is satisfied for *workload* in *mode*."""
    if condition is None:
        return True
    if isinstance(condition, dict):
        # Per-workload condition: absent key means "always run for that workload".
        cond = condition.get(workload)
        return _eval_condition(cond, workload, mode)
    if isinstance(condition, str):
        c = condition.strip()
        if c == "full_run":
            return mode == "full"
        if " or " in c:
            return any(_eval_condition(p.strip(), workload, mode) for p in c.split(" or "))
        if " and " in c:
            return all(_eval_condition(p.strip(), workload, mode) for p in c.split(" and "))
        return _env_bool(c)
    return True


# ---------------------------------------------------------------------------
# Argument rendering
# ---------------------------------------------------------------------------

def _render(args: list[str], ctx: dict[str, str]) -> list[str]:
    return [a.format(**ctx) for a in args]


# ---------------------------------------------------------------------------
# Report runner
# ---------------------------------------------------------------------------

def _run_report(defn: dict[str, Any], ctx: dict[str, str], script_root: Path) -> bool:
    script = script_root / defn["script"]
    cmd = [sys.executable, str(script)] + _render(defn.get("args", []), ctx)
    print(f"  run: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(
            f"  ERROR: report '{defn['name']}' exited with code {result.returncode}",
            file=sys.stderr,
        )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Documentation runner (IntuneCD-startdocumentation)
# ---------------------------------------------------------------------------

def _run_documentation(defn: dict[str, Any], ctx: dict[str, str]) -> bool:
    split = _env_bool("SPLIT_DOCUMENTATION")
    backup_path = defn["backup_path"].format(**ctx)
    output_path = defn["output_path"].format(**ctx)
    tenant_name = os.environ.get("TENANT_NAME", "").strip()
    repo_uri = os.environ.get("BUILD_REPOSITORY_URI", "").strip()

    intro = (
        f'Intune backup and documentation generated at {repo_uri}'
        f' <img align="right" width="96" height="96" src="./logo.png">'
        if repo_uri else ""
    )

    cmd = [
        "IntuneCD-startdocumentation",
        f"--path={backup_path}",
        f"--tenantname={tenant_name}",
    ]
    if intro:
        cmd.append(f"--intro={intro}")
    if defn.get("enrich", False):
        cmd.append("--enrich-documentation")

    if split:
        cmd.append("--split")
        print(f"  run (split): {' '.join(cmd)}")
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            print(
                f"  ERROR: documentation '{defn['name']}' exited with code {result.returncode}",
                file=sys.stderr,
            )
            return False
        # Rewrite relative links in split index.md and write to output_path.
        # Mirrors the sed command that was previously inlined in the pipeline:
        #   s#](\./#](./$(BACKUP_FOLDER)/$(INTUNE_BACKUP_SUBDIR)/#g
        index = Path(backup_path) / "index.md"
        if index.is_file():
            content = index.read_text(encoding="utf-8")
            backup_folder = ctx.get("backup_folder", "")
            backup_subdir = ctx.get("backup_subdir", "")
            if backup_folder and backup_subdir:
                content = content.replace(
                    "](./", f"](./{backup_folder}/{backup_subdir}/"
                )
            Path(output_path).write_text(content, encoding="utf-8")
    else:
        cmd.append(f"--outpath={output_path}")
        print(f"  run: {' '.join(cmd)}")
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            print(
                f"  ERROR: documentation '{defn['name']}' exited with code {result.returncode}",
                file=sys.stderr,
            )
            return False

    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", required=True, choices=["intune", "entra"])
    parser.add_argument("--backup-dir", required=True, help="Workload backup root directory.")
    parser.add_argument("--reports-dir", required=True, help="Workload reports output directory.")
    parser.add_argument("--repo-root", required=True, help="Repository root directory.")
    parser.add_argument(
        "--mode",
        default="light",
        help="Run mode: 'full', '1' (full) or 'light', '0' (light). Default: light.",
    )
    parser.add_argument(
        "--type",
        dest="run_type",
        default="all",
        choices=["all", "reports", "documentation"],
        help="Which definition type to dispatch. Default: all.",
    )
    parser.add_argument(
        "--definitions",
        default=None,
        help=(
            "Path to definitions YAML. "
            "Defaults to reports/definitions.yml inside --repo-root."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Normalise mode to "full" or "light"
    mode = "full" if args.mode in ("full", "1") else "light"

    definitions_path = (
        Path(args.definitions)
        if args.definitions
        else Path(args.repo_root) / "reports" / "definitions.yml"
    )
    if not definitions_path.is_file():
        print(f"ERROR: definitions file not found: {definitions_path}", file=sys.stderr)
        return 1

    defs = _load_definitions(definitions_path)
    script_root = Path(args.repo_root) / "scripts"

    # Compute the relative components of backup_dir for split-doc link rewriting
    backup_dir = Path(args.backup_dir).resolve()
    repo_root_path = Path(args.repo_root).resolve()
    try:
        rel_parts = backup_dir.relative_to(repo_root_path).parts
        backup_folder = rel_parts[0] if rel_parts else ""
        backup_subdir = rel_parts[1] if len(rel_parts) > 1 else (rel_parts[0] if rel_parts else "")
    except ValueError:
        backup_folder = ""
        backup_subdir = backup_dir.name

    ctx: dict[str, str] = {
        "backup_dir": str(backup_dir),
        "reports_dir": str(Path(args.reports_dir).resolve()),
        "repo_root": str(repo_root_path),
        "backup_folder": backup_folder,
        "backup_subdir": backup_subdir,
    }

    failed: list[str] = []

    if args.run_type in ("all", "reports"):
        for defn in defs.get("reports", []):
            if args.workload not in defn.get("workloads", []):
                continue
            if not _eval_condition(defn.get("condition"), args.workload, mode):
                print(f"skip (condition not met): {defn['name']}")
                continue
            print(f"Generating report: {defn['name']}")
            if not _run_report(defn, ctx, script_root):
                failed.append(defn["name"])

    if args.run_type in ("all", "documentation"):
        for defn in defs.get("documentation", []):
            if args.workload not in defn.get("workloads", []):
                continue
            if not _eval_condition(defn.get("condition"), args.workload, mode):
                print(f"skip (condition not met): {defn['name']}")
                continue
            print(f"Generating documentation: {defn['name']}")
            if not _run_documentation(defn, ctx):
                failed.append(defn["name"])

    if failed:
        print(f"FAILED: {', '.join(failed)}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
