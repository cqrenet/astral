#!/usr/bin/env python3
"""Translate the Azure DevOps REST calls used by ASTRAL's PR scripts to GitHub or Gitea.

ensure_rolling_pr, update_pr_review_summary, apply_reviewer_rejections and
queue_post_merge_restore speak Azure DevOps REST. On GitHub/Gitea Actions,
common.request_json routes every ``*/_apis/*`` URL here and gets back the
Azure DevOps-shaped JSON those scripts expect, so their logic runs unchanged.

Mapping                         GitHub (incl. GHES)                Gitea (REST only, no GraphQL)
  pull request                  pull request                       pull request (no drafts)
  ticket thread (marker)        file-level review comment thread   PR comment; "Quote reply" = reply in thread
  any other thread              PR comment, updated in place       PR comment, updated in place
  reviewer vote Reject (-10)    review CHANGES_REQUESTED           review REQUEST_CHANGES
  queue build                   workflow_dispatch                  workflow_dispatch
  commits/{sha}, trees/{sha}    git database API                   git database API (paged trees)

Unknown endpoints raise: new Azure DevOps calls must be mapped here explicitly.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import common  # common.request_json imports this module lazily, so there is no import cycle

QUOTED_TICKET_RE = re.compile(r"(?m)^\s*>.*AUTO-CHANGE-TICKET:(?P<id>[A-Za-z0-9_-]+)")
RESOLVED_STATUSES = {2, 3, 4, 5}  # Azure DevOps: fixed, wontFix, closed, byDesign
_RETRY_CODES = {429, 500, 502, 503, 504}

# (pr number, thread id) -> (kind, review thread node id); filled when threads are listed/created.
_thread_kind: dict[tuple[int, int], tuple[str, str]] = {}

_THREADS_QUERY = """
query($owner:String!, $name:String!, $number:Int!, $threads:String, $comments:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      reviewThreads(first:100, after:$threads) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved path
          comments(first:100) {
            nodes { databaseId body createdAt lastEditedAt author { login ... on User { databaseId } } }
          }
        }
      }
      comments(first:100, after:$comments) {
        pageInfo { hasNextPage endCursor }
        nodes { databaseId body createdAt lastEditedAt author { login ... on User { databaseId } } }
      }
    }
  }
}
"""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _is_gitea() -> bool:
    explicit = os.environ.get("ASTRAL_SCM", "").strip().lower()
    return explicit == "gitea" or (not explicit and os.environ.get("GITEA_ACTIONS") == "true")


def _api() -> str:
    # Gitea runners set GITHUB_API_URL=<server>/api/v1.
    default = os.environ.get("GITHUB_SERVER_URL", "").rstrip("/") + "/api/v1" if _is_gitea() else "https://api.github.com"
    return (os.environ.get("GITHUB_API_URL") or default).rstrip("/")


def _graphql_url() -> str:
    return os.environ.get("GITHUB_GRAPHQL_URL") or f"{_api()}/graphql"


def _repo() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        raise RuntimeError("GITHUB_REPOSITORY is not set (expected owner/repo).")
    return repo


def _http(method: str, url: str, token: str, body: Any = None) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if _is_gitea():
        headers = {"Authorization": f"token {token}", "Accept": "application/json"}
    else:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(3):
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code in _RETRY_CODES and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            detail = exc.read().decode("utf-8", errors="replace")[:2048]
            # Same shape as common.request_json so callers' error checks (e.g. "forbidden") keep working.
            raise RuntimeError(f"{method} {url} failed: HTTP Error {exc.code}: {exc.reason} — {detail}") from exc
    raise RuntimeError(f"{method} {url} failed after retries")


def _rest(method: str, path: str, token: str, body: Any = None) -> Any:
    return _http(method, f"{_api()}/repos/{_repo()}{path}", token, body)


def _graphql(query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
    payload = _http("POST", _graphql_url(), token, {"query": query, "variables": variables})
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL error: {payload['errors']}")
    return payload.get("data") or {}


def _token(headers: dict[str, str] | None, token: str | None) -> str:
    if token:
        return token
    auth = str((headers or {}).get("Authorization", ""))
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    raise RuntimeError("forge_compat: no token supplied for GitHub API call.")


# ---------------------------------------------------------------------------
# Shape conversion
# ---------------------------------------------------------------------------
def _branch(ref: str) -> str:
    ref = (ref or "").strip()
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref


def _merge_strategy() -> str:
    # GitHub/Gitea have no per-PR merge strategy; report the configured one so callers see no drift.
    return common.normalize_merge_strategy(os.environ.get("ROLLING_PR_MERGE_STRATEGY", ""))


def _merged(pr: dict[str, Any]) -> bool:
    return bool(pr.get("merged_at") or pr.get("merged"))


def _to_ado_pr(pr: dict[str, Any], reviewers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    merged = _merged(pr)
    if pr.get("state") == "open":
        status = "active"
    else:
        status = "completed" if merged else "abandoned"
    html_url = pr.get("html_url", "")
    return {
        "pullRequestId": pr["number"],
        "title": pr.get("title") or "",
        "description": pr.get("body") or "",
        "isDraft": bool(pr.get("draft")),
        "status": status,
        "sourceRefName": "refs/heads/" + pr["head"]["ref"],
        "targetRefName": "refs/heads/" + pr["base"]["ref"],
        "creationDate": pr.get("created_at") or "",
        "closedDate": pr.get("closed_at") or "",
        "lastMergeSourceCommit": {"commitId": pr["head"]["sha"]},
        "lastMergeTargetCommit": {"commitId": pr["base"]["sha"]},
        # Only merged PRs have a real merge commit (open/closed PRs carry a test-merge SHA).
        "lastMergeCommit": {"commitId": pr.get("merge_commit_sha") or ""} if merged else {},
        "completionOptions": {"mergeStrategy": _merge_strategy()},
        "url": html_url,
        "_links": {"web": {"href": html_url}},
        "reviewers": reviewers or [],
    }


def _reviewers(number: int, token: str) -> list[dict[str, Any]]:
    """Latest review state per user, as Azure DevOps votes (Reject = -10)."""
    latest: dict[str, dict[str, Any]] = {}
    page = "limit=50" if _is_gitea() else "per_page=100"
    for review in _rest("GET", f"/pulls/{number}/reviews?{page}", token):
        user = review.get("user") or {}
        state = review.get("state")
        if not user.get("login") or review.get("dismissed"):
            continue
        if state in ("COMMENTED", "COMMENT", "PENDING", "REQUEST_REVIEW"):
            continue  # comments/requests don't change a reviewer's approval state
        latest[user["login"]] = {"user": user, "state": state}
    votes = {"APPROVED": 10, "CHANGES_REQUESTED": -10, "REQUEST_CHANGES": -10}
    return [
        {
            "uniqueName": login,
            "id": str(item["user"].get("id", "")),
            "displayName": login,
            "vote": votes.get(item["state"], 0),
        }
        for login, item in latest.items()
    ]


def _comment(node: dict[str, Any], parent_id: int) -> dict[str, Any]:
    author = node.get("author") or {}
    return {
        "id": node.get("databaseId"),
        "parentCommentId": parent_id,
        "content": node.get("body") or "",
        "author": {
            "uniqueName": author.get("login", ""),
            "id": str(author.get("databaseId", "") or ""),
            "displayName": author.get("login", ""),
        },
        "isDeleted": False,
        # Anyone with write access can edit other users' comments on GitHub, so edited
        # comments must never count as reviewer decisions (see common.latest_thread_decision).
        "isEdited": node.get("lastEditedAt") is not None,
        "publishedDate": node.get("createdAt") or "",
    }


def _rest_comment(comment: dict[str, Any], parent_id: int) -> dict[str, Any]:
    """REST issue comment (Gitea) in Azure DevOps comment shape."""
    user = comment.get("user") or {}
    return {
        "id": comment.get("id"),
        "parentCommentId": parent_id,
        "content": comment.get("body") or "",
        "author": {"uniqueName": user.get("login", ""), "id": str(user.get("id", "")), "displayName": user.get("login", "")},
        "isDeleted": False,
        # Gitea also lets write users edit others' comments; edited comments never count as decisions.
        "isEdited": (comment.get("updated_at") or "") != (comment.get("created_at") or ""),
        "publishedDate": comment.get("created_at") or "",
    }


# ---------------------------------------------------------------------------
# Pull requests
# ---------------------------------------------------------------------------
def _list_prs(query: dict[str, str], token: str) -> list[dict[str, Any]]:
    status = query.get("searchCriteria.status", "active").lower()
    source = _branch(query.get("searchCriteria.sourceRefName", ""))
    target = _branch(query.get("searchCriteria.targetRefName", ""))
    if _is_gitea():
        # Gitea's list has no head/base filter: newest 50 by update, filtered here.
        state = {"active": "open", "completed": "closed", "abandoned": "closed"}.get(status, "all")
        prs = [
            pr
            for pr in _rest("GET", f"/pulls?state={state}&sort=recentupdate&limit=50", token)
            if (not source or pr["head"]["ref"] == source) and (not target or pr["base"]["ref"] == target)
        ]
        return _filter_and_map(prs, status, token)
    params = {
        "state": {"active": "open", "completed": "closed", "abandoned": "closed"}.get(status, "all"),
        "sort": "updated",
        "direction": "desc",
        # ponytail: newest page only (30 closed / 100 open); rolling branches only need recent PRs.
        "per_page": "100" if status == "active" else "30",
    }
    if source:
        params["head"] = f"{_repo().split('/')[0]}:{source}"
    if target:
        params["base"] = target
    return _filter_and_map(_rest("GET", "/pulls?" + urllib.parse.urlencode(params), token), status, token)


def _filter_and_map(prs: list[dict[str, Any]], status: str, token: str) -> list[dict[str, Any]]:
    result = []
    for pr in prs:
        merged = _merged(pr)
        if (status == "completed" and not merged) or (status == "abandoned" and merged):
            continue
        reviewers = _reviewers(pr["number"], token) if pr.get("state") == "closed" else []
        result.append(_to_ado_pr(pr, reviewers))
    return result


def _get_pr(number: int, token: str) -> dict[str, Any]:
    pr = _rest("GET", f"/pulls/{number}", token)
    return _to_ado_pr(pr, _reviewers(number, token) if pr.get("state") == "closed" else [])


def _create_pr(body: dict[str, Any], token: str) -> dict[str, Any]:
    pr = _rest(
        "POST",
        "/pulls",
        token,
        {
            "title": body.get("title", ""),
            "head": _branch(body.get("sourceRefName", "")),
            "base": _branch(body.get("targetRefName", "")),
            "body": body.get("description", ""),
            "draft": bool(body.get("isDraft")),
        },
    )
    return _to_ado_pr(pr)


def _update_pr(number: int, body: dict[str, Any], token: str) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if "title" in body:
        patch["title"] = body["title"]
    if "description" in body:
        patch["body"] = body["description"]
    pr = _rest("PATCH", f"/pulls/{number}", token, patch) if patch else _rest("GET", f"/pulls/{number}", token)
    if not _is_gitea() and "isDraft" in body and bool(body["isDraft"]) != bool(pr.get("draft")):  # Gitea: no drafts
        mutation = "convertPullRequestToDraft" if body["isDraft"] else "markPullRequestReadyForReview"
        _graphql(
            f"mutation($id:ID!) {{ {mutation}(input:{{pullRequestId:$id}}) {{ clientMutationId }} }}",
            {"id": pr["node_id"]},
            token,
        )
        pr["draft"] = bool(body["isDraft"])
    return _to_ado_pr(pr)


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------
def _list_threads_gitea(number: int, token: str) -> list[dict[str, Any]]:
    """Gitea PR comments are flat. A comment that quotes a ticket comment (Gitea "Quote reply")
    joins that ticket's thread; every other comment is its own thread."""
    comments: list[dict[str, Any]] = []
    for page in range(1, 41):  # ponytail: 2000 comments max per PR
        batch = _rest("GET", f"/issues/{number}/comments?limit=50&page={page}", token)
        comments.extend(batch)
        if len(batch) < 50:
            break
    threads: list[dict[str, Any]] = []
    ticket_threads: dict[str, dict[str, Any]] = {}
    for comment in sorted(comments, key=lambda c: int(c["id"])):
        body = comment.get("body") or ""
        quoted = QUOTED_TICKET_RE.search(body)
        own_marker = common.ticket_path_from_content(body)
        if quoted and not own_marker and quoted.group("id") in ticket_threads:
            thread = ticket_threads[quoted.group("id")]
            thread["comments"].append(_rest_comment(comment, thread["id"]))
            continue
        comment_id = int(comment["id"])
        _thread_kind[(number, comment_id)] = ("issue", "")
        thread = {"id": comment_id, "status": 1, "comments": [_rest_comment(comment, 0)]}
        if own_marker:
            marker = common.TICKET_MARKER_RE.search(body)
            ticket_threads[marker.group("id")] = thread
        threads.append(thread)
    return threads


