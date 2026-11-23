#!/usr/bin/env python3
"""Active Directory backup for a domain-joined runner: export, scrub, validate, commit drift.

Pure Python (no bash), because the runner is Windows. Steps:
  1. Branch the drift branch off the baseline.
  2. Run scripts/export_ad_baseline.ps1 into <workload-root>.
  3. Canonicalize JSON, drop report timestamps, redact GPP cpassword values.
  4. Check the export is complete.
  5. Name the administrator behind each change from the domain controllers' directory-change events
     (5136/5137/5139/5141; scripts/export_ad_changes.ps1), one commit per set of authors.
  6. Push (CHANGE_DETECTED / ROLLING_PR_SYNC_REQUIRED outputs).

Files without a matching event (auditing off, controller unreachable, change by a machine account) are committed
under the service identity. Attribution never fails the backup.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

import common

log = common.log

REDACTED = "[REDACTED]"
# Group Policy Preferences store passwords as a reversible "cpassword"; as an XML attribute or element.
_CPASSWORD_ATTR = re.compile(r'(\bcpassword\s*=\s*)(["\'])(.*?)\2', re.IGNORECASE | re.DOTALL)
_CPASSWORD_ELEM = re.compile(r"(<cpassword>)(.*?)(</cpassword>)", re.IGNORECASE | re.DOTALL)
# Get-GPOReport stamps every report with the time it was read: pure noise in a diff.
_READ_TIME = re.compile(r"[ \t]*<ReadTime>.*?</ReadTime>[ \t]*\r?\n?", re.IGNORECASE | re.DOTALL)
_XML_ENCODING = re.compile(r'(<\?xml[^>]*?encoding\s*=\s*)(["\'])[^"\']*\2', re.IGNORECASE)
# Folders every complete export has (the optional categories depend on the -Include flags).
REQUIRED_FILES = ("Domain/Domain.json",)


def scrub_text(text: str) -> tuple[str, int]:
    """Strip volatile and secret content from an XML/text report; returns (text, redactions)."""
    count = 0

    def _attr(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}{match.group(2)}{REDACTED}{match.group(2)}"

    def _elem(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}{REDACTED}{match.group(3)}"

    text = _CPASSWORD_ATTR.sub(_attr, text)
    text = _CPASSWORD_ELEM.sub(_elem, text)
    text = _READ_TIME.sub("", text)
    text = _XML_ENCODING.sub(r"\1\2utf-8\2", text, count=1)
    return text.replace("\r\n", "\n"), count


def _read(path: pathlib.Path) -> str:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def normalize_tree(root: pathlib.Path) -> int:
    """Rewrite every exported file in canonical form; returns the number of redacted secrets."""
    redactions = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.suffix.lower() == ".json":
            data = json.loads(_read(path))
            text, count = json.dumps(data, indent=4, ensure_ascii=False, sort_keys=True) + "\n", 0
        else:
            text, count = scrub_text(_read(path))
        redactions += count
        path.write_bytes(text.encode("utf-8"))
    return redactions


def validate_export(root: pathlib.Path, include_gpos: bool) -> list[str]:
    errors = [f"Missing {name}" for name in REQUIRED_FILES if not (root / name).is_file()]
    if include_gpos and not any((root / "Group Policy Objects").glob("*.xml")):
        errors.append("No Group Policy Objects exported (a domain always has the Default Domain Policy)")
    return errors


def find_powershell(explicit: str = "") -> str:
    """Windows PowerShell first: the AD and GroupPolicy modules are native there, not in PowerShell 7."""
    for candidate in (explicit, "powershell", "pwsh"):
        if candidate and shutil.which(candidate):
            return shutil.which(candidate) or candidate
    raise SystemExit("PowerShell not found; the AD runner needs Windows PowerShell with RSAT (see docs/active-directory-setup.md)")


def run_export(powershell: str, script: pathlib.Path, root: pathlib.Path, options: dict[str, str]) -> None:
    command = [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Root", str(root)]
    for name, value in options.items():
        command += [f"-{name}", value]
    log.info(f"Running {script.name}")
    # The export is read-only and prints counts only; no output is parsed, so a failure is the exit code.
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise SystemExit(f"{script.name} failed with exit code {result.returncode}")


@dataclass(frozen=True)
class Identity:
    key: str
    value: str  # e-mail style: sAMAccountName@dns-domain (best effort; not necessarily the UPN)
    name: str


# Attributes of the domain root that make up the default password policy (the rest belongs to Domain.json).
_POLICY_ATTRIBUTES = {"minpwdlength", "minpwdage", "maxpwdage", "lockoutthreshold", "lockoutduration", "lockoutobservationwindow", "pwdproperties", "pwdhistorylength"}
# Files that collect several directory objects: matched by the object class of the event.
_CLASS_FILES = {"Trusts/Trusts.json": "trusteddomain", "Sites/Sites.json": "site", "Sites/Subnets.json": "subnet", "Sites/SiteLinks.json": "sitelink"}
_GPO_DN = re.compile(r"\{([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})\},cn=policies,cn=system,")
_COMMIT_SUBJECT = re.compile(r"^(\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}) -- .* \(AD\)$")


def _guid(value: str) -> str:
    return (value or "").strip().strip("{}").lower()


def event_keys(event: dict, domain_dn: str) -> set[str]:
    """Match keys of a directory-change event; they meet the keys of the exported files (see file_keys)."""
    keys: set[str] = set()
    attribute = str(event.get("Attribute") or "").lower()
    for field in ("ObjectDn", "OldObjectDn", "NewObjectDn"):
        dn = str(event.get(field) or "").strip().lower()
        if not dn:
            continue
        gpo = _GPO_DN.search(dn)
        if gpo:
            keys.add(f"gpo:{gpo.group(1)}")
        elif dn == domain_dn.lower() and attribute in _POLICY_ATTRIBUTES:
            keys.add("pwdpolicy")
        else:
            keys.add(f"dn:{dn}")
    if _guid(str(event.get("ObjectGuid") or "")):
        keys.add(f"guid:{_guid(str(event.get('ObjectGuid')))}")
    object_class = str(event.get("ObjectClass") or "").lower()
    if object_class in _CLASS_FILES.values():
        keys.add(f"class:{object_class}")
    return keys


def file_keys(path: str, text: str) -> set[str]:
    """Match keys of one exported file."""
    rel = path.replace("\\", "/")
    keys: set[str] = set()
    if "/Group Policy Objects/" in rel:
        match = re.search(r"__([0-9a-fA-F-]{36})\.xml$", rel)
        if match:
            keys.add(f"gpo:{match.group(1).lower()}")
        return keys
    for suffix, object_class in _CLASS_FILES.items():
        if rel.endswith("/" + suffix):
            return {f"class:{object_class}"}
    if rel.endswith("/Domain/DefaultPasswordPolicy.json"):
        return {"pwdpolicy"}
    try:
        data = json.loads(text)
    except ValueError:
        return keys
    if isinstance(data, dict):
        if data.get("DistinguishedName"):
            keys.add(f"dn:{str(data['DistinguishedName']).lower()}")
        if _guid(str(data.get("ObjectGuid") or "")):
            keys.add(f"guid:{_guid(str(data['ObjectGuid']))}")
    return keys


def identity_from_event(event: dict, dns_root: str) -> Identity | None:
    """Who made the change; None for machine accounts and SYSTEM (replication and housekeeping, not an administrator)."""
    name = str(event.get("SubjectName") or "").strip()
    if not name or name.endswith("$") or str(event.get("SubjectSid") or "") == "S-1-5-18":
        return None
    domain = str(event.get("SubjectDomain") or "").strip().lower()
    return Identity(key=f"user:{domain}\\{name.lower()}", value=f"{name}@{dns_root or 'local.invalid'}", name=name)


def attribute_files(
    changed: list[str],
    read_text: Callable[[str], str],
    events: list[dict],
    domain_dn: str,
    dns_root: str,
    fallback: Identity,
) -> dict[str, list[Identity]]:
    """Identities per changed file: those whose events touched it, else the fallback (service) identity."""
    by_key: dict[str, dict[str, Identity]] = defaultdict(dict)
    for event in events:
        identity = identity_from_event(event, dns_root)
        if identity is None:
            continue
        for key in event_keys(event, domain_dn):
            by_key[key][identity.key] = identity
    result: dict[str, list[Identity]] = {}
    for path in changed:
        found: dict[str, Identity] = {}
        for key in file_keys(path, read_text(path)):
            found.update(by_key.get(key, {}))
        result[path] = sorted(found.values(), key=lambda item: item.key) or [fallback]
    return result


def last_ad_backup_time(repo_root: pathlib.Path, depth: int = 300) -> dt.datetime | None:
    """Start of the last merged AD backup, from the commit subject '<YYYY.MM.DD_HH.MM> -- <author> (AD)'."""
    proc = common.git_proc(repo_root, ["log", f"-{depth}", "--format=%s"], check=False)
    for subject in proc.stdout.splitlines():
        match = _COMMIT_SUBJECT.match(subject.strip())
        if match:
            return dt.datetime.strptime(match.group(1), "%Y.%m.%d_%H.%M").replace(tzinfo=dt.timezone.utc)
    return None


def collect_events(powershell: str, script: pathlib.Path, servers: list[str], since: dt.datetime, until: dt.datetime) -> list[dict]:
    """Run export_ad_changes.ps1; returns its events. Unreadable controllers are logged, not fatal."""
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "events.json"
        command = [
            powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-OutFile", str(out), "-Servers", ",".join(servers),
            "-Since", since.strftime("%Y-%m-%dT%H:%M:%SZ"), "-Until", until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ]
        result = subprocess.run(command, check=False)
        if result.returncode != 0 or not out.is_file():
            raise RuntimeError(f"{script.name} failed with exit code {result.returncode}")
        data = json.loads(out.read_text(encoding="utf-8-sig"))
    for failure in data.get("ServersFailed") or []:
        common.ci_log("warning", f"Cannot read directory-change events from {failure.get('Server')}: {failure.get('Error')}")
    return [event for event in data.get("Events") or [] if isinstance(event, dict)]


def make_attributor(
    repo_root: pathlib.Path, root: pathlib.Path, powershell: str, started: dt.datetime, lookback_hours: int, servers_override: str
) -> Callable[[list[str], Identity], dict[str, list[Identity]]]:
    """Attribution step for commit_drift; any failure degrades to the service identity."""

    def attribute(changed: list[str], fallback: Identity) -> dict[str, list[Identity]]:
        try:
            domain = json.loads((root / "Domain" / "Domain.json").read_text(encoding="utf-8"))
            servers = [s.strip() for s in servers_override.split(",") if s.strip()] or list(domain.get("DomainControllers") or [])
            since = last_ad_backup_time(repo_root)
            if since is None:
                since = started - dt.timedelta(hours=lookback_hours)
                log.info(f"No earlier AD backup commit found; reading change events of the last {lookback_hours} hours")
            events = collect_events(powershell, pathlib.Path(__file__).with_name("export_ad_changes.ps1"), servers, since, dt.datetime.now(dt.timezone.utc))
            log.info(f"Read {len(events)} directory-change events since {since:%Y-%m-%d %H:%M} UTC from {len(servers)} domain controller(s)")
            if not events:
                common.ci_log("warning", "No directory-change events found. Check 'Audit Directory Service Changes' and the audit entries (SACL); see docs/active-directory-setup.md.")

            def read_text(path: str) -> str:
                file = repo_root / path
                if file.is_file():
                    return file.read_text(encoding="utf-8")
                shown = common.git_proc(repo_root, ["show", f"HEAD:{path}"], check=False)  # deleted object: use its last snapshot
                return shown.stdout if shown.returncode == 0 else ""

            result = attribute_files(changed, read_text, events, str(domain.get("DistinguishedName") or ""), str(domain.get("DnsRoot") or ""), fallback)
            unresolved = sum(1 for identities in result.values() if identities == [fallback])
            if unresolved:
                common.ci_log("warning", f"No change event found for {unresolved} of {len(changed)} changed files; they are committed as {fallback.name}.")
            return result
        except Exception as exc:  # attribution is best effort
            common.ci_log("warning", f"Unable to attribute Active Directory changes ({exc}). Using the service identity.")
            return {}

    return attribute


def _report(changed: bool, sync_required: bool) -> None:
    common.ci_set_output("CHANGE_DETECTED", "1" if changed else "0")
    common.ci_set_output("ROLLING_PR_SYNC_REQUIRED", "1" if sync_required else "0")


def commit_drift(
    repo_root: pathlib.Path,
    workload_root: str,
    drift_branch: str,
    service_name: str,
    service_email: str,
    started: dt.datetime,
    attribute: Callable[[list[str], Identity], dict[str, list[Identity]]] | None = None,
) -> bool:
    """Commit and push the export, one commit per set of authors; False when there is nothing new."""
    fallback = Identity(key=f"service:{service_email}", value=service_email, name=service_name)
    common.git_proc(repo_root, ["config", "user.name", service_name])
    common.git_proc(repo_root, ["config", "user.email", service_email])
    common.git_proc(repo_root, ["add", "--all", "--", workload_root])
    changed = [line for line in common.git_proc(repo_root, ["diff", "--cached", "--name-only", "--", workload_root]).stdout.splitlines() if line.strip()]
    if not changed:
        log.info("No Active Directory change detected")
        _report(changed=False, sync_required=False)
        return False
    remote_ref = f"refs/remotes/origin/{drift_branch}"
    exists = common.git_proc(repo_root, ["show-ref", "--verify", "--quiet", remote_ref], check=False).returncode == 0
    if exists and common.git_proc(repo_root, ["diff", "--quiet", f"origin/{drift_branch}", "--", workload_root], check=False).returncode == 0:
        # Same snapshot as the existing drift branch: nothing to push, but the PR may still need refreshing.
        log.info("No Active Directory change detected (snapshot identical to existing drift branch)")
        _report(changed=False, sync_required=True)
        return False

    attributed = attribute(changed, fallback) if attribute else {}
    groups: dict[str, tuple[list[str], list[Identity]]] = {}
    for path in changed:
        identities = attributed.get(path) or [fallback]
        files, _ = groups.setdefault("&".join(i.key for i in identities), ([], identities))
        files.append(path)

    stamp = started.astimezone(dt.timezone.utc).strftime("%Y.%m.%d_%H.%M")
    common.git_proc(repo_root, ["reset", "--quiet", "--", workload_root])
    for files, identities in groups.values():
        for path in files:
            common.git_proc(repo_root, ["add", "--all", "--", path])
        names = ", ".join(i.name for i in identities)
        common.git_proc(repo_root, ["config", "user.name", names])
        common.git_proc(repo_root, ["config", "user.email", ", ".join(i.value for i in identities)])
        common.git_proc(repo_root, ["commit", "-m", f"{stamp} -- {names} (AD)"])
    common.git_proc(repo_root, ["push", "--force-with-lease", "origin", f"HEAD:{drift_branch}"])
    _report(changed=True, sync_required=True)
    common.ci_set_output("COMMIT_SHA", common.git_proc(repo_root, ["rev-parse", "HEAD"]).stdout.strip())
    common.ci_set_output("MODIFICATION_AUTHOR", ", ".join(sorted({i.value for _, identities in groups.values() for i in identities})))
    return True


def _flag(name: str, default: str = "true") -> str:
    return common.env_text(name, default) or default


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Every flag defaults from the environment, so the same command line works in PowerShell, cmd and bash steps.
    parser.add_argument("--repo-root", default=".")
    parser.add_argument(
        "--workload-root",
        default=f"{common.env_text('BACKUP_FOLDER', 'tenant-state')}/{common.env_text('AD_BACKUP_SUBDIR', 'ad')}",
        help="Repo-relative export folder",
    )
    parser.add_argument("--baseline-branch", default=common.env_text("BASELINE_BRANCH", "main"))
    parser.add_argument("--drift-branch", default=common.env_text("DRIFT_BRANCH_AD", "drift/ad"))
    parser.add_argument("--service-name", default=common.env_text("USER_NAME", "ASTRAL Backup Service"))
    parser.add_argument("--service-email", default=common.env_text("USER_EMAIL", "astral-backup@local.invalid"))
    parser.add_argument("--powershell", default=common.env_text("AD_POWERSHELL"), help="PowerShell executable (default: powershell, then pwsh)")
    parser.add_argument("--server", default=common.env_text("AD_SERVER"), help="Domain controller to query (default: the runner's domain)")
    args = parser.parse_args()

    repo_root = pathlib.Path(args.repo_root).resolve()
    workload_root = args.workload_root.strip().strip("/")
    root = repo_root / workload_root
    started = dt.datetime.now(dt.timezone.utc)

    common.git_proc(repo_root, ["checkout", "--quiet", "--force", "-B", args.drift_branch, f"origin/{args.baseline_branch}"])
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)

    options = {
        "Server": args.server,
        "IncludeGpos": _flag("AD_INCLUDE_GPOS"),
        "IncludeOus": _flag("AD_INCLUDE_OUS"),
        "IncludePrivilegedGroups": _flag("AD_INCLUDE_PRIVILEGED_GROUPS"),
        "IncludePasswordPolicies": _flag("AD_INCLUDE_PASSWORD_POLICIES"),
        "IncludeTrusts": _flag("AD_INCLUDE_TRUSTS"),
        "IncludeSites": _flag("AD_INCLUDE_SITES"),
        "ExtraPrivilegedGroupsCsv": _flag("AD_EXTRA_PRIVILEGED_GROUPS_CSV", ""),
    }
    powershell = find_powershell(args.powershell)
    run_export(powershell, pathlib.Path(__file__).with_name("export_ad_baseline.ps1"), root, options)

    redactions = normalize_tree(root)
    if redactions:
        common.ci_log("warning", f"Redacted {redactions} GPP cpassword value(s) from the export. Remove the passwords from those GPOs.")
    errors = validate_export(root, common.to_bool(options["IncludeGpos"]))
    if errors:
        for error in errors:
            log.error(error)
        return 1

    attribute = None
    if common.to_bool(_flag("AD_ATTRIBUTE_AUTHORS")):
        attribute = make_attributor(
            repo_root, root, powershell, started, common.env_int("AD_AUDIT_LOOKBACK_HOURS", 48), common.env_text("AD_AUDIT_SERVERS")
        )
    commit_drift(repo_root, workload_root, args.drift_branch, args.service_name, args.service_email, started, attribute)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log.error(str(exc))
        sys.exit(1)
