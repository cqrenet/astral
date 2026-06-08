#!/usr/bin/env python3
"""Generate a per-setting CSV inventory from Intune backup JSON files.

Covers Settings Catalog policies (with human-readable setting/value names resolved
from configurationSettings.json) and flat Device Configurations / Compliance Policies.

Output columns: OS, Source, Type, Category, Scoping, Area, Version,
                Policy, Setting, Setting ID, Value
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

OUTPUT_FILE = "policy-settings.csv"

FIELDNAMES = [
    "OS", "Source", "Type", "Category", "Scoping", "Area", "Version",
    "Policy", "Setting", "Setting ID", "Value",
]

# JSON keys to skip when enumerating flat Device Configuration / Compliance Policy objects
_SKIP_KEYS = {
    "@odata.type", "id", "createdDateTime", "lastModifiedDateTime", "version",
    "displayName", "description", "roleScopeTagIds", "scheduledActionsForRule",
    "assignments", "deviceStatusOverview", "userStatusOverview",
    "deviceStatuses", "userStatuses", "deviceManagementApplicabilityRuleOsEdition",
    "deviceManagementApplicabilityRuleOsVersion", "deviceManagementApplicabilityRuleDeviceMode",
    "supportsScopeTags", "settingCount", "priorityMetaData", "creationSource",
    "templateReference", "name", "platforms", "technologies",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Path to backup root (e.g. tenant-state/intune).")
    parser.add_argument("--output-dir", required=True, help="Directory where the CSV will be written.")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Filename metadata
# ---------------------------------------------------------------------------

def _parse_metadata(policy_name: str) -> dict[str, str]:
    """Extract OS/Source/Type/Category/Scoping/Area/Version from naming convention.

    Expected: "Win - OIB - SC - Device Security - D - Audit and Event Logging - v3.7"
    Falls back gracefully for policies that don't match the pattern.
    """
    name = policy_name.replace(".json", "").strip()
    name = re.sub(r"__[0-9a-f\-]{36}$", "", name, flags=re.I)
    name = re.sub(r"_[a-zA-Z]+$", "", name)

    parts = [p.strip() for p in name.split(" - ")]

    if len(parts) >= 7:
        return {
            "OS": parts[0],
            "Source": parts[1],
            "Type": parts[2],
            "Category": parts[3],
            "Scoping": parts[4],
            "Area": parts[5],
            "Version": parts[6],
            "Policy": name,
        }
    if len(parts) >= 4:
        return {
            "OS": parts[0],
            "Source": parts[1] if len(parts) > 1 else "",
            "Type": parts[2] if len(parts) > 2 else "",
            "Category": parts[3] if len(parts) > 3 else "",
            "Scoping": "",
            "Area": "",
            "Version": "",
            "Policy": name,
        }
    return {
        "OS": "", "Source": "", "Type": "", "Category": "",
        "Scoping": "", "Area": "", "Version": "", "Policy": name,
    }


# ---------------------------------------------------------------------------
# Settings-catalog catalog lookup
# ---------------------------------------------------------------------------

def _load_catalog(root: Path) -> dict[str, Any]:
    """Return {settingDefinitionId: definition_dict} from configurationSettings.json."""
    catalog_path = root / "configurationSettings.json"
    if not catalog_path.is_file():
        return {}
    with catalog_path.open(encoding="utf-8") as f:
        raw = json.load(f)
    entries = raw.get("value", raw) if isinstance(raw, dict) else raw
    return {e["id"]: e for e in entries if "id" in e}


def _display_name(catalog: dict[str, Any], setting_id: str) -> str:
    defn = catalog.get(setting_id)
    if defn:
        return defn.get("displayName") or defn.get("name") or setting_id
    tail = setting_id.rsplit("_", 1)[-1]
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", tail).title()


def _choice_label(catalog: dict[str, Any], setting_id: str, value_id: str) -> str:
    defn = catalog.get(setting_id)
    if defn:
        for opt in defn.get("options", []):
            if opt.get("itemId") == value_id:
                return opt.get("displayName") or value_id
    suffix = value_id.removeprefix(setting_id).lstrip("_")
    if suffix == "1":
        return "Enabled"
    if suffix == "0":
        return "Disabled"
    return suffix.title() if suffix.islower() else suffix or value_id


# ---------------------------------------------------------------------------
# Recursive Settings-Catalog setting walker (yields rows)
# ---------------------------------------------------------------------------

def _walk(
    si: dict,
    catalog: dict[str, Any],
    meta: dict[str, str],
    parent_name: str = "",
) -> list[dict]:
    rows: list[dict] = []
    otype = si.get("@odata.type", "")
    sid = si.get("settingDefinitionId", "")
    setting_name = _display_name(catalog, sid)
    if parent_name:
        setting_name = f"{parent_name} > {setting_name}"

    children: list[dict] = []

    if "ChoiceSettingInstance" in otype and "Collection" not in otype:
        csv_val = si.get("choiceSettingValue", {})
        value_id = csv_val.get("value", "")
        value = _choice_label(catalog, sid, value_id)
        rows.append({**meta, "Setting": setting_name, "Setting ID": sid, "Value": value})
        children = csv_val.get("children", [])

    elif "SimpleSettingInstance" in otype and "Collection" not in otype:
        raw = si.get("simpleSettingValue", {})
        value = str(raw.get("value", "")) if isinstance(raw, dict) else str(raw)
        if value:
            rows.append({**meta, "Setting": setting_name, "Setting ID": sid, "Value": value})

    elif "SimpleSettingCollectionInstance" in otype:
        vals = [
            str(v.get("value", "")) if isinstance(v, dict) else str(v)
            for v in si.get("simpleSettingCollectionValue", [])
        ]
        if vals:
            rows.append({**meta, "Setting": setting_name, "Setting ID": sid, "Value": "; ".join(vals)})

    elif "ChoiceSettingCollectionInstance" in otype:
        items = si.get("choiceSettingCollectionValue", [])
        vals = [_choice_label(catalog, sid, item.get("value", "")) for item in items if isinstance(item, dict)]
        if vals:
            rows.append({**meta, "Setting": setting_name, "Setting ID": sid, "Value": "; ".join(vals)})

    elif "GroupSettingCollectionInstance" in otype:
        for group in si.get("groupSettingCollectionValue", []):
            children.extend(group.get("children", []))

    for child in children:
        if isinstance(child, dict):
            rows.extend(_walk(child, catalog, meta, parent_name=setting_name))

    return rows


# ---------------------------------------------------------------------------
# Per-category processors
# ---------------------------------------------------------------------------

def process_settings_catalog(root: Path, catalog: dict[str, Any]) -> list[dict]:
    folder = root / "Settings Catalog"
    rows: list[dict] = []
    if not folder.is_dir():
        return rows
    for path in sorted(folder.glob("*.json")):
        with path.open(encoding="utf-8") as f:
            policy = json.load(f)
        name = policy.get("name") or path.stem
        meta = _parse_metadata(name)
        if not meta["Type"]:
            meta["Type"] = "SC"
        for setting in policy.get("settings", []):
            si = setting.get("settingInstance", {})
            rows.extend(_walk(si, catalog, meta))
    return rows


def _platform_from_type(odata_type: str) -> str:
    t = odata_type.lower()
    if "windows" in t:
        return "Windows"
    if "ios" in t or "iphone" in t:
        return "iOS"
    if "macos" in t or "mac" in t:
        return "macOS"
    if "android" in t:
        return "Android"
    return ""


def process_flat_category(root: Path, category: str) -> list[dict]:
    folder = root / category
    if not folder.is_dir():
        return []
    if (folder / "Policies").is_dir():
        folder = folder / "Policies"
    rows: list[dict] = []
    for path in sorted(folder.glob("*.json")):
        with path.open(encoding="utf-8") as f:
            policy = json.load(f)
        if not isinstance(policy, dict):
            continue
        name = policy.get("displayName") or policy.get("name") or path.stem
        meta = _parse_metadata(name)
        meta["Type"] = meta["Type"] or category
        platform = _platform_from_type(policy.get("@odata.type", ""))
        if platform and not meta["OS"]:
            meta["OS"] = platform
        for key, value in policy.items():
            if key in _SKIP_KEYS:
                continue
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                value_str = json.dumps(value, ensure_ascii=False)
                if len(value_str) > 500:
                    value_str = value_str[:497] + "..."
            else:
                value_str = str(value)
            rows.append({
                **meta,
                "Setting": key,
                "Setting ID": "",
                "Value": value_str,
            })
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    root = Path(args.root)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    catalog = _load_catalog(root)

    rows: list[dict] = []
    rows.extend(process_settings_catalog(root, catalog))
    rows.extend(process_flat_category(root, "Device Configurations"))
    rows.extend(process_flat_category(root, "Compliance Policies"))

    for row in rows:
        for col in FIELDNAMES:
            v = row.get(col, "")
            if "\n" in v or "\r" in v:
                row[col] = v.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")

    out_path = output_dir / OUTPUT_FILE
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"Written {len(rows)} rows to {out_path}")


if __name__ == "__main__":
    main()