def _list_threads(number: int, token: str) -> list[dict[str, Any]]:
    if _is_gitea():
        return _list_threads_gitea(number, token)
    owner, name = _repo().split("/", 1)
    review_nodes: list[dict[str, Any]] = []
    issue_nodes: list[dict[str, Any]] = []
    cursors: dict[str, str | None] = {"threads": None, "comments": None}
    more = {"threads": True, "comments": True}
    while more["threads"] or more["comments"]:
        data = _graphql(_THREADS_QUERY, {"owner": owner, "name": name, "number": number, **cursors}, token)
        pull = ((data.get("repository") or {}).get("pullRequest")) or {}
        for key, connection, sink in (
            ("threads", pull.get("reviewThreads") or {}, review_nodes),
            ("comments", pull.get("comments") or {}, issue_nodes),
        ):
            if more[key]:
                sink.extend(connection.get("nodes") or [])
                page = connection.get("pageInfo") or {}
                more[key] = bool(page.get("hasNextPage"))
                cursors[key] = page.get("endCursor")

    threads: list[dict[str, Any]] = []
    for node in review_nodes:
        comments = ((node.get("comments") or {}).get("nodes")) or []
        if not comments:
            continue
        root_id = int(comments[0]["databaseId"])
        _thread_kind[(number, root_id)] = ("review", node.get("id", ""))
        threads.append(
            {
                "id": root_id,
                "status": 4 if node.get("isResolved") else 1,
                "threadContext": {"filePath": "/" + str(node.get("path") or "")},
                "comments": [_comment(c, 0 if i == 0 else root_id) for i, c in enumerate(comments)],
            }
        )
    for node in issue_nodes:
        comment_id = int(node["databaseId"])
        _thread_kind[(number, comment_id)] = ("issue", "")
        threads.append({"id": comment_id, "status": 1, "comments": [_comment(node, 0)]})
    return threads


