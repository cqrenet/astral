from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "infra" / "mcp-server" / "mcp_server.py"


class _FastMCP:
    """Minimal stand-in for mcp.server.fastmcp.FastMCP: records tools and prompts."""

    def __init__(self, *args, **kwargs) -> None:
        self.tools: dict = {}
        self.run = MagicMock()

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return register

    prompt = tool

    def sse_app(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"inner"})

        return app


def _load():
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = _FastMCP
    security = types.ModuleType("mcp.server.transport_security")
    security.TransportSecuritySettings = lambda **kwargs: None
    for name, module in (
        ("mcp", types.ModuleType("mcp")),
        ("mcp.server", types.ModuleType("mcp.server")),
        ("mcp.server.fastmcp", fastmcp),
        ("mcp.server.transport_security", security),
    ):
        sys.modules[name] = module
    with patch.dict(os.environ, {"ASTRAL_REPO_ROOT": "/nonexistent"}):
        spec = importlib.util.spec_from_file_location("mcp_server_under_test", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


m = _load()


def _call_asgi(app, path="/sse", headers=None, scope_type="http"):
    """Drive an ASGI app once; returns (status or None, body, whether the inner app ran)."""
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": scope_type, "path": path, "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()]}
    asyncio.run(app(scope, receive, send))
    start = next((s for s in sent if s["type"] == "http.response.start"), None)
    body = b"".join(s.get("body", b"") for s in sent if s["type"] == "http.response.body")
    return (start or {}).get("status"), body


class AuthMiddlewareTests(unittest.TestCase):
    def _app(self):
        return m._McpAuthMiddleware(m.mcp.sse_app())

    def test_api_key_via_bearer_or_header_is_accepted(self) -> None:
        with patch.object(m, "_api_key", "s3cret"):
            for headers in ({"authorization": "Bearer s3cret"}, {"x-api-key": "s3cret"}, {"Authorization": "bearer s3cret"}):
                with self.subTest(headers=headers):
                    self.assertEqual(_call_asgi(self._app(), headers=headers), (200, b"inner"))

    def test_wrong_missing_or_unconfigured_credentials_get_401(self) -> None:
        with patch.object(m, "_api_key", "s3cret"):
            for headers in ({"authorization": "Bearer nope"}, {}, {"authorization": "Basic s3cret"}):
                with self.subTest(headers=headers):
                    self.assertEqual(_call_asgi(self._app(), headers=headers), (401, b"Unauthorized"))
        with patch.object(m, "_api_key", ""), patch.object(m, "_entra_tenant_id", ""):
            self.assertEqual(_call_asgi(self._app(), headers={"authorization": "Bearer anything"})[0], 401)

    def test_health_and_discovery_paths_and_non_http_scopes_are_exempt(self) -> None:
        with patch.object(m, "_api_key", "s3cret"):
            for path in ("/health", "/", "/.well-known/oauth-authorization-server"):
                self.assertEqual(_call_asgi(self._app(), path=path)[0], 200, path)
            self.assertEqual(_call_asgi(self._app(), path="/sse", scope_type="lifespan")[0], 200)
            self.assertEqual(_call_asgi(self._app(), path="/messages/")[0], 401)  # only exact exempt paths

    def test_entra_token_is_validated_when_configured(self) -> None:
        with patch.object(m, "_api_key", ""), patch.object(m, "_entra_tenant_id", "t"), patch.object(m, "_mcp_client_id", "c"):
            with patch.object(m, "_validate_entra_token", return_value=True) as validate:
                self.assertEqual(_call_asgi(self._app(), headers={"authorization": "Bearer jwt"})[0], 200)
                validate.assert_called_once_with("jwt")
            with patch.object(m, "_validate_entra_token", return_value=False):
                self.assertEqual(_call_asgi(self._app(), headers={"authorization": "Bearer jwt"})[0], 401)


