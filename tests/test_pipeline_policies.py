"""Structural checks over the CI definitions (Azure DevOps, GitHub Actions, Gitea Actions).

They encode rules the pipelines must keep: no injection through templated inputs, every step that
applies reviewer decisions receives the allowlist, and every script invocation matches the script's
real command line. Needs PyYAML (the rest of the test suite is stdlib only), so they skip without it.
"""

from __future__ import annotations

import ast
import glob
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
ADO_FILES = ["azure-pipelines.yml", "azure-pipelines-review-sync.yml", "azure-pipelines-restore.yml",
             "deploy/update-from-upstream.yml", "deploy/validate-deployment.yml"]
ACTIONS_FILES = sorted(glob.glob(str(ROOT / ".github/workflows/*.yml")) + glob.glob(str(ROOT / ".gitea/workflows/*.yml")))
ALL_YAML = [str(ROOT / f) for f in ADO_FILES] + ACTIONS_FILES + [str(ROOT / "templates" / n) for n in ("variables-common.yml", "variables-tenant.yml")]


def _load(path: str):
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _ado_steps(node):
    """(step, script text) for every Azure DevOps task / script step."""
    if isinstance(node, dict):
        shorthand = [k for k in ("bash", "script", "pwsh", "powershell") if isinstance(node.get(k), str)]
        if "task" in node or shorthand:
            if "task" in node:
                inputs = node.get("inputs") or {}
                body = "\n".join(str(inputs.get(k, "")) for k in ("script", "Inline", "inlineScript"))
            else:
                body = node[shorthand[0]]
            yield node, body
            return
        for value in node.values():
            yield from _ado_steps(value)
    elif isinstance(node, list):
        for value in node:
            yield from _ado_steps(value)


