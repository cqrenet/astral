#!/usr/bin/env python3
"""Get an app-only Microsoft Graph token on a GitHub/Gitea Actions runner and export it as GRAPH_TOKEN.

GRAPH_AUTH_METHOD selects how:
  managed-identity  Runner on an Azure VM/VMSS: Instance Metadata Service. AZURE_CLIENT_ID picks a
                    user-assigned identity. Any job on that runner can do this, so restrict the
                    runner group to this repository and to the ASTRAL workflows.
  github-oidc       Any runner: the job's GitHub OIDC token is exchanged through an Entra federated
                    credential (AZURE_TENANT_ID, AZURE_CLIENT_ID; job needs `id-token: write`).
                    Entra must be able to fetch <GHES>/_services/token/.well-known/* anonymously.
  client-certificate  Any runner (e.g. Gitea, which has no OIDC): AZURE_CLIENT_CERTIFICATE holds a PEM
                    with the private key and certificate (store it as a secret). Signed with `openssl`.
  client-secret     Any runner: AZURE_CLIENT_SECRET. Prefer client-certificate.

Role checks run before anything uses the token:
  --require "A|B"   at least one of A, B (repeatable; fails the step if missing)
  --warn-missing C  warning only (repeatable)

The token is masked (::add-mask::) before anything else is printed, and is written to GITHUB_ENV.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.parse
import urllib.request
from typing import Any

GRAPH = "https://graph.microsoft.com"


def _http_json(url: str, headers: dict[str, str], data: bytes | None = None) -> dict[str, Any]:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def token_from_managed_identity(client_id: str) -> str:
    query = {"api-version": "2018-02-01", "resource": GRAPH}
    if client_id:
        query["client_id"] = client_id
    url = "http://169.254.169.254/metadata/identity/oauth2/token?" + urllib.parse.urlencode(query)
    return _http_json(url, {"Metadata": "true"})["access_token"]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _token_url(authority: str, tenant_id: str) -> str:
    return f"{authority.rstrip('/')}/{urllib.parse.quote(tenant_id)}/oauth2/v2.0/token"


def _client_credentials(tenant_id: str, client_id: str, authority: str, credential: dict[str, str]) -> str:
    if not tenant_id or not client_id:
        raise SystemExit("AZURE_TENANT_ID and AZURE_CLIENT_ID are required.")
    form = urllib.parse.urlencode(
        {"client_id": client_id, "scope": f"{GRAPH}/.default", "grant_type": "client_credentials", **credential}
    ).encode("ascii")
    url = _token_url(authority, tenant_id)
    return _http_json(url, {"Content-Type": "application/x-www-form-urlencoded"}, form)["access_token"]


def certificate_assertion(pem_path: str, client_id: str, token_url: str, now: int | None = None) -> str:
    """RS256 client assertion (x5t + x5t#S256 thumbprints), signed with the openssl CLI."""
    der = subprocess.run(["openssl", "x509", "-in", pem_path, "-outform", "DER"], capture_output=True, check=True).stdout
    header = {"alg": "RS256", "typ": "JWT", "x5t": _b64url(hashlib.sha1(der, usedforsecurity=False).digest()),
              "x5t#S256": _b64url(hashlib.sha256(der).digest())}
    now = int(time.time()) if now is None else now
    claims = {"aud": token_url, "iss": client_id, "sub": client_id, "jti": str(uuid.uuid4()),
              "nbf": now, "iat": now, "exp": now + 600}
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
    signature = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", pem_path], input=signing_input.encode("ascii"), capture_output=True, check=True
    ).stdout
    return f"{signing_input}.{_b64url(signature)}"


def token_from_client_certificate(tenant_id: str, client_id: str, authority: str, pem_text: str) -> str:
    if "PRIVATE KEY" not in pem_text or "CERTIFICATE" not in pem_text:
        raise SystemExit("AZURE_CLIENT_CERTIFICATE must be a PEM with both the private key and the certificate.")
    with tempfile.TemporaryDirectory() as tmp:
        pem_path = os.path.join(tmp, "client.pem")
        fd = os.open(pem_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(pem_text)
        assertion = certificate_assertion(pem_path, client_id, _token_url(authority, tenant_id))
    return _client_credentials(tenant_id, client_id, authority, {
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion,
    })


def token_from_github_oidc(tenant_id: str, client_id: str, authority: str) -> str:
    request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not request_url or not request_token:
        raise SystemExit("GitHub OIDC is unavailable: grant the job `permissions: id-token: write`.")
    if not tenant_id or not client_id:
        raise SystemExit("github-oidc needs AZURE_TENANT_ID and AZURE_CLIENT_ID.")
    sep = "&" if "?" in request_url else "?"
    assertion = _http_json(
        f"{request_url}{sep}audience={urllib.parse.quote('api://AzureADTokenExchange')}",
        {"Authorization": f"Bearer {request_token}"},
    )["value"]
    return _client_credentials(tenant_id, client_id, authority, {
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion,
    })


def token_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise SystemExit("Invalid Graph access token format.")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))


def missing_roles(roles: list[str], required: list[str]) -> list[str]:
    """Each requirement is 'A|B' (any of). Returns unmet requirements."""
    have = set(roles)
    return [req for req in required if not any(r.strip() in have for r in req.split("|") if r.strip())]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--require", action="append", default=[], help="Required role(s), 'A|B' = one of.")
    parser.add_argument("--warn-missing", action="append", default=[], help="Role that only warns if missing.")
    args = parser.parse_args()

    method = os.environ.get("GRAPH_AUTH_METHOD", "").strip().lower()
    client_id = os.environ.get("AZURE_CLIENT_ID", "").strip()
    tenant_id = os.environ.get("AZURE_TENANT_ID", "").strip()
    authority = os.environ.get("AZURE_AUTHORITY_HOST", "").strip() or "https://login.microsoftonline.com"
    if method == "managed-identity":
        token = token_from_managed_identity(client_id)
    elif method == "github-oidc":
        token = token_from_github_oidc(tenant_id, client_id, authority)
    elif method == "client-certificate":
        token = token_from_client_certificate(tenant_id, client_id, authority, os.environ.get("AZURE_CLIENT_CERTIFICATE", ""))
    elif method == "client-secret":
        secret = os.environ.get("AZURE_CLIENT_SECRET", "")
        if not secret:
            raise SystemExit("AZURE_CLIENT_SECRET is empty.")
        token = _client_credentials(tenant_id, client_id, authority, {"client_secret": secret})
    else:
        raise SystemExit("Set GRAPH_AUTH_METHOD to managed-identity, github-oidc, client-certificate or client-secret.")

    print(f"::add-mask::{token}", flush=True)  # must precede any other output

    claims = token_claims(token)
    roles = sorted(str(r) for r in claims.get("roles", []) or [])
    print(f"Graph token: appid={claims.get('appid')} tid={claims.get('tid')} aud={claims.get('aud')} exp={claims.get('exp')}")
    print(f"Graph token roles: {', '.join(roles) or '(none)'}")

    for role in missing_roles(roles, args.warn_missing):
        print(f"::warning::Graph token does not contain {role}.")
    missing = missing_roles(roles, args.require)
    if missing:
        print(f"::error::Graph token is missing required application permissions: {', '.join(missing)}")
        return 1

    env_file = os.environ.get("GITHUB_ENV")
    if not env_file:
        raise SystemExit("GITHUB_ENV is not set; this script is meant for GitHub/Gitea Actions.")
    with open(env_file, "a", encoding="utf-8") as handle:
        handle.write(f"GRAPH_TOKEN={token}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
