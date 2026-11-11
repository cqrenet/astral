from __future__ import annotations

import base64
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


common = _load("common")
compat = _load("forge_compat")

REPO_API = "https://ghes.example/github/_apis/git/repositories/github"
ENV = {
    "GITHUB_API_URL": "https://ghes.example/api/v3",
    "GITHUB_GRAPHQL_URL": "https://ghes.example/api/graphql",
    "GITHUB_REPOSITORY": "ssd/astral",
    "GITHUB_SERVER_URL": "https://ghes.example",
    "ASTRAL_SCM": "github",
}


def _pr(number, state="open", merged_at=None, draft=False):
    return {
        "number": number, "state": state, "merged_at": merged_at, "draft": draft, "node_id": f"PR_{number}",
        "title": "t", "body": "b", "html_url": f"https://ghes.example/ssd/astral/pull/{number}",
        "head": {"ref": "drift/intune", "sha": "headsha"}, "base": {"ref": "main", "sha": "basesha"},
        "merge_commit_sha": "mergesha", "created_at": "2026-11-01T00:00:00Z", "closed_at": None,
    }


def _marker(path: str) -> str:
    return "Automation marker: AUTO-CHANGE-TICKET:" + base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")


class FakeGitHub:
    """Routes compat._http calls to canned responses and records them."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, token, body=None):
        self.calls.append((method, url, body))
        for (m, fragment), response in self.routes.items():
            if m == method and fragment in url:
                return response(body) if callable(response) else response
        raise AssertionError(f"unexpected call {method} {url}")


class GithubAdoCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        compat._thread_kind.clear()
        patcher = patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, routes, *args, **kwargs):
        fake = FakeGitHub(routes)
        with patch.object(compat, "_http", fake):
            return compat.request_json(*args, token="t", **kwargs), fake

    def test_completed_prs_only_merged_with_votes(self) -> None:
        routes = {
            ("GET", "/pulls?"): [_pr(1, "closed", merged_at="2026-11-02T00:00:00Z"), _pr(2, "closed")],
            ("GET", "/pulls/1/reviews"): [
                {"user": {"login": "rev", "id": 7}, "state": "APPROVED"},
                {"user": {"login": "rev", "id": 7}, "state": "COMMENTED"},
                {"user": {"login": "other", "id": 8}, "state": "CHANGES_REQUESTED"},
            ],
        }
        result, fake = self._run(
            routes,
            f"{REPO_API}/pullrequests?searchCriteria.status=completed&searchCriteria.sourceRefName=refs/heads/drift/intune",
        )
        prs = result["value"]
        self.assertEqual([p["pullRequestId"] for p in prs], [1])
        self.assertEqual(prs[0]["status"], "completed")
        self.assertEqual(prs[0]["lastMergeCommit"], {"commitId": "mergesha"})
        votes = {r["uniqueName"]: r["vote"] for r in prs[0]["reviewers"]}
        self.assertEqual(votes, {"rev": 10, "other": -10})
        self.assertIn("head=ssd%3Adrift%2Fintune", fake.calls[0][1])

    def test_abandoned_pr_has_no_merge_commit(self) -> None:
        routes = {("GET", "/pulls?"): [_pr(2, "closed")], ("GET", "/pulls/2/reviews"): []}
        result, _ = self._run(routes, f"{REPO_API}/pullrequests?searchCriteria.status=abandoned")
        self.assertEqual(result["value"][0]["status"], "abandoned")
        self.assertEqual(result["value"][0]["lastMergeCommit"], {})

    def test_threads_map_review_and_issue_comments(self) -> None:
        graphql = {"data": {"repository": {"pullRequest": {
            "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": [{
                "id": "RT_1", "isResolved": True, "path": "tenant-state/intune/a.json",
                "comments": {"nodes": [
                    {"databaseId": 10, "body": "ticket", "createdAt": "x", "lastEditedAt": None, "author": {"login": "github-actions"}},
                    {"databaseId": 11, "body": "/reject", "createdAt": "y", "lastEditedAt": "z", "author": {"login": "rev", "databaseId": 7}},
                ]},
            }]},
            "comments": {"pageInfo": {"hasNextPage": False}, "nodes": [
                {"databaseId": 20, "body": "AI narrative", "createdAt": "x", "lastEditedAt": None, "author": {"login": "github-actions"}},
            ]},
        }}}}
        result, _ = self._run({("POST", "/graphql"): graphql}, f"{REPO_API}/pullrequests/5/threads?api-version=7.1")
        review, issue = result["value"]
        self.assertEqual((review["id"], review["status"]), (10, 4))
        self.assertEqual(review["comments"][1]["parentCommentId"], 10)
        self.assertTrue(review["comments"][1]["isEdited"])
        self.assertEqual(review["comments"][1]["author"], {"uniqueName": "rev", "id": "7", "displayName": "rev"})
        self.assertEqual((issue["id"], issue["status"]), (20, 1))
        # Edited comments never count as reviewer decisions.
        self.assertIsNone(common.latest_thread_decision(review["comments"], {"rev"}))

    def test_threads_follow_both_graphql_cursors(self) -> None:
        def page(threads, comments, threads_more=False, comments_more=False):
            return {"data": {"repository": {"pullRequest": {
                "reviewThreads": {"pageInfo": {"hasNextPage": threads_more, "endCursor": "T" + str(len(threads))}, "nodes": threads},
                "comments": {"pageInfo": {"hasNextPage": comments_more, "endCursor": "C" + str(len(comments))}, "nodes": comments},
            }}}}

        def thread(i):
            return {"id": f"RT_{i}", "isResolved": False, "path": "p", "comments": {"nodes": [
                {"databaseId": 100 + i, "body": "b", "createdAt": "x", "lastEditedAt": None, "author": {"login": "a"}}]}}

        def comment(i):
            return {"databaseId": 200 + i, "body": "c", "createdAt": "x", "lastEditedAt": None, "author": {"login": "a"}}

        pages = iter([
            page([thread(1)], [comment(1)], threads_more=True, comments_more=True),
            page([thread(2)], [comment(2)], threads_more=True),  # comments finished after page 1
            page([thread(3)], []),
        ])
        seen = []

        def graphql(body):
            seen.append(body["variables"])
            return next(pages)

        result, _ = self._run({("POST", "/graphql"): graphql}, f"{REPO_API}/pullrequests/5/threads?api-version=7.1")
        self.assertEqual([t["id"] for t in result["value"]], [101, 102, 103, 201, 202])
        self.assertEqual([(v["threads"], v["comments"]) for v in seen], [(None, None), ("T1", "C1"), ("T1", "C1")])

    def test_ticket_thread_becomes_file_review_comment(self) -> None:
        path = "tenant-state/intune/Scripts/a.json"
        routes = {
            ("GET", "/pulls/5"): _pr(5),
            ("POST", "/pulls/5/comments"): {"id": 99},
        }
        result, fake = self._run(
            routes, f"{REPO_API}/pullrequests/5/threads?api-version=7.1", method="POST",
            body={"comments": [{"content": f"Change needed\n\n{_marker(path)}"}], "status": 1},
        )
        self.assertEqual(result["id"], 99)
        body = fake.calls[-1][2]
        self.assertEqual((body["path"], body["commit_id"], body["subject_type"]), (path, "headsha", "file"))

    def test_other_thread_is_issue_comment_and_updates_in_place(self) -> None:
        routes = {("POST", "/issues/5/comments"): {"id": 50}}
        self._run(routes, f"{REPO_API}/pullrequests/5/threads", method="POST", body={"comments": [{"content": "guide"}]})
        routes = {("GET", "/issues/comments/50"): {"body": "guide"}, ("PATCH", "/issues/comments/50"): {"id": 50}}
        _, fake = self._run(routes, f"{REPO_API}/pullrequests/5/threads/50/comments", method="POST", body={"content": "guide v2"})
        self.assertEqual(fake.calls[-1][0], "PATCH")

    def test_reply_to_issue_ticket_never_overwrites_marker(self) -> None:
        compat._thread_kind[(5, 60)] = ("issue", "")
        routes = {("GET", "/issues/comments/60"): {"body": _marker("tenant-state/intune/a.json")}, ("POST", "/issues/5/comments"): {"id": 61}}
        _, fake = self._run(routes, f"{REPO_API}/pullrequests/5/threads/60/comments", method="POST", body={"content": "Auto-action"})
        self.assertEqual(fake.calls[-1][0], "POST")

    def test_queue_build_dispatches_workflow_with_string_inputs(self) -> None:
        routes = {("POST", "/actions/workflows/astral-restore.yml/dispatches"): {}}
        result, fake = self._run(
            routes, "https://ghes.example/github/_apis/build/builds?api-version=7.1", method="POST",
            body={"definition": {"id": "astral-restore.yml"}, "sourceBranch": "refs/heads/main",
                  "templateParameters": {"dryRun": True, "maxWorkers": 10, "restoreMode": "full"}},
        )
        self.assertEqual(fake.calls[0][2], {"ref": "main", "inputs": {"dryRun": "true", "maxWorkers": "10", "restoreMode": "full"}})
        self.assertIn("/actions/workflows/astral-restore.yml", result["_links"]["web"]["href"])

    def test_unknown_endpoint_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._run({}, f"{REPO_API}/pushes", method="POST", body={})

    def test_common_request_json_routes_only_on_github(self) -> None:
        with patch.object(compat, "request_json", return_value={"routed": True}) as routed:
            self.assertEqual(common.request_json(f"{REPO_API}/pullrequests/1", token="t"), {"routed": True})
            routed.assert_called_once()


class PostMergeRestoreOnGithubTests(unittest.TestCase):
    """queue_post_merge_restore.py end to end through the compat layer."""

    def _run_main(self, reject_author: str, edited: bool):
        module = _load("queue_post_merge_restore")
        path = "tenant-state/intune/Scripts/a.json"
        merged = dict(_pr(7, "closed", merged_at="2099-01-01T00:00:00Z"), closed_at="2099-01-01T00:00:00Z")
        graphql = {"data": {"repository": {"pullRequest": {
            "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": [{
                "id": "RT", "isResolved": False, "path": path,
                "comments": {"nodes": [
                    {"databaseId": 1, "body": _marker(path), "lastEditedAt": None, "author": {"login": "github-actions"}},
                    {"databaseId": 2, "body": "/reject", "lastEditedAt": "x" if edited else None,
                     "author": {"login": reject_author}},
                ]},
            }]},
            "comments": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        }}}}
        fake = FakeGitHub({
            ("GET", "/pulls?"): [merged],
            ("GET", "/pulls/7/reviews"): [],
            ("POST", "/graphql"): graphql,
            ("POST", "/dispatches"): {},
            ("POST", "/issues/7/comments"): {"id": 3},
        })
        env = dict(ENV, SYSTEM_ACCESSTOKEN="t", SYSTEM_COLLECTIONURI="https://ghes.example", SYSTEM_TEAMPROJECT="github",
                   BUILD_REPOSITORY_ID="github", AUTO_REMEDIATE_AFTER_MERGE="true",
                   AUTO_REMEDIATE_RESTORE_PIPELINE_ID="astral-restore.yml", PR_DECISION_ALLOWED_REVIEWERS="Reviewer")
        argv = ["x", "--workload", "intune", "--drift-branch", "drift/intune", "--baseline-branch", "main"]
        with patch.dict(os.environ, env), patch.object(compat, "_http", fake), patch.object(sys, "argv", argv):
            self.assertEqual(module.main(), 0)
        return [c for c in fake.calls if "/dispatches" in c[1]]

    def test_allowed_reviewer_reject_dispatches_selective_restore(self) -> None:
        dispatches = self._run_main("reviewer", edited=False)
        self.assertEqual(len(dispatches), 1)
        inputs = dispatches[0][2]["inputs"]
        self.assertEqual((inputs["restoreMode"], inputs["restorePathsCsv"]), ("selective", "tenant-state/intune/Scripts/a.json"))

    def test_unlisted_or_edited_reject_dispatches_nothing(self) -> None:
        self.assertEqual(self._run_main("intruder", edited=False), [])
        self.assertEqual(self._run_main("reviewer", edited=True), [])


GITEA_ENV = dict(ENV, ASTRAL_SCM="gitea", GITHUB_API_URL="https://gitea.example/api/v1", GITHUB_SERVER_URL="https://gitea.example")


def _gitea_comment(cid, body, login="rev", edited=False):
    return {"id": cid, "body": body, "user": {"login": login, "id": 7},
            "created_at": "2026-11-10T10:00:00Z", "updated_at": "2026-11-10T11:00:00Z" if edited else "2026-11-10T10:00:00Z"}


class GiteaCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        compat._thread_kind.clear()
        patcher = patch.dict(os.environ, GITEA_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, routes, *args, **kwargs):
        fake = FakeGitHub(routes)
        with patch.object(compat, "_http", fake):
            return compat.request_json(*args, token="t", **kwargs), fake

    def test_pr_list_filters_branches_and_maps_request_changes(self) -> None:
        other = dict(_pr(3, "closed", merged_at="x"), head={"ref": "feature", "sha": "s"})
        merged = dict(_pr(4, "closed"), merged=True, merged_at="2026-11-02T00:00:00Z")
        routes = {
            ("GET", "/pulls?state=closed"): [other, merged],
            ("GET", "/pulls/4/reviews"): [
                {"user": {"login": "rev", "id": 7}, "state": "REQUEST_CHANGES"},
                {"user": {"login": "old", "id": 8}, "state": "REQUEST_CHANGES", "dismissed": True},
            ],
        }
        result, fake = self._run(routes, f"{REPO_API}/pullrequests?searchCriteria.status=completed"
                                 "&searchCriteria.sourceRefName=refs/heads/drift/intune&searchCriteria.targetRefName=refs/heads/main")
        self.assertEqual([p["pullRequestId"] for p in result["value"]], [4])
        self.assertEqual(result["value"][0]["reviewers"], [{"uniqueName": "rev", "id": "7", "displayName": "rev", "vote": -10}])
        self.assertTrue(fake.calls[0][1].startswith("https://gitea.example/api/v1/repos/ssd/astral/pulls?"))

    def test_quote_reply_joins_ticket_thread(self) -> None:
        path = "tenant-state/intune/Settings Catalog/A.json"
        ticket = f"Change needed\n\nPolicy file: {path}\n\n{_marker(path)}"
        quote = "\n".join("> " + line for line in ticket.splitlines())
        routes = {("GET", "/issues/5/comments"): [
            _gitea_comment(10, ticket, login="gitea-actions"),
            _gitea_comment(11, quote + "\n\n/reject", login="rev"),
            _gitea_comment(12, quote + "\n\n/accept", login="rev", edited=True),
            _gitea_comment(13, "unrelated", login="rev"),
        ]}
        result, _ = self._run(routes, f"{REPO_API}/pullrequests/5/threads")
        ticket_thread, other = result["value"]
        self.assertEqual([c["id"] for c in ticket_thread["comments"]], [10, 11, 12])
        self.assertEqual(other["id"], 13)
        # The edited /accept is ignored, so the reviewer's /reject stands.
        self.assertEqual(common.latest_thread_decision(ticket_thread["comments"], {"rev"}), "reject")
        self.assertEqual(common.ticket_path_from_content(ticket_thread["comments"][0]["content"]), path)

    def test_ticket_thread_is_plain_comment_and_token_auth(self) -> None:
        routes = {("POST", "/issues/5/comments"): {"id": 70}}
        result, fake = self._run(routes, f"{REPO_API}/pullrequests/5/threads", method="POST",
                                 body={"comments": [{"content": _marker("tenant-state/intune/a.json")}]})
        self.assertEqual(result["id"], 70)
        self.assertEqual(len(fake.calls), 1)

    def test_paged_tree_and_commit_shape(self) -> None:
        routes = {
            ("GET", "/git/commits/abc"): {"sha": "abc", "commit": {"tree": {"sha": "tree1"}}},
            ("GET", "&page=1"): {"tree": [{"path": "a", "type": "blob", "sha": "1"}], "truncated": True},
            ("GET", "&page=2"): {"tree": [{"path": "b", "type": "blob", "sha": "2"}], "truncated": False},
        }
        commit, _ = self._run(routes, f"{REPO_API}/commits/abc")
        self.assertEqual(commit["treeId"], "tree1")
        tree, _ = self._run(routes, f"{REPO_API}/trees/tree1?recursive=true")
        self.assertEqual([e["relativePath"] for e in tree["treeEntries"]], ["a", "b"])

    def test_gitea_uses_token_auth_scheme(self) -> None:
        seen = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(request, timeout):
            seen["auth"] = request.get_header("Authorization")
            return Response()

        with patch.object(compat.urllib.request, "urlopen", fake_urlopen):
            compat._http("GET", "https://gitea.example/api/v1/x", "abc")
        self.assertEqual(seen["auth"], "token abc")


class ParsePipelineIdTests(unittest.TestCase):
    def test_numeric_id_everywhere(self) -> None:
        self.assertEqual(common.parse_pipeline_id("42", "X"), 42)

    def test_workflow_file_name_on_github_and_gitea(self) -> None:
        for scm in ("github", "gitea"):
            with self.subTest(scm=scm), patch.dict(os.environ, {"ASTRAL_SCM": scm}):
                self.assertEqual(common.parse_pipeline_id("astral-restore.yml", "X"), "astral-restore.yml")

    def test_workflow_file_name_rejected_on_ado(self) -> None:
        # Regression: Gitea was rejected here as well because only "github" was allowed.
        with patch.dict(os.environ, {"ASTRAL_SCM": "ado"}):
            with self.assertRaises(SystemExit):
                common.parse_pipeline_id("astral-restore.yml", "X")


class TicketMarkerTests(unittest.TestCase):
    def test_apply_rejections_reads_marker_written_by_summary(self) -> None:
        # Regression: apply_reviewer_rejections only accepted "<!-- AUTO-CHANGE-TICKET:... -->", but
        # update_pr_review_summary writes "Automation marker: AUTO-CHANGE-TICKET:..." -> /reject never applied.
        module = _load("apply_reviewer_rejections")
        path = "tenant-state/intune/Scripts/a.json"
        self.assertEqual(module.common.ticket_path_from_content(f"Change needed\n\n{_marker(path)}"), path)
        encoded = _marker(path).split(":")[-1]
        self.assertEqual(module.common.ticket_path_from_content(f"<!-- AUTO-CHANGE-TICKET:{encoded} -->"), path)


class GraphTokenRoleTests(unittest.TestCase):
    @unittest.skipUnless(__import__("shutil").which("openssl"), "openssl not installed")
    def test_certificate_assertion_is_verifiable(self) -> None:
        import json as _json
        import subprocess
        import tempfile

        token_mod = _load("get_graph_token")
        with tempfile.TemporaryDirectory() as tmp:
            key, cert, both = (os.path.join(tmp, n) for n in ("k.pem", "c.pem", "both.pem"))
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                            "-days", "1", "-subj", "/CN=astral-test"], check=True, capture_output=True)
            Path(both).write_text(Path(key).read_text() + Path(cert).read_text())
            assertion = token_mod.certificate_assertion(both, "client-1", "https://login.example/t/oauth2/v2.0/token", now=1000)
            header_b64, claims_b64, sig_b64 = assertion.split(".")
            pad = lambda value: value + "=" * (-len(value) % 4)  # noqa: E731
            claims = _json.loads(base64.urlsafe_b64decode(pad(claims_b64)))
            self.assertEqual((claims["iss"], claims["sub"], claims["exp"]), ("client-1", "client-1", 1600))
            self.assertIn("x5t#S256", _json.loads(base64.urlsafe_b64decode(pad(header_b64))))
            Path(tmp, "sig").write_bytes(base64.urlsafe_b64decode(pad(sig_b64)))
            Path(tmp, "data").write_text(f"{header_b64}.{claims_b64}")
            pub = subprocess.run(["openssl", "x509", "-in", cert, "-pubkey", "-noout"], check=True, capture_output=True).stdout
            Path(tmp, "pub.pem").write_bytes(pub)
            verify = subprocess.run(["openssl", "dgst", "-sha256", "-verify", os.path.join(tmp, "pub.pem"),
                                     "-signature", os.path.join(tmp, "sig"), os.path.join(tmp, "data")], capture_output=True)
            self.assertEqual(verify.returncode, 0)

    def test_missing_roles_supports_alternatives(self) -> None:
        token_mod = _load("get_graph_token")
        roles = ["DeviceManagementScripts.ReadWrite.All", "Group.Read.All"]
        self.assertEqual(
            token_mod.missing_roles(roles, ["DeviceManagementScripts.Read.All|DeviceManagementScripts.ReadWrite.All", "Policy.Read.All"]),
            ["Policy.Read.All"],
        )


if __name__ == "__main__":
    unittest.main()