def _kind(number: int, thread_id: int, token: str, need_node: bool = False) -> tuple[str, str]:
    """(kind, review thread node id); re-lists threads when unknown or the node id is needed."""
    cached = _thread_kind.get((number, thread_id))
    if cached is None or (need_node and cached[0] == "review" and not cached[1]):
        _list_threads(number, token)
        cached = _thread_kind.get((number, thread_id))
    if cached is None:
        raise RuntimeError(f"forge_compat: thread {thread_id} not found on PR #{number}")
    return cached


def _create_thread(number: int, body: dict[str, Any], token: str) -> dict[str, Any]:
    comments = body.get("comments") or [{}]
    content = str(comments[0].get("content", "") or "")
    path = common.ticket_path_from_content(content)
    if path and not _is_gitea():  # Gitea has no file-level review threads
        head_sha = _rest("GET", f"/pulls/{number}", token)["head"]["sha"]
        try:
            created = _rest(
                "POST",
                f"/pulls/{number}/comments",
                token,
                {"body": content, "commit_id": head_sha, "path": path, "subject_type": "file"},
            )
            _thread_kind[(number, int(created["id"]))] = ("review", "")
            return {"id": created["id"], "status": 1}
        except RuntimeError as exc:
            if "HTTP Error 422" not in str(exc):
                raise
            # File not part of the PR diff any more: fall back to a plain PR comment.
    created = _rest("POST", f"/issues/{number}/comments", token, {"body": content})
    _thread_kind[(number, int(created["id"]))] = ("issue", "")
    return {"id": created["id"], "status": 1}


