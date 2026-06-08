#!/usr/bin/env python3
"""Validate backup outputs for Intune and Entra workloads."""

from __future__ import annotations

import argparse
from pathlib import Path


def to_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", required=True, choices=["intune", "entra"])
    parser.add_argument("--mode", default="light", choices=["light", "full"])
    parser.add_argument("--root", required=True, help="Workload backup root path.")
    parser.add_argument("--reports-root", default="", help="Workload reports root path. When omitted, report file checks are skipped.")
    parser.add_argument("--include-named-locations", default="false")
    parser.add_argument("--include-authentication-strengths", default="false")
    parser.add_argument("--include-conditional-access", default="false")
    parser.add_argument("--include-enterprise-applications", default="false")
    parser.add_argument("--include-enterprise-applications-effective", default="false")
    parser.add_argument("--include-app-registrations", default="false")
    parser.add_argument("--include-app-registrations-effective", default="false")
    return parser.parse_args()


def _require_file(path: Path, label: str, errors: list[str]) -> None:
    if not path.is_file():
        errors.append(f"Missing {label}: {path}")


def _json_count(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(1 for _ in root.rglob("*.json"))


def _validate_intune(root: Path, reports_root: Path | None, errors: list[str]) -> None:
    if not root.exists():
        errors.append(f"Missing Intune backup root: {root}")
        return

    json_count = _json_count(root)
    if json_count == 0:
        errors.append(f"Intune backup root has no JSON exports: {root}")

    if reports_root is not None:
        _require_file(reports_root / "policy-assignments.md", "Intune assignment markdown report", errors)
        _require_file(reports_root / "policy-assignments.csv", "Intune assignment CSV report", errors)
        _require_file(reports_root / "object-inventory-all.csv", "Intune object inventory CSV", errors)

    if errors:
        return
    print(f"Intune output validation passed: jsonFiles={json_count}")


def _validate_entra(root: Path, reports_root: Path | None, args: argparse.Namespace, errors: list[str]) -> None:
    if not root.exists():
        errors.append(f"Missing Entra backup root: {root}")
        return

    include_named_locations = to_bool(args.include_named_locations)
    include_auth_strengths = to_bool(args.include_authentication_strengths)
    include_conditional_access = to_bool(args.include_conditional_access)
    include_enterprise_apps = to_bool(args.include_enterprise_applications)
    include_enterprise_apps_effective = to_bool(args.include_enterprise_applications_effective)
    include_app_registrations = to_bool(args.include_app_registrations)
    include_app_registrations_effective = to_bool(args.include_app_registrations_effective)

    expected_category_indexes: list[tuple[str, bool]] = [
        ("Named Locations", include_named_locations),
        ("Authentication Strengths", include_auth_strengths),
        ("Conditional Access", include_conditional_access),
        ("App Registrations", include_app_registrations_effective),
        ("Enterprise Applications", include_enterprise_apps_effective),
    ]

    for category_name, is_required in expected_category_indexes:
        if not is_required:
            continue
        index_path = root / category_name / f"{category_name}.md"
        _require_file(index_path, f"Entra export index for '{category_name}'", errors)

    if reports_root is not None:
        _require_file(reports_root / "object-inventory-all.csv", "Entra object inventory CSV", errors)

        if include_conditional_access:
            _require_file(reports_root / "policy-assignments.md", "Entra assignment markdown report", errors)
            _require_file(reports_root / "policy-assignments.csv", "Entra assignment CSV report", errors)

        if include_app_registrations_effective or include_enterprise_apps_effective:
            _require_file(reports_root / "apps-inventory.csv", "Entra apps inventory CSV", errors)

    if errors:
        return

    json_count = _json_count(root)
    print(
        "Entra output validation passed: "
        f"jsonFiles={json_count}, "
        f"mode={args.mode}, "
        f"enterpriseAppsConfigured={str(include_enterprise_apps).lower()}, "
        f"enterpriseAppsEffective={str(include_enterprise_apps_effective).lower()}, "
        f"appRegistrationsConfigured={str(include_app_registrations).lower()}, "
        f"appRegistrationsEffective={str(include_app_registrations_effective).lower()}"
    )


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    reports_root = Path(args.reports_root).resolve() if args.reports_root.strip() else None
    errors: list[str] = []

    if args.workload == "intune":
        _validate_intune(root=root, reports_root=reports_root, errors=errors)
    else:
        _validate_entra(root=root, reports_root=reports_root, args=args, errors=errors)

    if errors:
        print("Backup output validation failed:")
        for item in errors:
            print(f" - {item}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