@unittest.skipUnless(yaml, "PyYAML not installed")
class PipelinePolicyTests(unittest.TestCase):
    def test_every_pipeline_file_parses(self) -> None:
        for path in ALL_YAML:
            with self.subTest(path=os.path.relpath(path, ROOT)):
                self.assertIsNotNone(_load(path))

    def test_actions_run_steps_never_embed_expressions(self) -> None:
        # ${{ }} inside a run: script is expanded before the shell parses it: pass values through env: instead.
        for path in ACTIONS_FILES:
            for job_name, job in (_load(path).get("jobs") or {}).items():
                for step in job.get("steps") or []:
                    with self.subTest(path=os.path.relpath(path, ROOT), job=job_name, step=step.get("name")):
                        self.assertNotIn("${{", step.get("run", ""))

    def test_gitea_workflows_use_no_marketplace_actions(self) -> None:
        for path in glob.glob(str(ROOT / ".gitea/workflows/*.yml")):
            for job in (_load(path).get("jobs") or {}).values():
                for step in job.get("steps") or []:
                    with self.subTest(path=os.path.relpath(path, ROOT)):
                        self.assertNotIn("uses", step)

    def test_ado_scripts_only_template_typed_parameters(self) -> None:
        # A string parameter expanded into script text is shell injection for whoever can queue the run.
        for rel in ADO_FILES:
            data = _load(str(ROOT / rel))
            types = {p["name"]: p.get("type", "string") for p in data.get("parameters") or []} if isinstance(data.get("parameters"), list) else {}
            for step, body in _ado_steps(data):
                for expr in re.findall(r"\$\{\{\s*([^}]+?)\s*\}\}", body):
                    name = expr.split(".", 1)[1] if expr.startswith("parameters.") else expr
                    with self.subTest(file=rel, step=step.get("displayName"), expr=expr):
                        self.assertIn(types.get(name), ("boolean", "number"), f"{expr} must reach the script through env:")

    def test_steps_applying_reviewer_decisions_receive_the_allowlist(self) -> None:
        needing = ("ensure_rolling_pr.py", "apply_reviewer_rejections.py", "queue_post_merge_restore.py")
        for rel in ("azure-pipelines.yml", "azure-pipelines-review-sync.yml"):
            for step, body in _ado_steps(_load(str(ROOT / rel))):
                for script in needing:
                    if script in body:
                        with self.subTest(file=rel, step=step.get("displayName"), script=script):
                            self.assertIn("PR_DECISION_ALLOWED_REVIEWERS", step.get("env") or {})

    def test_restore_mode_is_constrained_everywhere(self) -> None:
        ado = {p["name"]: p for p in _load(str(ROOT / "azure-pipelines-restore.yml"))["parameters"]}
        self.assertEqual(ado["restoreMode"].get("values"), ["full", "selective"])

    def test_script_invocations_use_flags_the_script_accepts(self) -> None:
        definitions = {}
        for path in glob.glob(str(ROOT / "scripts" / "*.py")):
            flags = {}
            for node in ast.walk(ast.parse(Path(path).read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument":
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("--"):
                            flags[arg.value] = any(k.arg == "required" and getattr(k.value, "value", False) is True for k in node.keywords)
            definitions[os.path.basename(path)] = flags

        files = ADO_FILES + [os.path.relpath(p, ROOT) for p in ACTIONS_FILES]
        files += [os.path.relpath(p, ROOT) for p in glob.glob(str(ROOT / "scripts" / "*.sh")) + glob.glob(str(ROOT / "scripts" / "*.ps1"))]
        checked = 0
        for rel in files:
            lines = (ROOT / rel).read_text(encoding="utf-8").split("\n")
            for index, line in enumerate(lines):
                match = re.search(r"scripts/([A-Za-z0-9_]+\.py)\b", line)
                if not match or match.group(1) not in definitions or re.search(r"\b(cp|Copy-Item)\b", line):
                    continue
                block, end = line, index
                while lines[end].rstrip().endswith(("\\", "`")) and end + 1 < len(lines):
                    end += 1
                    block += "\n" + lines[end]
                used = set(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", block))
                known = definitions[match.group(1)]
                checked += 1
                with self.subTest(file=rel, line=index + 1, script=match.group(1)):
                    self.assertEqual({u for u in used if not any(k.startswith(u) for k in known)}, set())
                    if "${common[@]}" not in block and "$@" not in block:  # flags passed through an array/arg list
                        self.assertEqual({f for f, required in known.items() if required and f not in used}, set())
        self.assertGreater(checked, 30)

    def test_every_referenced_script_exists(self) -> None:
        for rel in ADO_FILES + [os.path.relpath(p, ROOT) for p in ACTIONS_FILES]:
            for name in re.findall(r"scripts/((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|sh|ps1))", (ROOT / rel).read_text(encoding="utf-8")):
                with self.subTest(file=rel, script=name):
                    self.assertTrue((ROOT / "scripts" / name).exists())


@unittest.skipUnless(yaml and shutil.which("bash"), "needs PyYAML and bash")
class UpdateFromUpstreamTests(unittest.TestCase):
    """The 'Fetch upstream' step against a local stand-in for the upstream repository."""

    def _step_script(self) -> str:
        data = _load(str(ROOT / "deploy/update-from-upstream.yml"))
        for step, body in _ado_steps(data):
            if step.get("displayName") == "Fetch upstream":
                return body
        raise AssertionError("Fetch upstream step not found")

    def _run(self, ref_input: str):
        git = lambda cwd, *a: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
        with tempfile.TemporaryDirectory() as td:
            upstream, work = str(Path(td, "upstream")), str(Path(td, "work"))
            subprocess.run(["git", "init", "-q", "-b", "main", upstream], check=True)
            Path(upstream, "f").write_text("1")
            git(upstream, "add", ".")
            git(upstream, "commit", "-q", "-m", "one")
            Path(upstream, "f").write_text("2")
            git(upstream, "commit", "-q", "-am", "two")
            git(upstream, "tag", "v1.0.0")
            Path(upstream, "f").write_text("3")
            git(upstream, "commit", "-q", "-am", "three")
            subprocess.run(["git", "clone", "-q", upstream, work], check=True, capture_output=True)
            git(work, "reset", "-q", "--hard", "HEAD~2")  # behind both the tag and main, so there is something to fetch
            script = self._step_script().replace("https://github.com/cqrenet/astral", upstream)
            marker = Path(td, "pwned")
            env = {**os.environ, "UPSTREAM_REF_INPUT": ref_input.replace("MARKER", str(marker))}
            result = subprocess.run(["bash", "-c", script], cwd=work, capture_output=True, text=True, env=env)
            return result, marker.exists(), git(upstream, "rev-parse", "main"), git(upstream, "rev-parse", "v1.0.0")

    def test_latest_and_tags_resolve(self) -> None:
        result, pwned, main_sha, tag_sha = self._run("latest")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"variable=UPSTREAM_SHA]{main_sha}", result.stdout)
        result, _, _, tag_sha = self._run("v1.0.0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"variable=UPSTREAM_SHA]{tag_sha}", result.stdout)

    def test_blank_input_means_latest(self) -> None:
        result, _, main_sha, _ = self._run("   ")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"variable=FETCH_REF]upstream/main", result.stdout)

    def test_malicious_refs_are_rejected_and_never_executed(self) -> None:
        for payload in ('x"; touch MARKER; echo "', "v1; touch MARKER", "$(touch MARKER)", "`touch MARKER`",
                        "--upload-pack=touch MARKER", "-s", "a b", "x\ntouch MARKER"):
            with self.subTest(payload=payload):
                result, executed, _, _ = self._run(payload)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("Invalid upstream ref", result.stdout)
                self.assertFalse(executed)
                self.assertNotIn("variable=FETCH_REF", result.stdout)  # nothing unsafe reaches later steps


if __name__ == "__main__":
    unittest.main()
