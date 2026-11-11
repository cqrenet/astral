#!/usr/bin/env python3
"""Prepare the restore source folder: full Intune tree, or a selective copy of validated paths.

Env: BACKUP_FOLDER, INTUNE_BACKUP_SUBDIR, RESTORE_MODE (full|selective), RESTORE_PATHS_CSV,
repo root (BUILD_SOURCESDIRECTORY / GITHUB_WORKSPACE), temp dir (AGENT_TEMPDIRECTORY / RUNNER_TEMP).
Sets RESTORE_SOURCE_PATH. Shared by azure-pipelines-restore.yml and .github/workflows/astral-restore.yml.
"""

from __future__ import annotations

import os
import pathlib
import shutil

import common

repo_root = pathlib.Path(os.environ.get("BUILD_SOURCESDIRECTORY") or os.environ["GITHUB_WORKSPACE"]).resolve()
backup_folder = os.environ["BACKUP_FOLDER"]
intune_subdir = os.environ["INTUNE_BACKUP_SUBDIR"]
restore_mode = os.environ.get("RESTORE_MODE", "").strip().lower() or "full"
restore_paths_csv = os.environ.get("RESTORE_PATHS_CSV", "").strip()
temp_root = pathlib.Path(os.environ.get("AGENT_TEMPDIRECTORY") or os.environ["RUNNER_TEMP"]).resolve() / "restore-scope-intune"
intune_root = repo_root / backup_folder / intune_subdir

if not intune_root.is_dir():
  common.ci_log("error", f"Intune restore source root not found: {intune_root}")
  raise SystemExit(1)

if restore_mode == "full":
  print(f"Restore mode: full (source={intune_root})")
  common.ci_set_env("RESTORE_SOURCE_PATH", intune_root)
  raise SystemExit(0)

if restore_mode not in {"selective", "paths"}:
  common.ci_log("error", f"Unsupported restoreMode '{restore_mode}'. Use 'full' or 'selective'.")
  raise SystemExit(1)

raw_items = [item.strip() for item in restore_paths_csv.replace("\n", ",").split(",")]
raw_items = [item for item in raw_items if item]
if not raw_items:
  common.ci_log("error", f"restoreMode=selective requires restorePathsCsv with at least one path.")
  raise SystemExit(1)

backup_prefix = f"{backup_folder}/{intune_subdir}/"
copied = []
errors = []

if temp_root.exists():
  shutil.rmtree(temp_root)
temp_root.mkdir(parents=True, exist_ok=True)

def normalize_to_intune_relative(path_text):
  p = path_text.replace("\\", "/").lstrip("./")
  if p.startswith("/"):
    return None
  if p.startswith(backup_prefix):
    p = p[len(backup_prefix):]
  elif p.startswith(f"{intune_subdir}/"):
    p = p[len(intune_subdir) + 1 :]
  return p.strip("/")

for item in raw_items:
  rel = normalize_to_intune_relative(item)
  if not rel:
    errors.append(f"Invalid path '{item}'")
    continue

  parts = pathlib.PurePosixPath(rel).parts
  if any(part in {"..", ""} for part in parts):
    errors.append(f"Path traversal is not allowed: '{item}'")
    continue

  src = intune_root.joinpath(*parts)
  if not src.is_file():
    errors.append(f"Path not found in selected baseline snapshot: '{item}' -> '{src}'")
    continue

  dst = temp_root.joinpath(*parts)
  dst.parent.mkdir(parents=True, exist_ok=True)
  shutil.copy2(src, dst)
  copied.append(rel)

if errors:
  for message in errors:
    common.ci_log("error", f"{message}")
  raise SystemExit(1)

if not copied:
  common.ci_log("error", f"No files were prepared for selective restore.")
  raise SystemExit(1)

print(f"Restore mode: selective (files={len(copied)})")
for rel in copied:
  print(f" - {backup_folder}/{intune_subdir}/{rel}")
common.ci_set_env("RESTORE_SOURCE_PATH", temp_root)
