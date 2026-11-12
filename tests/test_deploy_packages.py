"""The deployed artifacts (Function App zip, MCP container image) ship only a few files from scripts/.

These tests keep that list honest: every module-level sibling import of a shipped script must be
shipped too, the shipped scripts must run on their own, and the provisioning scripts must actually
stage them.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

# Files copied into the Function App package (infra/change-probe/scripts/) by the provisioning scripts.
FUNCTION_APP_SCRIPTS = ("common.py", "probe_tenant_changes.py", "trigger_backup_pipeline.py")


def _mcp_image_scripts() -> tuple[str, ...]:
    dockerfile = (ROOT / "infra/mcp-server/Dockerfile").read_text(encoding="utf-8")
    return tuple(re.findall(r"^COPY scripts/(\S+) \./scripts/", dockerfile, re.M))


def _module_level_sibling_imports(path: Path) -> set[str]:
    """Names of scripts/*.py modules imported at module level (imports inside functions are optional paths)."""
    siblings = {p.stem for p in SCRIPTS.glob("*.py")}
    found: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        # also look into top-level try/except blocks, where the scripts guard alternative import paths
        nodes = [node] + (list(ast.walk(node)) if isinstance(node, ast.Try) else [])
        for item in nodes:
            if isinstance(item, ast.Import):
                found |= {a.name.split(".")[0] for a in item.names} & siblings
            elif isinstance(item, ast.ImportFrom) and item.module:
                found |= {item.module.split(".")[-1]} & siblings
                found |= {a.name for a in item.names} & siblings
    return found


def _closure(files: tuple[str, ...]) -> set[str]:
    needed, queue = set(), [Path(f).stem for f in files]
    while queue:
        name = queue.pop()
        if name in needed:
            continue
        needed.add(name)
        queue.extend(_module_level_sibling_imports(SCRIPTS / f"{name}.py"))
    return needed


class DeployPackageTests(unittest.TestCase):
    def test_function_app_package_is_self_contained(self) -> None:
        shipped = {Path(f).stem for f in FUNCTION_APP_SCRIPTS}
        self.assertEqual(_closure(FUNCTION_APP_SCRIPTS) - shipped, set(), "add the missing script(s) to the package")

    def test_mcp_image_is_self_contained(self) -> None:
        shipped_files = _mcp_image_scripts()
        self.assertIn("astral_mcp_tools.py", shipped_files)
        shipped = {Path(f).stem for f in shipped_files}
        self.assertEqual(_closure(shipped_files) - shipped, set())
        for name in shipped_files:
            self.assertTrue((SCRIPTS / name).exists(), name)

    def test_function_app_scripts_run_with_only_the_shipped_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "scripts").mkdir()
            for name in FUNCTION_APP_SCRIPTS:
                shutil.copy(SCRIPTS / name, Path(td) / "scripts" / name)
            for script in ("probe_tenant_changes.py", "trigger_backup_pipeline.py"):
                with self.subTest(script=script):
                    result = subprocess.run(
                        [sys.executable, "-E", f"scripts/{script}", "--help"], cwd=td, capture_output=True, text=True
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_mcp_tools_import_with_only_the_shipped_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "scripts").mkdir()
            for name in _mcp_image_scripts():
                shutil.copy(SCRIPTS / name, Path(td) / "scripts" / name)
            result = subprocess.run(
                [sys.executable, "-E", "-c", "import sys; sys.path.insert(0, 'scripts'); import astral_mcp_tools"],
                cwd=td, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_provisioning_scripts_stage_the_function_scripts(self) -> None:
        # infra/change-probe/scripts/ is generated and gitignored, so a fresh clone has nothing there.
        for rel in ("deploy/provision.ps1",):
            text = (ROOT / rel).read_text(encoding="utf-8-sig")
            with self.subTest(script=rel):
                for name in FUNCTION_APP_SCRIPTS:
                    self.assertIn(f'"{name}"', text, f"{rel} does not stage {name} into the Function App package")

    def test_mcp_staging_matches_the_dockerfile(self) -> None:
        shipped = set(_mcp_image_scripts())
        for rel in ("deploy/provision.ps1",):
            text = (ROOT / rel).read_text(encoding="utf-8-sig")
            staged = set(re.findall(r'\$repoRoot, "scripts", "([^"]+\.py)"', text))
            with self.subTest(script=rel):
                self.assertTrue(shipped <= staged, f"{rel} stages {sorted(staged)} but the image needs {sorted(shipped)}")

    def test_intunecd_version_is_the_same_everywhere(self) -> None:
        # One version must be installed by every pipeline and documented; a partial bump would make the
        # backup and the restore run different IntuneCD releases.
        sources = {
            "requirements.txt": r"IntuneCD==([\w.]+)",
            "pyproject.toml": r'"IntuneCD==([\w.]+)"',
            "AGENTS.md": r"pinned to `([\w.]+)` via `INTUNECD_VERSION`",
            "azure-pipelines.yml": r"name: INTUNECD_VERSION\s+value: ([\w.]+)",
            "azure-pipelines-restore.yml": r"name: INTUNECD_VERSION\s+value: ([\w.]+)",
            ".github/workflows/astral-backup.yml": r'INTUNECD_VERSION: "([\w.]+)"',
            ".github/workflows/astral-restore.yml": r'INTUNECD_VERSION: "([\w.]+)"',
            ".gitea/workflows/astral-backup.yml": r'INTUNECD_VERSION: "([\w.]+)"',
            ".gitea/workflows/astral-restore.yml": r'INTUNECD_VERSION: "([\w.]+)"',
        }
        found = {}
        for rel, pattern in sources.items():
            match = re.search(pattern, (ROOT / rel).read_text(encoding="utf-8"))
            self.assertIsNotNone(match, f"no IntuneCD version found in {rel}")
            found[rel] = match.group(1)
        self.assertEqual(len(set(found.values())), 1, f"IntuneCD versions disagree: {found}")
        # Nothing may install it from a literal that bypasses the shared variable.
        for path in ROOT.glob("azure-pipelines*.yml"):
            self.assertNotRegex(path.read_text(encoding="utf-8"), r"IntuneCD==\d")

    def test_requirements_are_bounded(self) -> None:
        # requirements.in holds the direct dependencies: pinned or capped at the next major version.
        for path in sorted((ROOT / "infra").glob("*/requirements.in")):
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    with self.subTest(file=str(path.relative_to(ROOT)), requirement=line):
                        self.assertRegex(line, r"==|<", "pin it or cap its next major version")

    def test_requirements_lock_files_are_hash_pinned(self) -> None:
        # requirements.txt is the uv-compiled lock: every package pinned with sha256 hashes, so installs are reproducible.
        for path in sorted((ROOT / "infra").glob("*/requirements.txt")):
            text = path.read_text(encoding="utf-8")
            entries = re.findall(r"^([A-Za-z0-9_.\[\]-]+)(==[^\s\\]+)?[^\n]*\\\n((?:\s+--hash=sha256:[0-9a-f]{64}\s*\\?\n?)+)",
                                 text, flags=re.MULTILINE)
            packages = [m for m in re.findall(r"^([A-Za-z0-9_.\[\]-]+)==", text, flags=re.MULTILINE)]
            self.assertTrue(packages, f"{path} has no pinned packages")
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertEqual(len(packages), len(entries), "every pinned package needs sha256 hashes")
            self.assertTrue((path.parent / "requirements.in").is_file(), f"{path.parent} lacks requirements.in")

    def test_runtime_module_installs_are_version_bounded(self) -> None:
        text = (ROOT / "scripts/commit_intune_drift.ps1").read_text(encoding="utf-8")
        for line in text.splitlines():
            if "Install-Module" in line and not line.strip().startswith("#"):
                self.assertIn("-MaximumVersion", line)

    def test_powershell_scripts_with_non_ascii_text_have_a_bom(self) -> None:
        # Windows PowerShell 5.1 reads BOM-less files as ANSI: an em dash in a string literal then ends the string.
        for path in sorted((ROOT / "deploy").glob("*.ps1")):
            raw = path.read_bytes()
            with self.subTest(script=path.name):
                if any(byte > 127 for byte in raw):
                    self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "non-ASCII PowerShell needs a UTF-8 BOM")

    def test_powershell_scripts_avoid_join_path_with_three_or_more_parts(self) -> None:
        # Join-Path -AdditionalChildPath (positional extras) exists only in PowerShell 6+; the scripts claim 5.1.
        pattern = re.compile(r'Join-Path\s+\S+\s+"[^"]*"\s+"[^"]*"')
        for path in sorted((ROOT / "deploy").glob("*.ps1")):
            with self.subTest(script=path.name):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8-sig")))


if __name__ == "__main__":
    unittest.main()