class EntraTokenTests(unittest.TestCase):
    """_validate_entra_token with a stubbed jwt library: the claim rules are what matter."""

    def _validate(self, claims, *, tenant="tenant-1", client="api://client-1", jwks=None, kid="k1"):
        jwt = types.ModuleType("jwt")
        jwt.get_unverified_header = lambda token: {"kid": kid}
        jwt.decode = MagicMock(return_value=claims)
        algorithms = types.ModuleType("jwt.algorithms")
        algorithms.RSAAlgorithm = types.SimpleNamespace(from_jwk=lambda jwk: "pubkey")
        keys = [{"kid": "k1"}] if jwks is None else jwks
        with patch.dict(sys.modules, {"jwt": jwt, "jwt.algorithms": algorithms}), \
                patch.object(m, "_entra_tenant_id", tenant), patch.object(m, "_mcp_client_id", client), \
                patch.object(m, "_get_jwks", return_value=keys):
            return m._validate_entra_token("token"), jwt.decode

    def test_valid_delegated_token_is_accepted_with_both_audience_forms(self) -> None:
        ok, decode = self._validate({"tid": "tenant-1", "iss": "https://login.microsoftonline.com/tenant-1/v2.0", "scp": "user_impersonation other"})
        self.assertTrue(ok)
        self.assertEqual(decode.call_args.kwargs["audience"], ["client-1", "api://client-1"])
        self.assertEqual(decode.call_args.kwargs["algorithms"], ["RS256"])

    def test_wrong_tenant_issuer_or_missing_scope_is_rejected(self) -> None:
        good = {"tid": "tenant-1", "iss": "https://login.microsoftonline.com/tenant-1/v2.0", "scp": "user_impersonation"}
        self.assertFalse(self._validate({**good, "tid": "other"})[0])
        self.assertFalse(self._validate({**good, "iss": "https://login.microsoftonline.com/tenant-1.attacker.example/v2.0"})[0])
        self.assertFalse(self._validate({**good, "scp": "read"})[0])
        self.assertFalse(self._validate({k: v for k, v in good.items() if k != "scp"})[0])  # app-only token, no scp
        self.assertFalse(self._validate({**good, "scp": "user_impersonation_extra"})[0])  # exact scope match only

    def test_unknown_signing_key_is_rejected(self) -> None:
        self.assertFalse(self._validate({"tid": "tenant-1", "iss": "tenant-1", "scp": "user_impersonation"}, jwks=[{"kid": "other"}])[0])

    def test_any_library_error_fails_closed(self) -> None:
        jwt = types.ModuleType("jwt")
        jwt.get_unverified_header = MagicMock(side_effect=ValueError("bad token"))
        algorithms = types.ModuleType("jwt.algorithms")
        algorithms.RSAAlgorithm = types.SimpleNamespace(from_jwk=lambda jwk: None)
        with patch.dict(sys.modules, {"jwt": jwt, "jwt.algorithms": algorithms}):
            self.assertFalse(m._validate_entra_token("garbage"))


class MainTests(unittest.TestCase):
    def _main(self, argv, **auth):
        env = {"_api_key": "", "_entra_tenant_id": "", "_mcp_client_id": "", **auth}
        with patch.object(sys, "argv", ["mcp_server.py", *argv]), \
                patch.multiple(m, **env):
            return m.main()

    def test_sse_refuses_to_start_without_any_auth(self) -> None:
        self.assertEqual(self._main(["--transport", "sse"]), 1)

    def test_stdio_runs_without_auth(self) -> None:
        m.mcp.run.reset_mock()
        self.assertEqual(self._main([]), 0)
        m.mcp.run.assert_called_once_with(transport="stdio")

    def test_describe_auth_reports_the_enforced_modes(self) -> None:
        cases = [
            (("k", "t", "c"), "API key + Entra JWT (tenant=t, client=c)"),
            (("k", "", ""), "API key only."),
            (("", "t", "c"), "Entra JWT only (tenant=t, client=c)"),
            (("", "t", ""), None),  # a tenant without a client id is not usable
            (("", "", ""), None),
        ]
        for (key, tenant, client), expected in cases:
            with self.subTest(expected=expected), patch.multiple(m, _api_key=key, _entra_tenant_id=tenant, _mcp_client_id=client):
                self.assertEqual(m._describe_auth(), expected)

    def test_sse_starts_uvicorn_with_the_built_app_once_auth_is_configured(self) -> None:
        uvicorn = types.ModuleType("uvicorn")
        uvicorn.run = MagicMock()
        with patch.dict(sys.modules, {"uvicorn": uvicorn}), patch.object(m, "_build_sse_app", return_value="APP"):
            self.assertEqual(self._main(["--transport", "sse", "--host", "0.0.0.0", "--port", "9000"], _api_key="k"), 0)
        uvicorn.run.assert_called_once_with("APP", host="0.0.0.0", port=9000)

    def test_claims_authorized_is_a_pure_rule_check(self) -> None:
        with patch.multiple(m, _entra_tenant_id="t"):
            self.assertTrue(m._claims_authorized({"tid": "t", "iss": "https://login.microsoftonline.com/t/v2.0", "scp": "user_impersonation"}))
            self.assertFalse(m._claims_authorized({"tid": "t", "iss": "https://login.microsoftonline.com/t.attacker.example/v2.0", "scp": "user_impersonation"}))
            self.assertFalse(m._claims_authorized({"tid": "t", "iss": "https://login.microsoftonline.com/t/v2.0", "scp": ""}))
            self.assertFalse(m._claims_authorized({"tid": "u", "iss": "https://login.microsoftonline.com/t/v2.0", "scp": "user_impersonation"}))

    def test_oauth_metadata_needs_tenant_and_client(self) -> None:
        with patch.object(m, "_entra_tenant_id", ""), patch.object(m, "_mcp_client_id", "c"):
            self.assertIsNone(m._build_oauth_metadata())
        with patch.object(m, "_entra_tenant_id", "t"), patch.object(m, "_mcp_client_id", "c"):
            meta = m._build_oauth_metadata()
        self.assertEqual(meta["issuer"], "https://login.microsoftonline.com/t/v2.0")
        self.assertIn("api://c/user_impersonation", meta["scopes_supported"])
        self.assertEqual(meta["code_challenge_methods_supported"], ["S256"])


if __name__ == "__main__":
    unittest.main()
