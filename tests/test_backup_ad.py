from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_module():
    spec = importlib.util.spec_from_file_location("backup_ad", SCRIPTS / "backup_ad.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["backup_ad"] = module
    spec.loader.exec_module(module)
    return module


GPO_XML = (
    '<?xml version="1.0" encoding="utf-16"?>\r\n<GPO>\r\n  <ReadTime>2026-01-01T00:00:00</ReadTime>\r\n'
    '  <Name>Default</Name>\r\n  <Prefs cpassword="Zm9vYmFy" userName="svc" />\r\n  <cpassword>c2VjcmV0</cpassword>\r\n</GPO>\r\n'
)


class ScrubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def test_secrets_timestamps_and_encoding_are_scrubbed(self) -> None:
        text, count = self.m.scrub_text(GPO_XML)
        self.assertEqual(count, 2)
        self.assertNotIn("Zm9vYmFy", text)
        self.assertNotIn("c2VjcmV0", text)
        self.assertNotIn("ReadTime", text)
        self.assertNotIn("\r", text)
        self.assertIn('encoding="utf-8"', text)
        self.assertIn('cpassword="[REDACTED]"', text)
        self.assertIn("<cpassword>[REDACTED]</cpassword>", text)

    def test_normalize_tree_is_idempotent_and_reads_utf16_and_bom(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "g.xml").write_bytes(GPO_XML.encode("utf-16"))
            (root / "d.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"b": 1, "a": "é"}, indent=2).encode("utf-8"))
            self.assertEqual(self.m.normalize_tree(root), 2)
            first = {p.name: p.read_bytes() for p in root.iterdir()}
            self.assertEqual(first["d.json"], '{\n    "a": "é",\n    "b": 1\n}\n'.encode("utf-8"))
            self.assertEqual(self.m.normalize_tree(root), 2)  # redacted values are still redacted: stable count
            self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, first)

    def test_validate_export_requires_domain_json_and_gpos(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(len(self.m.validate_export(root, True)), 2)
            (root / "Domain").mkdir()
            (root / "Domain" / "Domain.json").write_text("{}")
            self.assertEqual(len(self.m.validate_export(root, True)), 1)
            self.assertEqual(self.m.validate_export(root, False), [])
            (root / "Group Policy Objects").mkdir()
            (root / "Group Policy Objects" / "x__1.xml").write_text("<GPO/>")
            self.assertEqual(self.m.validate_export(root, True), [])


class _GitRepoCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def _git(self, cwd, *args):
        return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def _repo(self, td):
        origin, work = str(Path(td, "origin.git")), str(Path(td, "work"))
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
        subprocess.run(["git", "clone", "-q", origin, work], check=True, capture_output=True)
        Path(work, "README.md").write_text("x")
        self._git(work, "add", ".")
        self._git(work, "commit", "-q", "-m", "init")
        self._git(work, "push", "-q", "origin", "HEAD:main")
        self._git(work, "checkout", "-q", "-B", "drift/ad", "origin/main")
        return origin, work

    def _commit(self, work):
        outputs = []
        with patch.object(self.m.common, "ci_set_output", lambda name, value: outputs.append((name, str(value)))):
            changed = self.m.commit_drift(Path(work), "tenant-state/ad", "drift/ad", "Svc", "svc@x", dt.datetime(2026, 5, 1, 2, 30, tzinfo=dt.timezone.utc))
        return changed, dict(outputs)


class CommitDriftTests(_GitRepoCase):

    def test_new_snapshot_is_committed_and_pushed_then_identical_one_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            origin, work = self._repo(td)
            Path(work, "tenant-state/ad/Domain").mkdir(parents=True)
            Path(work, "tenant-state/ad/Domain/Domain.json").write_text("{}\n")
            changed, out = self._commit(work)
            self.assertTrue(changed)
            self.assertEqual((out["CHANGE_DETECTED"], out["ROLLING_PR_SYNC_REQUIRED"]), ("1", "1"))
            self.assertEqual(self._git(work, "log", "-1", "--format=%s"), "2026.05.01_02.30 -- Svc (AD)")
            self.assertEqual(self._git(origin, "rev-parse", "drift/ad"), self._git(work, "rev-parse", "HEAD"))

            # A later run on a fresh branch from main produces the same snapshot: nothing to push, PR refresh only.
            self._git(work, "fetch", "-q", "origin")
            self._git(work, "checkout", "-q", "-f", "-B", "drift/ad", "origin/main")
            Path(work, "tenant-state/ad/Domain").mkdir(parents=True)
            Path(work, "tenant-state/ad/Domain/Domain.json").write_text("{}\n")
            changed, out = self._commit(work)
            self.assertFalse(changed)
            self.assertEqual((out["CHANGE_DETECTED"], out["ROLLING_PR_SYNC_REQUIRED"]), ("0", "1"))

    def test_unchanged_export_means_no_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, work = self._repo(td)
            Path(work, "tenant-state/ad").mkdir(parents=True)
            Path(work, "tenant-state/ad/Domain.json").write_text("{}\n")
            self._git(work, "add", ".")
            self._git(work, "commit", "-q", "-m", "baseline")
            changed, out = self._commit(work)
            self.assertFalse(changed)
            self.assertEqual((out["CHANGE_DETECTED"], out["ROLLING_PR_SYNC_REQUIRED"]), ("0", "0"))


class RiskClassificationTests(unittest.TestCase):
    def test_security_relevant_ad_areas_rank_above_topology(self) -> None:
        import pr_changes

        def score(path: str) -> int:
            return pr_changes._classify_risk(f"tenant-state/ad/{path}", "edit", "tenant-state", "reports")[0]

        for path in ("Privileged Groups/Domain Admins__S-1-5-21-1-512.json", "Group Policy Objects/Default__1.xml",
                     "Fine-Grained Password Policies/Tier0__1.json", "Trusts/Trusts.json", "Domain/DefaultPasswordPolicy.json"):
            self.assertEqual(score(path), 3, path)
        for path in ("Organizational Units/HQ__1.json", "Sites/Subnets.json"):
            self.assertEqual(score(path), 2, path)


DOMAIN_DN = "DC=corp,DC=local"
GPO_GUID = "31b2f340-016d-11d2-945f-00c04fb984f9"


def _event(subject="alice", **fields):
    base = {"TimeUtc": "2026-05-01T01:00:00Z", "EventId": 5136, "Server": "dc1", "SubjectName": subject,
            "SubjectDomain": "CORP", "SubjectSid": "S-1-5-21-1-1100", "ObjectDn": "", "ObjectGuid": "", "ObjectClass": "", "Attribute": ""}
    return {**base, **fields}


class AttributionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()
        cls.fallback = cls.m.Identity(key="service:bot@x", value="bot@x", name="Bot")

    def _attribute(self, files: dict[str, str], events: list[dict]):
        return self.m.attribute_files(list(files), files.get, events, DOMAIN_DN, "corp.local", self.fallback)

    def _names(self, result):
        return {path: [i.name for i in identities] for path, identities in result.items()}

    def test_events_reach_the_files_they_changed(self) -> None:
        ou = "tenant-state/ad/Organizational Units/HQ__g1.json"
        gpo = f"tenant-state/ad/Group Policy Objects/Baseline__{GPO_GUID}.xml"
        group = "tenant-state/ad/Privileged Groups/Domain Admins__S-1-5-21-1-512.json"
        files = {
            ou: json.dumps({"DistinguishedName": "OU=HQ,DC=corp,DC=local", "ObjectGuid": "11111111-1111-1111-1111-111111111111"}),
            gpo: "<GPO/>",
            group: json.dumps({"DistinguishedName": "CN=Domain Admins,CN=Users,DC=corp,DC=local", "ObjectGuid": "22222222-2222-2222-2222-222222222222"}),
        }
        events = [
            _event("alice", ObjectGuid="{11111111-1111-1111-1111-111111111111}", ObjectDn="OU=HQ,DC=corp,DC=local", Attribute="gPLink"),
            # A GPO edit is logged on the policy container; the GPO id is in the DN, not in ObjectGUID.
            _event("bob", ObjectGuid="{99999999-9999-9999-9999-999999999999}", ObjectDn=f"CN={{{GPO_GUID.upper()}}},CN=Policies,CN=System,DC=corp,DC=local", Attribute="versionNumber"),
            _event("carol", ObjectDn="CN=Domain Admins,CN=Users,DC=corp,DC=local", Attribute="member"),
            _event("carol", ObjectDn="CN=Domain Admins,CN=Users,DC=corp,DC=local", Attribute="member"),  # duplicate event: one author
        ]
        self.assertEqual(self._names(self._attribute(files, events)), {ou: ["alice"], gpo: ["bob"], group: ["carol"]})

    def test_several_authors_unmatched_files_and_ignored_subjects(self) -> None:
        ou = "tenant-state/ad/Organizational Units/HQ__g1.json"
        other = "tenant-state/ad/Organizational Units/Lab__g2.json"
        files = {ou: json.dumps({"DistinguishedName": "OU=HQ,DC=corp,DC=local"}), other: json.dumps({"DistinguishedName": "OU=Lab,DC=corp,DC=local"})}
        events = [
            _event("bob", ObjectDn="OU=HQ,DC=corp,DC=local"),
            _event("alice", ObjectDn="OU=HQ,DC=corp,DC=local"),
            _event("DC01$", ObjectDn="OU=Lab,DC=corp,DC=local"),                                   # machine account
            _event("SYSTEM", SubjectSid="S-1-5-18", ObjectDn="OU=Lab,DC=corp,DC=local"),           # housekeeping
        ]
        names = self._names(self._attribute(files, events))
        self.assertEqual(names[ou], ["alice", "bob"])  # sorted, deterministic
        self.assertEqual(names[other], ["Bot"])        # nobody to name: service identity

    def test_password_policy_events_only_reach_the_policy_file(self) -> None:
        policy = "tenant-state/ad/Domain/DefaultPasswordPolicy.json"
        domain = "tenant-state/ad/Domain/Domain.json"
        files = {policy: "{}", domain: json.dumps({"DistinguishedName": DOMAIN_DN})}
        names = self._names(self._attribute(files, [_event("alice", ObjectDn=DOMAIN_DN, Attribute="minPwdLength"), _event("bob", ObjectDn=DOMAIN_DN, Attribute="gPLink")]))
        self.assertEqual(names, {policy: ["alice"], domain: ["bob"]})

    def test_aggregate_files_match_on_object_class_and_moves_on_the_old_name(self) -> None:
        trusts, sites = "tenant-state/ad/Trusts/Trusts.json", "tenant-state/ad/Sites/Sites.json"
        gone = "tenant-state/ad/Organizational Units/Old__g3.json"
        files = {trusts: "[]", sites: "[]", gone: json.dumps({"DistinguishedName": "OU=Old,DC=corp,DC=local"})}
        events = [
            _event("alice", ObjectClass="trustedDomain", ObjectDn="CN=partner.example,CN=System,DC=corp,DC=local"),
            _event("bob", ObjectClass="site", ObjectDn="CN=HQ,CN=Sites,CN=Configuration,DC=corp,DC=local"),
            _event("carol", EventId=5139, OldObjectDn="OU=Old,DC=corp,DC=local", NewObjectDn="OU=Archive,DC=corp,DC=local"),
        ]
        self.assertEqual(self._names(self._attribute(files, events)), {trusts: ["alice"], sites: ["bob"], gone: ["carol"]})

    def test_last_backup_time_comes_from_the_newest_ad_commit_subject(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["git", "init", "-q", td], check=True)
            self.assertIsNone(self.m.last_ad_backup_time(Path(td)))  # no commits at all
            for subject in ("2026.04.01_02.30 -- Old (AD)", "2026.05.02_02.31 -- alice, bob (AD)", "unrelated", "2026.05.03_02.30 -- x (Entra)"):
                subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", subject], cwd=td, check=True)
            self.assertEqual(self.m.last_ad_backup_time(Path(td)), dt.datetime(2026, 5, 2, 2, 31, tzinfo=dt.timezone.utc))

    @unittest.skipUnless(shutil.which("sh"), "needs sh")
    def test_collect_events_passes_the_window_and_reports_unreadable_controllers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td, "powershell")
            args_log = Path(td, "args")
            # Stands in for powershell.exe: records its arguments and writes the JSON the real script would write.
            fake.write_text(
                '#!/bin/sh\necho "$*" > "%s"\nwhile [ $# -gt 0 ]; do [ "$1" = -OutFile ] && out="$2"; shift; done\n'
                'echo \'{"ServersRead":["dc1"],"ServersFailed":[{"Server":"dc2","Error":"RPC unavailable"}],"Events":[{"SubjectName":"alice"}]}\' > "$out"\n' % args_log
            )
            fake.chmod(0o755)
            warnings: list[str] = []
            since, until = dt.datetime(2026, 5, 1, 2, 0, tzinfo=dt.timezone.utc), dt.datetime(2026, 5, 2, 2, 0, tzinfo=dt.timezone.utc)
            with patch.object(self.m.common, "ci_log", lambda level, message: warnings.append(f"{level}: {message}")):
                events = self.m.collect_events(str(fake), Path("export_ad_changes.ps1"), ["dc1", "dc2"], since, until)
            self.assertEqual(events, [{"SubjectName": "alice"}])
            self.assertIn("-Servers dc1,dc2 -Since 2026-05-01T02:00:00Z -Until 2026-05-02T02:00:00Z", args_log.read_text())
            self.assertEqual(warnings, ["warning: Cannot read directory-change events from dc2: RPC unavailable"])
            fake.write_text("#!/bin/sh\nexit 1\n")
            with self.assertRaises(RuntimeError):
                self.m.collect_events(str(fake), Path("x.ps1"), ["dc1"], since, until)


class AttributedCommitTests(_GitRepoCase):
    """commit_drift with an attribution step, against real local repositories."""

    def test_each_set_of_authors_gets_its_own_commit_and_a_deleted_object_keeps_its_author(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            origin, work = self._repo(td)
            ou_dir = Path(work, "tenant-state/ad/Organizational Units")
            ou_dir.mkdir(parents=True)
            (ou_dir / "Old__g1.json").write_text(json.dumps({"DistinguishedName": "OU=Old,DC=corp,DC=local"}))
            self._git(work, "add", ".")
            self._git(work, "commit", "-q", "-m", "baseline")
            self._git(work, "push", "-q", "origin", "HEAD:main")
            self._git(work, "fetch", "-q", "origin")
            self._git(work, "checkout", "-q", "-B", "drift/ad", "origin/main")
            (ou_dir / "Old__g1.json").unlink()                                        # OU deleted by carol
            Path(work, "tenant-state/ad/Domain").mkdir()
            Path(work, "tenant-state/ad/Domain/Domain.json").write_text('{"x": 1}\n')  # changed by alice and bob
            Path(work, "tenant-state/ad/Sites.json").write_text("[]\n")                # nobody found

            def attribute(changed, fallback):
                identity = lambda n: self.m.Identity(key=f"user:{n}", value=f"{n}@corp.local", name=n)
                texts = {p: self.m.file_keys(p, "") for p in changed}  # file_keys must cope with deleted/empty content
                self.assertIn("tenant-state/ad/Organizational Units/Old__g1.json", texts)
                return {
                    "tenant-state/ad/Organizational Units/Old__g1.json": [identity("carol")],
                    "tenant-state/ad/Domain/Domain.json": [identity("alice"), identity("bob")],
                }

            outputs = []
            with patch.object(self.m.common, "ci_set_output", lambda name, value: outputs.append((name, str(value)))):
                changed = self.m.commit_drift(Path(work), "tenant-state/ad", "drift/ad", "Svc", "svc@x", dt.datetime(2026, 5, 1, 2, 30, tzinfo=dt.timezone.utc), attribute)
            self.assertTrue(changed)
            log = self._git(work, "log", "--format=%an|%ae|%s", "origin/main..HEAD").splitlines()
            self.assertEqual(sorted(log), sorted([
                "carol|carol@corp.local|2026.05.01_02.30 -- carol (AD)",
                "alice, bob|alice@corp.local, bob@corp.local|2026.05.01_02.30 -- alice, bob (AD)",
                "Svc|svc@x|2026.05.01_02.30 -- Svc (AD)",
            ]))
            self.assertEqual(self._git(origin, "rev-parse", "drift/ad"), self._git(work, "rev-parse", "HEAD"))
            self.assertEqual(dict(outputs)["MODIFICATION_AUTHOR"], "alice@corp.local, bob@corp.local, carol@corp.local, svc@x")

    @unittest.skipUnless(shutil.which("sh"), "needs sh")
    def test_attributor_reads_deleted_objects_from_head_and_degrades_when_the_event_read_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, work = self._repo(td)
            root = Path(work, "tenant-state/ad")
            (root / "Domain").mkdir(parents=True)
            (root / "Organizational Units").mkdir()
            (root / "Domain/Domain.json").write_text(json.dumps({"DistinguishedName": DOMAIN_DN, "DnsRoot": "corp.local", "DomainControllers": ["dc1"]}))
            (root / "Organizational Units/Old__g1.json").write_text(json.dumps({"DistinguishedName": "OU=Old,DC=corp,DC=local"}))
            self._git(work, "add", ".")
            self._git(work, "commit", "-q", "-m", "2026.04.30_02.30 -- Svc (AD)")
            (root / "Organizational Units/Old__g1.json").unlink()
            fake = Path(td, "powershell")
            fake.write_text(
                '#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -OutFile ] && out="$2"; shift; done\n'
                'echo \'{"ServersRead":["dc1"],"Events":[{"SubjectName":"carol","SubjectDomain":"CORP","ObjectDn":"OU=Old,DC=corp,DC=local"}]}\' > "$out"\n'
            )
            fake.chmod(0o755)
            fallback = self.m.Identity(key="service:svc@x", value="svc@x", name="Svc")
            attribute = self.m.make_attributor(Path(work), root, str(fake), dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc), 48, "")
            path = "tenant-state/ad/Organizational Units/Old__g1.json"
            result = attribute([path], fallback)
            self.assertEqual([i.value for i in result[path]], ["carol@corp.local"])

            fake.write_text("#!/bin/sh\nexit 1\n")  # event read fails: no exception, caller falls back to the service identity
            with patch.object(self.m.common, "ci_log", lambda *a: None):
                self.assertEqual(attribute([path], fallback), {})

    def test_failing_attribution_still_commits_under_the_service_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, work = self._repo(td)
            Path(work, "tenant-state/ad").mkdir(parents=True)
            Path(work, "tenant-state/ad/a.json").write_text("{}\n")
            with patch.object(self.m.common, "ci_set_output", lambda *a: None):
                self.m.commit_drift(Path(work), "tenant-state/ad", "drift/ad", "Svc", "svc@x", dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc), lambda changed, fallback: {})
            self.assertEqual(self._git(work, "log", "-1", "--format=%an|%s"), "Svc|2026.05.01_00.00 -- Svc (AD)")


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class ReviewIsBackupOnlyTests(unittest.TestCase):
    def test_ad_review_never_enables_remediation_even_when_the_tenant_asks_for_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = Path(td, "calls.log")
            fake = Path(td, "bin", "python3")
            fake.parent.mkdir()
            # Stands in for every python3 call: records the script, the workload and the remediation switches.
            fake.write_text(f'#!/bin/sh\necho "$(basename "$1") $* remediate_rejection=$AUTO_REMEDIATE_ON_PR_REJECTION" >> "{log}"\n')
            fake.chmod(0o755)
            env = {
                **os.environ, "PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}", "WORKLOAD": "ad", "ENSURE_PR": "true",
                "FORGE_TOKEN": "t", "GITHUB_SERVER_URL": "https://forge.invalid", "ASTRAL_SCM": "github",
                "AUTO_REMEDIATE_ON_PR_REJECTION": "true", "AUTO_REMEDIATE_AFTER_MERGE": "true",
            }
            subprocess.run(["bash", str(SCRIPTS / "run_review.sh")], cwd=td, env=env, check=True, capture_output=True)
            calls = log.read_text().splitlines()
        by_script = {line.split()[0]: line for line in calls}
        self.assertIn("--workload ad", by_script["ensure_rolling_pr.py"])
        self.assertIn("--drift-branch drift/ad", by_script["ensure_rolling_pr.py"])
        self.assertTrue(by_script["ensure_rolling_pr.py"].endswith("remediate_rejection=false"))
        self.assertNotIn("queue_post_merge_restore.py", by_script)


@unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
class ExporterScriptTests(unittest.TestCase):
    def test_scripts_parse_and_are_ascii(self) -> None:
        cmd = "$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($env:AD_SCRIPT,[ref]$null,[ref]$e);$e|ForEach-Object{$_.Message};exit [int]($e.Count -gt 0)"
        for name in ("export_ad_baseline.ps1", "export_ad_changes.ps1"):
            with self.subTest(script=name):
                path = SCRIPTS / name
                self.assertTrue(path.read_bytes().isascii(), "Windows PowerShell 5.1 misreads non-ASCII in a BOM-less file")
                result = subprocess.run(["pwsh", "-NoProfile", "-Command", cmd], capture_output=True, text=True, env={**os.environ, "AD_SCRIPT": str(path)})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
