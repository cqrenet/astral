from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
MODULE_PATH = SCRIPTS / "diagnostics" / "precheck_azure_openai_availability.py"
sys.path.insert(0, str(SCRIPTS))  # sibling modules the precheck shares with the PR summary

ENV = {
    "ASTRAL_SCM": "ado",  # CI variables are printed as ##vso lines, which these tests read back
    "ENABLE_PR_AI_SUMMARY": "true",
    "AZURE_OPENAI_ENDPOINT": "https://res.openai.azure.com/",
    "AZURE_OPENAI_DEPLOYMENT": "gpt-4o",
    "AZURE_OPENAI_API_KEY": "key",
}


def load_module():
    spec = importlib.util.spec_from_file_location("precheck_aoai", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return json.dumps({"choices": []}).encode()


def _http_error(code: int, body: str = "") -> HTTPError:
    return HTTPError("https://x", code, "reason", {}, io.BytesIO(body.encode()))


class PrecheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def _run(self, outcomes, env=None):
        """Run main() with scripted urlopen outcomes; returns (requests, stdout, AOAI_AVAILABLE value)."""
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request)
            outcome = outcomes[len(requests) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return _Response()

        out = io.StringIO()
        with patch.dict(os.environ, {**ENV, **(env or {})}, clear=True), \
                patch.object(self.m, "urlopen", fake_urlopen), contextlib.redirect_stdout(out):
            self.assertEqual(self.m.main(), 0)  # the precheck never fails the pipeline
        text = out.getvalue()
        flag = next((line.rsplit("]", 1)[1] for line in text.splitlines() if "variable=AOAI_AVAILABLE" in line), None)
        return requests, text, flag

    def test_disabled_or_unconfigured_skips_without_any_request(self) -> None:
        for env in ({"ENABLE_PR_AI_SUMMARY": "false"}, {"AZURE_OPENAI_API_KEY": ""}):
            with self.subTest(env=env):
                requests, text, flag = self._run([], env)
                self.assertEqual((requests, flag), ([], "0"))
                self.assertIn("skipped", text)

    def test_success_on_the_deployments_route(self) -> None:
        requests, _, flag = self._run([None])
        self.assertEqual(flag, "1")
        self.assertEqual(len(requests), 1)
        self.assertIn("/openai/deployments/gpt-4o/chat/completions?api-version=", requests[0].full_url)
        self.assertTrue(requests[0].full_url.startswith("https://res.openai.azure.com/openai"))  # endpoint normalized
        body = json.loads(requests[0].data)
        self.assertEqual((body["max_tokens"], body["temperature"]), (16, 0.0))
        self.assertEqual(requests[0].get_header("Api-key"), "key")

    def test_cognitive_services_endpoint_tries_the_v1_route_first(self) -> None:
        requests, _, flag = self._run([None], {"AZURE_OPENAI_ENDPOINT": "https://res.cognitiveservices.azure.com/openai/v1"})
        self.assertEqual(flag, "1")
        self.assertEqual(requests[0].full_url, "https://res.cognitiveservices.azure.com/openai/v1/chat/completions")
        self.assertEqual(json.loads(requests[0].data)["model"], "gpt-4o")

    def test_404_falls_back_to_the_other_route(self) -> None:
        requests, text, flag = self._run([_http_error(404), None])
        self.assertEqual((flag, len(requests)), ("1", 2))
        self.assertTrue(requests[1].full_url.endswith("/openai/v1/chat/completions"))
        self.assertIn("SUCCESS via v1 route", text)

    def test_credentials_and_deployment_errors_stop_with_a_hint(self) -> None:
        for code, hint in ((401, "AZURE_OPENAI_API_KEY"), (403, "AZURE_OPENAI_API_KEY"), (400, "deployment name")):
            with self.subTest(code=code):
                requests, text, flag = self._run([_http_error(code, "nope")])
                self.assertEqual((flag, len(requests)), ("0", 1))
                self.assertIn(hint, text)

    def test_server_error_and_network_errors_stop_without_retrying(self) -> None:
        for outcome in (_http_error(500), URLError("offline"), RuntimeError("boom")):
            with self.subTest(outcome=type(outcome).__name__):
                requests, _, flag = self._run([outcome])
                self.assertEqual((flag, len(requests)), ("0", 1))

    def test_unsupported_token_parameter_is_retried_with_the_alternate(self) -> None:
        requests, _, flag = self._run([_http_error(400, "Unsupported parameter: 'max_tokens' is not supported"), None])
        self.assertEqual((flag, len(requests)), ("1", 2))
        self.assertIn("max_completion_tokens", json.loads(requests[1].data))

    def test_unsupported_temperature_is_retried_without_it(self) -> None:
        requests, _, flag = self._run([_http_error(400, "Unsupported value: 'temperature' does not support 0.0"), None])
        self.assertEqual((flag, len(requests)), ("1", 2))
        self.assertNotIn("temperature", json.loads(requests[1].data))

    def test_every_route_missing_ends_with_guidance(self) -> None:
        requests, text, flag = self._run([_http_error(404), _http_error(404), _http_error(404), _http_error(404)] * 2)
        self.assertEqual(flag, "0")
        self.assertIn("no successful response", text)
        self.assertIn("without /openai path suffix", text)


if __name__ == "__main__":
    unittest.main()
