#!/usr/bin/env python3
"""Resolve Intune assignment group IDs to display names (and restore missing group IDs).

Env: BUILD_SOURCESDIRECTORY (repo root), BACKUP_FOLDER (e.g. tenant-state/intune), ACCESS_TOKEN (Graph).
Shared by azure-pipelines.yml and .github/workflows/astral-backup.yml.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import urllib.error
import urllib.parse
import urllib.request

root = pathlib.Path(os.environ["BUILD_SOURCESDIRECTORY"]) / os.environ["BACKUP_FOLDER"]
token = os.environ.get("ACCESS_TOKEN", "").strip()
if not token:
  print("No Graph token available. Skipping assignment group name enrichment.")
  raise SystemExit(0)

if not root.exists():
  print(f"Backup folder not found: {root}. Skipping assignment group name enrichment.")
  raise SystemExit(0)

group_target_type = "#microsoft.graph.groupAssignmentTarget"
guid_pattern = re.compile(
  r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

cache = {}
assignment_cache = {}
looked_up = 0
updated_files = 0
group_targets_seen = 0
group_targets_without_id_before = 0
group_targets_without_id_after = 0
files_with_missing_group_targets = 0
files_with_restored_group_ids = 0
restored_group_ids = 0
assignment_endpoint_hits = 0

assignment_endpoint_templates = [
  "https://graph.microsoft.com/beta/deviceManagement/deviceConfigurations/{object_id}/assignments",
  "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies/{object_id}/assignments",
  "https://graph.microsoft.com/beta/deviceManagement/groupPolicyConfigurations/{object_id}/assignments",
  "https://graph.microsoft.com/beta/deviceManagement/deviceCompliancePolicies/{object_id}/assignments",
  "https://graph.microsoft.com/beta/deviceManagement/deviceHealthScripts/{object_id}/assignments",
  "https://graph.microsoft.com/beta/deviceManagement/deviceManagementScripts/{object_id}/assignments",
  "https://graph.microsoft.com/beta/deviceManagement/deviceShellScripts/{object_id}/assignments",
  "https://graph.microsoft.com/beta/deviceManagement/deviceCustomAttributeShellScripts/{object_id}/assignments",
  "https://graph.microsoft.com/beta/deviceManagement/deviceEnrollmentConfigurations/{object_id}/assignments",
  "https://graph.microsoft.com/beta/deviceManagement/windowsAutopilotDeploymentProfiles/{object_id}/assignments",
  "https://graph.microsoft.com/beta/deviceAppManagement/mobileApps/{object_id}/assignments",
  "https://graph.microsoft.com/beta/deviceAppManagement/mobileAppConfigurations/{object_id}/assignments",
  "https://graph.microsoft.com/beta/deviceAppManagement/targetedManagedAppConfigurations/{object_id}/assignments",
  "https://graph.microsoft.com/beta/deviceAppManagement/androidManagedAppProtections/{object_id}/assignments",
  "https://graph.microsoft.com/beta/deviceAppManagement/iosManagedAppProtections/{object_id}/assignments",
]

def object_id_from_filename(file_path: pathlib.Path):
  match = guid_pattern.search(file_path.name)
  if not match:
    return None
  return match.group(1)

def resolve_group_name(group_id: str):
  nonlocal_vars["looked_up"] += 1
  if group_id in cache:
    return cache[group_id]
  url = (
    "https://graph.microsoft.com/v1.0/groups/"
    + urllib.parse.quote(group_id)
    + "?$select=displayName"
  )
  req = urllib.request.Request(
    url,
    headers={
      "Authorization": f"Bearer {token}",
      "Accept": "application/json",
    },
    method="GET",
  )
  try:
    with urllib.request.urlopen(req, timeout=20) as resp:
      payload = json.loads(resp.read().decode("utf-8"))
      name = payload.get("displayName")
  except urllib.error.HTTPError as exc:
    print(f"Warning: unable to resolve group {group_id} (HTTP {exc.code})")
    name = None
  except Exception as exc:
    print(f"Warning: unable to resolve group {group_id} ({exc})")
    name = None
  cache[group_id] = name
  return name

def assignment_signature(assignment):
  if not isinstance(assignment, dict):
    return (None, None, None, None, None)
  target = assignment.get("target")
  if not isinstance(target, dict):
    return (assignment.get("intent"), assignment.get("source"), None, None, None)
  return (
    assignment.get("intent"),
    assignment.get("source"),
    target.get("@odata.type"),
    target.get("deviceAndAppManagementAssignmentFilterId"),
    target.get("deviceAndAppManagementAssignmentFilterType"),
  )

def fetch_assignments(object_id: str):
  if object_id in assignment_cache:
    return assignment_cache[object_id]
  safe_id = urllib.parse.quote(object_id)
  for endpoint_template in assignment_endpoint_templates:
    url = endpoint_template.format(object_id=safe_id)
    req = urllib.request.Request(
      url,
      headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
      },
      method="GET",
    )
    try:
      with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
      if exc.code in (400, 404):
        continue
      print(f"Warning: assignment query failed for object {object_id} at {url} (HTTP {exc.code})")
      continue
    except Exception as exc:
      print(f"Warning: assignment query failed for object {object_id} at {url} ({exc})")
      continue

    value = payload.get("value")
    if isinstance(value, list):
      assignment_cache[object_id] = (value, url)
      return value, url

  assignment_cache[object_id] = ([], None)
  return assignment_cache[object_id]

def collect_group_assignments(node):
  found = []
  if isinstance(node, dict):
    target = node.get("target")
    if isinstance(target, dict) and target.get("@odata.type") == group_target_type:
      found.append(node)
    for value in node.values():
      found.extend(collect_group_assignments(value))
  elif isinstance(node, list):
    for item in node:
      found.extend(collect_group_assignments(item))
  return found

nonlocal_vars = {"looked_up": 0}
json_files = sorted(root.rglob("*.json"))
for file_path in json_files:
  try:
    content = json.loads(file_path.read_text(encoding="utf-8"))
  except Exception:
    continue

  file_changed = False
  local_group_assignments = collect_group_assignments(content)
  if not local_group_assignments:
    continue

  file_missing_before = 0
  file_missing_assignments = []
  for assignment in local_group_assignments:
    target = assignment.get("target", {})
    group_targets_seen += 1
    group_id = target.get("groupId")
    if isinstance(group_id, str) and group_id:
      group_name = resolve_group_name(group_id)
      if group_name and target.get("groupDisplayName") != group_name:
        target["groupDisplayName"] = group_name
        file_changed = True
    else:
      group_targets_without_id_before += 1
      file_missing_before += 1
      file_missing_assignments.append(assignment)

  if file_missing_before > 0:
    files_with_missing_group_targets += 1
    object_id = object_id_from_filename(file_path)
    if object_id:
      remote_assignments, matched_endpoint = fetch_assignments(object_id)
      if matched_endpoint:
        assignment_endpoint_hits += 1
      remote_group_assignments = []
      for remote_assignment in remote_assignments:
        if not isinstance(remote_assignment, dict):
          continue
        remote_target = remote_assignment.get("target")
        if not isinstance(remote_target, dict):
          continue
        if remote_target.get("@odata.type") != group_target_type:
          continue
        remote_group_id = remote_target.get("groupId")
        if isinstance(remote_group_id, str) and remote_group_id:
          remote_group_assignments.append(remote_assignment)

      if remote_group_assignments:
        by_signature = {}
        for item in remote_group_assignments:
          by_signature.setdefault(assignment_signature(item), []).append(item)
        used_remote = set()
        file_restored_count = 0

        for local_assignment in file_missing_assignments:
          selected_remote = None
          sig = assignment_signature(local_assignment)
          candidates = by_signature.get(sig, [])
          for candidate in candidates:
            candidate_id = id(candidate)
            if candidate_id in used_remote:
              continue
            selected_remote = candidate
            used_remote.add(candidate_id)
            break
          if selected_remote is None:
            for candidate in remote_group_assignments:
              candidate_id = id(candidate)
              if candidate_id in used_remote:
                continue
              selected_remote = candidate
              used_remote.add(candidate_id)
              break
          if selected_remote is None:
            continue

          remote_target = selected_remote.get("target", {})
          remote_group_id = remote_target.get("groupId")
          if not (isinstance(remote_group_id, str) and remote_group_id):
            continue

          local_target = local_assignment.get("target", {})
          local_target["groupId"] = remote_group_id
          remote_group_name = remote_target.get("groupDisplayName") or remote_target.get("groupName")
          if isinstance(remote_group_name, str) and remote_group_name:
            local_target["groupDisplayName"] = remote_group_name
          else:
            group_name = resolve_group_name(remote_group_id)
            if group_name:
              local_target["groupDisplayName"] = group_name
          file_changed = True
          file_restored_count += 1
          restored_group_ids += 1

        if file_restored_count > 0:
          files_with_restored_group_ids += 1

  file_missing_after = 0
  for assignment in local_group_assignments:
    target = assignment.get("target", {})
    group_id = target.get("groupId")
    if not (isinstance(group_id, str) and group_id):
      file_missing_after += 1
  group_targets_without_id_after += file_missing_after

  if file_changed:
    file_path.write_text(json.dumps(content, indent=5, ensure_ascii=False) + "\n", encoding="utf-8")
    updated_files += 1

  looked_up = nonlocal_vars["looked_up"]
print(
  "Assignment group name enrichment complete. "
  + f"Files updated: {updated_files}. "
  + f"Group lookups performed: {looked_up}. "
  + f"Assignment endpoints matched: {assignment_endpoint_hits}. "
  + f"Group IDs restored from Graph assignments: {restored_group_ids}."
)
if files_with_missing_group_targets > 0:
  print(
    "Assignment targets missing groupId before fallback: "
    + str(group_targets_without_id_before)
    + " across "
    + str(files_with_missing_group_targets)
    + " files."
  )
if files_with_restored_group_ids > 0:
  print(
    "Assignment targets restored from endpoint fallback in "
    + str(files_with_restored_group_ids)
    + " files."
  )
if group_targets_seen > 0 and group_targets_without_id_after > 0:
  print(
    "Warning: "
    + str(group_targets_without_id_after)
    + " of "
    + str(group_targets_seen)
    + " group assignment targets still do not include groupId after fallback."
  )
  print("Warning: when groupId is unavailable from source APIs, groupDisplayName cannot be resolved.")