def _set_thread_status(number: int, thread_id: int, status: Any, token: str) -> dict[str, Any]:
    kind, node_id = _kind(number, thread_id, token, need_node=True)
    if kind == "review":
        mutation = "resolveReviewThread" if status in RESOLVED_STATUSES else "unresolveReviewThread"
        _graphql(
            f"mutation($id:ID!) {{ {mutation}(input:{{threadId:$id}}) {{ thread {{ id }} }} }}",
            {"id": node_id},
            token,
        )
    # Issue comments have no status on GitHub; nothing to do.
    return {"id": thread_id, "status": status}


def _add_comment(number: int, thread_id: int, content: str, token: str) -> dict[str, Any]:
    kind, _node = _kind(number, thread_id, token)
    if kind == "review":
        created = _rest("POST", f"/pulls/{number}/comments/{thread_id}/replies", token, {"body": content})
        return {"id": created.get("id"), "content": content}
    existing = _rest("GET", f"/issues/comments/{thread_id}", token)
    if common.ticket_path_from_content(existing.get("body") or ""):
        # Never overwrite a ticket comment (it carries the file marker); add a new comment instead.
        created = _rest("POST", f"/issues/{number}/comments", token, {"body": content})
    else:
        # Issue comments have no replies: bot threads keep one comment, updated in place.
        created = _rest("PATCH", f"/issues/comments/{thread_id}", token, {"body": content})
    return {"id": created.get("id"), "content": content}


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------
def _dispatch(body: dict[str, Any], token: str) -> dict[str, Any]:
    workflow = str((body.get("definition") or {}).get("id", "") or "").strip()
    if not workflow:
        raise RuntimeError("forge_compat: restore workflow (definition id) is empty.")
    inputs = {
        key: (str(value).lower() if isinstance(value, bool) else str(value))
        for key, value in (body.get("templateParameters") or {}).items()
    }
    ref = _branch(body.get("sourceBranch") or "main")
    _rest("POST", f"/actions/workflows/{urllib.parse.quote(workflow)}/dispatches", token, {"ref": ref, "inputs": inputs})
    server = os.environ.get("GITHUB_SERVER_URL", "").rstrip("/")
    return {"id": None, "_links": {"web": {"href": f"{server}/{_repo()}/actions/workflows/{workflow}"}}}


