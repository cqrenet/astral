from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "astral_mcp_tools.py"


def load_module():
    spec = importlib.util.spec_from_file_location("astral_mcp_tools", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AstralMcpToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def test_list_workloads(self) -> None:
        client = self.module.AstralMcpClient(local_root="/dev/null")
        self.assertEqual(client.list_workloads(), ["intune", "entra", "ad"])

    def test_list_categories_intune(self) -> None:
        client = self.module.AstralMcpClient(local_root="/dev/null")
        cats = client.list_categories("intune")
        self.assertIn("Compliance Policies", cats)
        self.assertIn("Conditional Access", client.list_categories("entra"))

    def test_rejects_traversal_and_unknown_inputs(self) -> None:
        client = self.module.AstralMcpClient(local_root="/nonexistent")
        with self.assertRaises(ValueError):
            client.list_policies("..", "..")
        with self.assertRaises(ValueError):
            client.list_policies("intune", "../../etc")
        with self.assertRaises(ValueError):
            client.get_policy("intune", "Compliance Policies", "../secret")
        with self.assertRaises(ValueError):
            client.get_assignment_report("../../")

    def test_local_list_policies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            policies_dir = root / "tenant-state" / "intune" / "Compliance Policies"
            policies_dir.mkdir(parents=True)
            (policies_dir / "PolicyA__id1.json").write_text('{"id": "1"}\n', encoding="utf-8")
            (policies_dir / "PolicyB__id2.json").write_text('{"id": "2"}\n', encoding="utf-8")
            (policies_dir / "readme.txt").write_text("ignore me\n", encoding="utf-8")

            client = self.module.AstralMcpClient(local_root=str(root))
            policies = client.list_policies("intune", "Compliance Policies")
            names = [p["name"] for p in policies]
            self.assertEqual(sorted(names), ["PolicyA__id1", "PolicyB__id2"])

    def test_local_get_policy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            policy_path = root / "tenant-state" / "entra" / "Conditional Access"
            policy_path.mkdir(parents=True)
            payload = {"displayName": "CA-001", "state": "enabled"}
            (policy_path / "CA-001__guid.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")

            client = self.module.AstralMcpClient(local_root=str(root))
            result = client.get_policy("entra", "Conditional Access", "CA-001__guid")
            self.assertEqual(result["displayName"], "CA-001")

    def test_local_search_policies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for cat in ["Compliance Policies", "Device Configurations"]:
                d = root / "tenant-state" / "intune" / cat
                d.mkdir(parents=True)
                (d / "Win_Policy__id.json").write_text("{}", encoding="utf-8")
                (d / "Mac_Policy__id.json").write_text("{}", encoding="utf-8")

            client = self.module.AstralMcpClient(local_root=str(root))
            results = client.search_policies("intune", "Win")
            self.assertEqual(len(results), 2)
            for r in results:
                self.assertIn("Win", r["name"])

    def test_local_file_commits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(["git", "init"], cwd=str(root), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(root), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(root), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            policy_path = root / "tenant-state" / "intune" / "Compliance Policies"
            policy_path.mkdir(parents=True)
            (policy_path / "P__id.json").write_text("{}", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=str(root), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "commit", "-m", "first"], cwd=str(root), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            client = self.module.AstralMcpClient(local_root=str(root))
            commits = client.get_policy_history("intune", "Compliance Policies", "P__id", limit=5)
            self.assertEqual(len(commits), 1)
            self.assertEqual(commits[0]["comment"], "first")

    def test_local_reads_reports_under_generator_names(self) -> None:
        # Regression: the client looked for assignments.md / <cat>_inventory.csv, which the generators never write.
        with tempfile.TemporaryDirectory() as td:
            reports = Path(td) / "tenant-state" / "reports" / "intune"
            (reports / "Object Inventory").mkdir(parents=True)
            (reports / "policy-assignments.md").write_text("# Assignments\n", encoding="utf-8")
            (reports / "Object Inventory" / "compliance-policies-inventory.csv").write_text(
                "ObjectName,ObjectType\nP1,Windows\n", encoding="utf-8"
            )
            client = self.module.AstralMcpClient(local_root=td)
            self.assertIn("Assignments", client.get_assignment_report("intune"))
            self.assertEqual(
                client.get_object_inventory("intune", "Compliance Policies"),
                [{"ObjectName": "P1", "ObjectType": "Windows"}],
            )

    def _write(self, root: Path, rel: str, text: str) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_object_inventory_without_category_reads_the_combined_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write(Path(td), "tenant-state/reports/intune/object-inventory-all.csv", "Name,Type\nA,x\n")
            rows = self.module.AstralMcpClient(local_root=td).get_object_inventory("intune")
        self.assertEqual(rows, [{"Name": "A", "Type": "x"}])

    def test_identity_snapshots_are_sorted_and_name_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root, "tenant-state/entra/Groups/b.json", json.dumps({"displayName": "beta", "memberCount": 2}))
            self._write(root, "tenant-state/entra/Groups/a.json", json.dumps({"displayName": "Alpha"}))
            self._write(root, "tenant-state/entra/Groups/skip.json", "[1, 2]")
            self._write(root, "tenant-state/entra/Groups/bad.json", "{not json")
            self._write(root, "tenant-state/entra/PIM Role Policies/g.json", json.dumps({"roleDisplayName": "Global Administrator"}))
            self._write(root, "tenant-state/entra/PIM Role Policies/u.json", json.dumps({"roleDisplayName": "User Administrator"}))
            self._write(root, "tenant-state/entra/Role Assignments/r.json", json.dumps({"displayName": "Helpdesk"}))
            self._write(root, "tenant-state/entra/Security/Security Defaults.json", json.dumps({"isEnabled": False}))
            client = self.module.AstralMcpClient(local_root=td)
            groups = client.get_ca_groups()
            pim = client.get_pim_policies("global")
            roles = client.get_privileged_role_assignments()
            security = client.get_security_settings()
        self.assertEqual([g["displayName"] for g in groups], ["Alpha", "beta"])
        self.assertEqual([p["roleDisplayName"] for p in pim], ["Global Administrator"])
        self.assertEqual([r["displayName"] for r in roles], ["Helpdesk"])
        self.assertEqual(security, {"securityDefaults": {"isEnabled": False}})

    def _ad_tree(self, root: Path) -> None:
        ad = root / "tenant-state" / "ad"
        (ad / "Group Policy Objects").mkdir(parents=True)
        (ad / "Group Policy Objects" / "Workstation Baseline__aaaa-1.xml").write_text("<GPO>w</GPO>", encoding="utf-8")
        (ad / "Group Policy Objects" / "Dup__bbbb-2.xml").write_text("<GPO/>", encoding="utf-8")
        (ad / "Group Policy Objects" / "Dup__cccc-3.xml").write_text("<GPO/>", encoding="utf-8")
        (ad / "Group Policy Objects" / "Big__dddd-4.xml").write_text("x" * 100_050, encoding="utf-8")
        (ad / "Organizational Units").mkdir()
        (ad / "Organizational Units" / "Workstations__g1.json").write_text(json.dumps({
            "DistinguishedName": "OU=Workstations,DC=corp,DC=local",
            "GpoLinks": [{"Order": 1, "GpoId": "AAAA-1", "Enabled": True, "Enforced": False}, {"Order": 2, "GpoId": "gone", "Enabled": True, "Enforced": False}],
        }), encoding="utf-8")
        (ad / "Privileged Groups").mkdir()
        (ad / "Privileged Groups" / "Domain Admins__S-1-512.json").write_text(json.dumps({"Name": "Domain Admins", "Members": [{"SamAccountName": "alice"}]}), encoding="utf-8")
        (ad / "Privileged Groups" / "Schema Admins__S-1-518.json").write_text(json.dumps({"Name": "Schema Admins", "Members": []}), encoding="utf-8")
        (ad / "Domain").mkdir()
        (ad / "Domain" / "Domain.json").write_text(json.dumps({"DnsRoot": "corp.local"}), encoding="utf-8")
        (ad / "Domain" / "DefaultPasswordPolicy.json").write_text(json.dumps({"MinPasswordLength": 12}), encoding="utf-8")
        (ad / "Sites").mkdir()
        (ad / "Sites" / "Sites.json").write_text(json.dumps([{"Name": "HQ"}]), encoding="utf-8")

    def test_active_directory_gpos_are_listed_and_read_by_exact_name_or_guid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._ad_tree(Path(td))
            client = self.module.AstralMcpClient(local_root=td)
            self.assertEqual([g["name"] for g in client.list_group_policy_objects("work")], ["Workstation Baseline"])
            self.assertEqual(client.get_group_policy_object("workstation baseline"), "<GPO>w</GPO>")
            self.assertEqual(client.get_group_policy_object("AAAA-1"), "<GPO>w</GPO>")
            with self.assertRaises(FileNotFoundError):
                client.get_group_policy_object("Workstation")  # substring is not a match
            with self.assertRaises(ValueError) as ambiguous:
                client.get_group_policy_object("Dup")
            self.assertIn("bbbb-2", str(ambiguous.exception))
            with self.assertRaises(ValueError):
                client.get_group_policy_object("")
            big = client.get_group_policy_object("Big")
            self.assertTrue(big.endswith("<!-- truncated: 50 more characters -->"))

    def test_active_directory_gpo_lookup_cannot_escape_the_export(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._ad_tree(Path(td))
            Path(td, "secret.xml").write_text("secret", encoding="utf-8")
            client = self.module.AstralMcpClient(local_root=td)
            for attempt in ("../../../secret", "..", "/etc/passwd"):
                with self.assertRaises(FileNotFoundError):
                    client.get_group_policy_object(attempt)

    def test_active_directory_ous_get_gpo_names_and_groups_filter_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._ad_tree(Path(td))
            client = self.module.AstralMcpClient(local_root=td)
            links = client.get_ad_organizational_units("workstations")[0]["GpoLinks"]
            self.assertEqual([link["GpoName"] for link in links], ["Workstation Baseline", ""])  # unknown GPO: empty name
            self.assertEqual([g["Name"] for g in client.get_ad_privileged_groups()], ["Domain Admins", "Schema Admins"])
            self.assertEqual([g["Name"] for g in client.get_ad_privileged_groups("schema")], ["Schema Admins"])
            self.assertEqual(client.get_ad_organizational_units("nothing"), [])

    def test_active_directory_overview_omits_what_was_not_exported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._ad_tree(Path(td))
            overview = self.module.AstralMcpClient(local_root=td).get_ad_domain()
            self.assertEqual(overview["domain"], {"DnsRoot": "corp.local"})
            self.assertEqual(overview["defaultPasswordPolicy"], {"MinPasswordLength": 12})
            self.assertEqual(overview["sites"]["Sites"], [{"Name": "HQ"}])
            self.assertNotIn("trusts", overview)
            self.assertEqual(self.module.AstralMcpClient(local_root=td + "/nowhere").get_ad_domain(), {})

    def test_active_directory_generic_tools_work_for_json_categories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._ad_tree(Path(td))
            client = self.module.AstralMcpClient(local_root=td)
            self.assertIn("Privileged Groups", client.list_categories("ad"))
            self.assertEqual(sorted(p["name"] for p in client.search_policies("ad", "domain")), ["Domain", "Domain Admins__S-1-512"])

    def test_settings_report_filters_and_setting_lookup_groups_by_policy(self) -> None:
        csv_text = (
            "Policy,Setting,Setting ID,Value,Category,OS\n"
            "Win Baseline,Firewall,fw_id,on,Settings Catalog,Windows\n"
            "Win Baseline,Edge sign-in,edge_id,off,Settings Catalog,Windows\n"
            "Mac Baseline,Firewall,fw_id,on,Device Configurations,macOS\n"
        )
        with tempfile.TemporaryDirectory() as td:
            self._write(Path(td), "tenant-state/reports/intune/policy-settings.csv", csv_text)
            client = self.module.AstralMcpClient(local_root=td)
            windows = client.get_settings_report(policy="win", os_filter="windows")
            capped = client.get_settings_report(max_rows=1)
            found = client.find_policies_for_setting("firewall")
        self.assertEqual(len(windows), 2)
        self.assertEqual(len(capped), 1)
        self.assertEqual([f["Policy"] for f in found], ["Mac Baseline", "Win Baseline"])
        self.assertEqual(found[0]["Matches"][0]["Category"], "Device Configurations")

    def test_report_definitions_merge_local_over_builtin(self) -> None:
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        builtin = (
            "reports:\n  - {name: assignments, workloads: [intune], outputs: [policy-assignments.md]}\n"
            "  - {name: apps, workloads: [entra], outputs: [apps.csv]}\n"
        )
        local = "reports:\n  - {name: assignments, workloads: [intune], outputs: [custom.md]}\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root, "reports/definitions.yml", builtin)
            self._write(root, "reports/definitions.local.yml", local)
            self._write(root, "tenant-state/reports/intune/custom.md", "# custom")
            client = self.module.AstralMcpClient(local_root=td)
            intune = client.list_report_definitions("intune")
            report = client.get_report("intune", "assignments")
            generated = client.list_generated_reports("intune")
            missing = client.get_report("intune", "nope")
        self.assertEqual([(d["name"], d["source"]) for d in intune], [("assignments", "local")])
        self.assertEqual(report, "# custom")
        self.assertEqual([g["name"] for g in generated], ["custom.md"])
        self.assertEqual(missing, "")

    def test_remote_recent_commits_sends_from_date(self) -> None:
        source = self.module._AdoSource("org", "proj", "tok", None, "main")
        seen: dict[str, str] = {}

        def fake_get(path, params):
            seen.update(params)
            return {"value": []}

        source._get = fake_get
        client = self.module.AstralMcpClient(organization="org", project="proj", token="tok")
        client._source = source
        client.get_recent_drift("intune", hours=6)
        self.assertIn("searchCriteria.fromDate", seen)

    def test_client_from_env_local_root(self) -> None:
        env_key = "ASTRAL_REPO_ROOT"
        original = os.environ.get(env_key)
        try:
            os.environ[env_key] = "/tmp/fake-astral"
            client = self.module.client_from_env()
            self.assertEqual(client.local_root, "/tmp/fake-astral")
        finally:
            if original is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = original


if __name__ == "__main__":
    unittest.main()


class RemoteCloneTests(unittest.TestCase):
    """ASTRAL_REPO_URL mode: any Git forge, kept fresh by sync_remote_clone."""

    def _git(self, cwd, *args):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True)

    def test_clone_then_refresh_sees_new_commit(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            origin, seed, dest = (os.path.join(tmp, n) for n in ("origin.git", "seed", "clone"))
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
            subprocess.run(["git", "clone", "-q", origin, seed], check=True, capture_output=True)
            folder = Path(seed, "tenant-state", "intune", "Compliance Policies")
            folder.mkdir(parents=True)
            (folder / "A__1.json").write_text("{}")
            self._git(seed, "add", ".")
            self._git(seed, "commit", "-qm", "a")
            self._git(seed, "push", "-q", "origin", "HEAD:main")

            env = {"ASTRAL_REPO_URL": f"file://{origin}", "ASTRAL_REPO_CLONE_DIR": dest, "ASTRAL_REPO_REFRESH_SECONDS": "0"}
            with unittest.mock.patch.dict(os.environ, env):
                client = module.client_from_env()
                self.assertEqual([p["name"] for p in client.list_policies("intune", "Compliance Policies")], ["A__1"])

                (folder / "B__2.json").write_text("{}")
                self._git(seed, "add", ".")
                self._git(seed, "commit", "-qm", "b")
                self._git(seed, "push", "-q", "origin", "HEAD:main")
                module.refresh_remote_clone()
                names = sorted(p["name"] for p in client.list_policies("intune", "Compliance Policies"))
                self.assertEqual(names, ["A__1", "B__2"])
                self.assertEqual(len(client.get_policy_history("intune", "Compliance Policies", "B__2")), 1)

    def test_rejects_non_https_url(self) -> None:
        module = load_module()
        with unittest.mock.patch.dict(os.environ, {"ASTRAL_REPO_URL": "ssh://host/repo.git"}):
            with self.assertRaises(RuntimeError):
                module.sync_remote_clone(force=True)

    def test_clone_token_is_passed_by_environment_not_argv(self) -> None:
        module = load_module()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs.get("env") or {}))
            return subprocess.CompletedProcess(cmd, 0)

        with tempfile.TemporaryDirectory() as tmp:
            env = {"ASTRAL_REPO_URL": "https://git.example/repo.git", "ASTRAL_REPO_CLONE_DIR": os.path.join(tmp, "c"),
                   "ASTRAL_REPO_TOKEN": "s3cret-token"}
            with unittest.mock.patch.dict(os.environ, env), unittest.mock.patch.object(module.subprocess, "run", fake_run):
                module.sync_remote_clone(force=True)
        self.assertTrue(calls)
        for cmd, child_env in calls:
            self.assertFalse(any("s3cret" in part or "extraheader" in part for part in cmd), cmd)
            self.assertIn("GIT_CONFIG_VALUE_0", child_env)
