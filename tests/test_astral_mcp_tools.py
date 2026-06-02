from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
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
        self.assertEqual(client.list_workloads(), ["intune", "entra"])

    def test_list_categories_intune(self) -> None:
        client = self.module.AstralMcpClient(local_root="/dev/null")
        cats = client.list_categories("intune")
        self.assertIn("Compliance Policies", cats)
        self.assertIn("Conditional Access", client.list_categories("entra"))

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