def _tree_entries(sha: str, token: str) -> list[dict[str, Any]]:
    quoted = urllib.parse.quote(sha)
    if not _is_gitea():
        tree = _rest("GET", f"/git/trees/{quoted}?recursive=1", token)
        if tree.get("truncated"):
            raise RuntimeError("GitHub truncated the recursive tree listing; tree comparison unavailable.")
        return tree.get("tree", [])
    entries: list[dict[str, Any]] = []
    for page in range(1, 101):  # Gitea pages recursive trees
        tree = _rest("GET", f"/git/trees/{quoted}?recursive=true&per_page=1000&page={page}", token)
        batch = tree.get("tree") or []
        entries.extend(batch)
        if not batch or not tree.get("truncated"):
            break
    return entries


# ---------------------------------------------------------------------------
# Router: (method, path pattern relative to /_apis/, handler(match, query, body, token))
# ---------------------------------------------------------------------------
def _commit_ids(sha: str, token: str) -> dict[str, Any]:
    commit = _rest("GET", f"/git/commits/{urllib.parse.quote(sha)}", token)
    tree = commit.get("tree") or (commit.get("commit") or {}).get("tree") or {}  # GitHub | Gitea shape
    return {"commitId": commit.get("sha", ""), "treeId": tree.get("sha", "")}


def _tree_listing(sha: str, token: str) -> dict[str, Any]:
    return {
        "treeEntries": [
            {"relativePath": e.get("path", ""), "gitObjectType": e.get("type", ""), "objectId": e.get("sha", "")}
            for e in _tree_entries(sha, token)
        ]
    }


def _workflow_definition(workflow_id: str, token: str) -> dict[str, Any]:
    workflow = _rest("GET", f"/actions/workflows/{urllib.parse.quote(workflow_id)}", token)
    return {"id": workflow.get("id"), "name": workflow.get("name", "")}


def _threads_response(number: int, token: str) -> dict[str, Any]:
    threads = _list_threads(number, token)
    return {"value": threads, "count": len(threads)}


_REPO = r"git/repositories/[^/]+"
_ROUTES: list[tuple[str, re.Pattern[str], Any]] = [
    (method, re.compile(pattern + "$", re.IGNORECASE), handler)
    for method, pattern, handler in (
        ("GET", rf"{_REPO}/pullrequests", lambda m, q, b, t: {"value": _list_prs(q, t)}),
        ("POST", rf"{_REPO}/pullrequests", lambda m, q, b, t: _create_pr(b, t)),
        ("GET", rf"{_REPO}/pullrequests/(\d+)", lambda m, q, b, t: _get_pr(int(m[1]), t)),
        ("PATCH", rf"{_REPO}/pullrequests/(\d+)", lambda m, q, b, t: _update_pr(int(m[1]), b, t)),
        ("GET", rf"{_REPO}/pullrequests/(\d+)/threads", lambda m, q, b, t: _threads_response(int(m[1]), t)),
        ("POST", rf"{_REPO}/pullrequests/(\d+)/threads", lambda m, q, b, t: _create_thread(int(m[1]), b, t)),
        (
            "PATCH",
            rf"{_REPO}/pullrequests/(\d+)/threads/(\d+)",
            lambda m, q, b, t: _set_thread_status(int(m[1]), int(m[2]), b.get("status"), t),
        ),
        (
            "POST",
            rf"{_REPO}/pullrequests/(\d+)/threads/(\d+)/comments",
            lambda m, q, b, t: _add_comment(int(m[1]), int(m[2]), str(b.get("content", "") or ""), t),
        ),
        ("GET", rf"{_REPO}/commits/([^/]+)", lambda m, q, b, t: _commit_ids(m[1], t)),
        ("GET", rf"{_REPO}/trees/([^/]+)", lambda m, q, b, t: _tree_listing(m[1], t)),
        ("POST", r"build/builds", lambda m, q, b, t: _dispatch(b, t)),
        ("GET", r"build/definitions/([^/]+)", lambda m, q, b, t: _workflow_definition(m[1], t)),
    )
]


def request_json(
    url: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    token: str | None = None,
) -> Any:
    token = _token(headers, token)
    method = method.upper()
    split = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(split.query))
    path = split.path.split("/_apis/", 1)[1].strip("/")
    for route_method, pattern, handler in _ROUTES:
        match = pattern.match(path)
        if route_method == method and match:
            return handler(match, query, body or {}, token)
    raise RuntimeError(f"forge_compat: unsupported Azure DevOps call {method} /_apis/{path}")
