from __future__ import annotations

import io
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "update_pr_review_summary.py"


def load_module():
    # Preload common helper so the script can import it.
    common_path = MODULE_PATH.parent / "common.py"
    common_spec = importlib.util.spec_from_file_location("common", common_path)
    if common_spec is not None and common_spec.loader is not None:
        common_mod = importlib.util.module_from_spec(common_spec)
        sys.modules["common"] = common_mod
        common_spec.loader.exec_module(common_mod)

    module_name = "update_pr_review_summary"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class UpdatePrReviewSummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def test_semantic_change_ignores_resolution_status_noise(self) -> None:
        old_excerpt = '{"displayName":"App","resolutionStatus":{"owners":{"error":"Temporary failure resolving"}}}'
        new_excerpt = '{"displayName":"App","resolutionStatus":{"owners":{"error":""}}}'
        semantic = self.module._extract_semantic_change(old_excerpt, new_excerpt)
        self.assertEqual(semantic, "No semantic key changes detected")

    def test_existing_change_fingerprint_parses_auto_block(self) -> None:
        description = (
            "Intro text\n\n"
            "<!-- AUTO-REVIEW-SUMMARY:START -->\n"
            "## Automated Review Summary (entra)\n\n"
            "- **Change Fingerprint:** `A1B2c3D4e5F6`\n"
            "<!-- AUTO-REVIEW-SUMMARY:END -->\n"
        )
        fingerprint = self.module._existing_change_fingerprint(description)
        self.assertEqual(fingerprint, "a1b2c3d4e5f6")

    def test_existing_change_fingerprint_returns_empty_when_missing(self) -> None:
        description = "## Automated Review Summary\n- **Change Fingerprint:** `abcdef012345`"
        fingerprint = self.module._existing_change_fingerprint(description)
        self.assertEqual(fingerprint, "")

    def test_existing_summary_version_parses_auto_block(self) -> None:
        description = (
            "Intro text\n\n"
            "<!-- AUTO-REVIEW-SUMMARY:START -->\n"
            "## Automated Review Summary (entra)\n\n"
            "- **Summary Version:** `2026-03-19b`\n"
            "- **Change Fingerprint:** `A1B2c3D4e5F6`\n"
            "<!-- AUTO-REVIEW-SUMMARY:END -->\n"
        )
        version = self.module._existing_summary_version(description)
        self.assertEqual(version, "2026-03-19b")

    def test_existing_summary_version_returns_empty_when_missing(self) -> None:
        description = (
            "<!-- AUTO-REVIEW-SUMMARY:START -->\n"
            "## Automated Review Summary (intune)\n"
            "<!-- AUTO-REVIEW-SUMMARY:END -->\n"
        )
        self.assertEqual(self.module._existing_summary_version(description), "")

    def test_auto_block_body_extracts_marked_content(self) -> None:
        description = (
            "Header\n\n"
            "<!-- AUTO-REVIEW-SUMMARY:START -->\n"
            "Line A\nLine B\n"
            "<!-- AUTO-REVIEW-SUMMARY:END -->\n"
        )
        body = self.module._auto_block_body(description)
        self.assertIn("Line A", body)
        self.assertIn("Line B", body)

    def test_auto_block_body_empty_when_markers_missing(self) -> None:
        self.assertEqual(self.module._auto_block_body("no markers"), "")

    def test_upsert_auto_block_places_summary_before_reviewer_actions(self) -> None:
        description = (
            "Rolling drift PR created by backup pipeline.\n\n"
            "- Source branch: `drift/intune`\n"
            "- Target branch: `main`\n"
            "- Last pipeline run: `1` (BuildId: 1)\n\n"
            "## Reviewer Quick Actions\n\n"
            "### 1) Accept all changes\n"
        )
        block = (
            "<!-- AUTO-REVIEW-SUMMARY:START -->\n"
            "## Automated Review Summary (intune)\n"
            "<!-- AUTO-REVIEW-SUMMARY:END -->"
        )
        updated = self.module._upsert_auto_block(description, block)
        summary_pos = updated.find("## Automated Review Summary")
        actions_pos = updated.find("## Reviewer Quick Actions")
        self.assertGreaterEqual(summary_pos, 0)
        self.assertGreaterEqual(actions_pos, 0)
        self.assertLess(summary_pos, actions_pos)

    def test_upsert_auto_block_repositions_existing_summary_before_reviewer_actions(self) -> None:
        description = (
            "Rolling drift PR created by backup pipeline.\n\n"
            "- Source branch: `drift/intune`\n"
            "- Target branch: `main`\n"
            "- Last pipeline run: `1` (BuildId: 1)\n\n"
            "## Reviewer Quick Actions\n\n"
            "### 1) Accept all changes\n\n"
            "<!-- AUTO-REVIEW-SUMMARY:START -->\n"
            "Old summary\n"
            "<!-- AUTO-REVIEW-SUMMARY:END -->\n"
        )
        block = (
            "<!-- AUTO-REVIEW-SUMMARY:START -->\n"
            "## Automated Review Summary (intune)\n"
            "<!-- AUTO-REVIEW-SUMMARY:END -->"
        )
        updated = self.module._upsert_auto_block(description, block)
        summary_pos = updated.find("## Automated Review Summary")
        actions_pos = updated.find("## Reviewer Quick Actions")
        self.assertLess(summary_pos, actions_pos)
        self.assertEqual(updated.count("<!-- AUTO-REVIEW-SUMMARY:START -->"), 1)

    def test_publish_draft_pr_updates_is_draft_when_delay_enabled(self) -> None:
        calls: list[dict[str, object]] = []

        def request_json(url: str, token: str, method: str = "GET", body: dict[str, object] | None = None):
            calls.append({"url": url, "token": token, "method": method, "body": body or {}})
            return {}

        with patch.dict(os.environ, {"ROLLING_PR_DELAY_REVIEWER_NOTIFICATIONS": "true"}, clear=False):
            with patch.object(self.module, "_request_json", side_effect=request_json):
                published = self.module._publish_draft_pr(
                    repo_api="https://dev.azure.com/org/project/_apis/git/repositories/repo",
                    token="token",
                    pr_id=77,
                    title="PR title",
                    description="PR description",
                    is_draft=True,
                )

        self.assertTrue(published)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "PATCH")
        self.assertEqual(calls[0]["body"]["isDraft"], False)

    def test_publish_draft_pr_skips_when_delay_disabled(self) -> None:
        with patch.dict(os.environ, {"ROLLING_PR_DELAY_REVIEWER_NOTIFICATIONS": "false"}, clear=False):
            with patch.object(self.module, "_request_json") as request_json:
                published = self.module._publish_draft_pr(
                    repo_api="https://dev.azure.com/org/project/_apis/git/repositories/repo",
                    token="token",
                    pr_id=77,
                    title="PR title",
                    description="PR description",
                    is_draft=True,
                )

        self.assertFalse(published)
        request_json.assert_not_called()

    def test_auto_block_contains_ai_fallback_true_for_fallback_marker(self) -> None:
        body = "...\n_AI fallback used: Azure OpenAI unavailable (timeout)_\n..."
        self.assertTrue(self.module._auto_block_contains_ai_fallback(body))

    def test_auto_block_contains_ai_fallback_true_for_unavailable_marker(self) -> None:
        body = "...\n_AI summary unavailable: Azure OpenAI is not configured_\n..."
        self.assertTrue(self.module._auto_block_contains_ai_fallback(body))

    def test_auto_block_contains_ai_fallback_false_for_normal_ai_text(self) -> None:
        body = "### AI Reviewer Narrative\nEverything looks consistent."
        self.assertFalse(self.module._auto_block_contains_ai_fallback(body))

    def test_preferred_aoai_token_param_uses_max_completion_tokens_for_gpt5(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            token_param = self.module._preferred_aoai_token_param("gpt-5.3-chat")
        self.assertEqual(token_param, "max_completion_tokens")

    def test_preferred_aoai_token_param_uses_max_tokens_for_non_gpt5(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            token_param = self.module._preferred_aoai_token_param("gpt-4.1")
        self.assertEqual(token_param, "max_tokens")

    def test_preferred_aoai_token_param_honors_override(self) -> None:
        with patch.dict(os.environ, {"AZURE_OPENAI_TOKEN_PARAM": "max_tokens"}, clear=False):
            token_param = self.module._preferred_aoai_token_param("gpt-5.3-chat")
        self.assertEqual(token_param, "max_tokens")

    def test_preferred_aoai_temperature_omits_for_gpt5(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            temperature = self.module._preferred_aoai_temperature("gpt-5.3-chat")
        self.assertEqual(temperature, None)

    def test_preferred_aoai_temperature_defaults_to_zero_for_non_gpt5(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            temperature = self.module._preferred_aoai_temperature("gpt-4.1")
        self.assertEqual(temperature, 0.0)

    def test_preferred_aoai_temperature_honors_override(self) -> None:
        with patch.dict(os.environ, {"AZURE_OPENAI_TEMPERATURE": "0.7"}, clear=False):
            temperature = self.module._preferred_aoai_temperature("gpt-5.3-chat")
        self.assertEqual(temperature, 0.7)

    def test_reviewer_instruction_requests_infrastructure_vs_admin_distinction(self) -> None:
        instruction = self.module._reviewer_instruction()
        self.assertIn("platform-managed or vendor-driven infrastructure drift", instruction)
        self.assertIn("tenant-admin changes", instruction)
        self.assertIn("mixed or insufficient", instruction)

    def test_minimal_reviewer_instruction_requests_change_source_classification(self) -> None:
        instruction = self.module._minimal_reviewer_instruction()
        self.assertIn("infrastructure/platform-driven", instruction)
        self.assertIn("admin-driven", instruction)
        self.assertIn("mixed/uncertain", instruction)

    def test_compact_ai_narrative_markdown_preserves_all_reviewer_sections(self) -> None:
        text = (
            "Plain-language summary\n"
            + ("Summary text. " * 20)
            + "\n\nOperational impact\n"
            + ("Operational impact text. " * 20)
            + "\n\nRisk assessment rationale\n"
            + ("Risk rationale text. " * 20)
            + "\n\nRecommended reviewer checks\n"
            + "- Check one\n- Check two\n- Check three\n"
            + "\nRollback considerations\n"
            + ("Rollback text. " * 20)
        )
        compact = self.module._compact_ai_narrative_markdown(text, 420)
        self.assertLessEqual(len(compact), 420)
        self.assertIn("#### Plain-Language Summary", compact)
        self.assertIn("#### Operational Impact", compact)
        self.assertIn("#### Risk Assessment Rationale", compact)
        self.assertIn("#### Recommended Reviewer Checks", compact)
        self.assertIn("#### Rollback Considerations", compact)

    def test_extract_ai_text_from_payload_rejects_truncated_response(self) -> None:
        text, error = self.module._extract_ai_text_from_payload(
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "This output was cut off"},
                    }
                ]
            }
        )
        self.assertEqual(text, "")
        self.assertIn("finish_reason=length", error)
        self.assertIn("partial content suppressed", error)

    def test_classify_change_source_marks_enterprise_app_add_as_infrastructure(self) -> None:
        change = self.module.ChangeItem(
            operation="Added",
            path="tenant-state/entra/Enterprise Applications/Microsoft Foo__id.json",
            risk_score=3,
            risk_label="HIGH",
            reason="Security or broad policy area",
            policy_type="identity_security",
            severity="HIGH",
        )
        source = self.module._classify_change_source(change, "New configuration object added")
        self.assertEqual(source["label"], "likely_infrastructure_driven")

    def test_classify_change_source_marks_assignment_change_as_admin(self) -> None:
        change = self.module.ChangeItem(
            operation="Modified",
            path="tenant-state/entra/Conditional Access/CA Policy__id.json",
            risk_score=3,
            risk_label="HIGH",
            reason="Security or broad policy area",
            policy_type="conditional_access",
            severity="HIGH",
        )
        source = self.module._classify_change_source(
            change,
            "assignment scope: likely broader (fewer exclusion targets)",
        )
        self.assertEqual(source["label"], "likely_admin_driven")

    def test_extract_semantic_change_new_app_includes_security_fields(self) -> None:
        new_excerpt = json.dumps(
            {
                "displayName": "Headless",
                "requiredResourceAccess": [
                    {"resourceAppId": "0003", "resourceAccess": [{"id": "abc", "type": "Scope"}]}
                ],
                "appRoles": [{"value": "Admin", "displayName": "Admin"}],
                "passwordCredentials": [{"hint": "abc"}],
                "signInAudience": "AzureADMultipleOrgs",
            }
        )
        semantic = self.module._extract_semantic_change(
            "", new_excerpt, "tenant-state/entra/App Registrations/Headless.json"
        )
        self.assertIn("New configuration object added", semantic)
        self.assertIn("requiredResourceAccess present", semantic)
        self.assertIn("appRoles present", semantic)
        self.assertIn("passwordCredentials present", semantic)
        self.assertIn("signInAudience=AzureADMultipleOrgs", semantic)

    def test_extract_semantic_change_new_app_without_security_fields_is_generic(self) -> None:
        new_excerpt = json.dumps({"displayName": "Headless"})
        semantic = self.module._extract_semantic_change(
            "", new_excerpt, "tenant-state/entra/App Registrations/Headless.json"
        )
        self.assertEqual(semantic, "New configuration object added")

    def test_classify_change_source_app_reg_permission_changes_are_admin_driven(self) -> None:
        change = self.module.ChangeItem(
            operation="Modified",
            path="tenant-state/entra/App Registrations/App.json",
            risk_score=3,
            risk_label="HIGH",
            reason="Security or broad policy area",
            policy_type="identity_security",
            severity="HIGH",
        )
        source = self.module._classify_change_source(change, "requiredResourceAccess present")
        self.assertEqual(source["label"], "likely_admin_driven")

    def test_classify_change_source_enterprise_app_permission_changes_are_admin_driven(self) -> None:
        change = self.module.ChangeItem(
            operation="Modified",
            path="tenant-state/entra/Enterprise Applications/App.json",
            risk_score=3,
            risk_label="HIGH",
            reason="Security or broad policy area",
            policy_type="identity_security",
            severity="HIGH",
        )
        source = self.module._classify_change_source(change, "oauth2PermissionScopes changed")
        self.assertEqual(source["label"], "likely_admin_driven")

    def test_reviewer_instruction_warns_against_downgrading_app_identity_risk(self) -> None:
        instruction = self.module._reviewer_instruction()
        self.assertIn("App Registrations and Enterprise Applications", instruction)
        self.assertIn("do not downgrade risk to LOW", instruction)
        self.assertIn("passwordCredentials", instruction)

    def test_build_change_source_assessment_marks_split_signals_as_mixed(self) -> None:
        assessment = self.module._build_change_source_assessment(
            [
                {
                    "change_source": "likely_admin_driven",
                    "change_source_reasons": ["Assignment/targeting semantics changed"],
                    "change_source_scores": {"admin": 5, "infrastructure": 0},
                },
                {
                    "change_source": "likely_infrastructure_driven",
                    "change_source_reasons": ["Enterprise application inventory often contains platform-managed object churn"],
                    "change_source_scores": {"admin": 0, "infrastructure": 5},
                },
            ]
        )
        self.assertEqual(assessment["dominant_source"], "mixed_or_uncertain")

    def test_call_azure_openai_payload_includes_change_source_assessment(self) -> None:
        class _FakeResponse:
            def __init__(self, payload: dict) -> None:
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                return json.dumps(self._payload).encode("utf-8")

        change = self.module.ChangeItem(
            operation="Modified",
            path="tenant-state/intune/Device Configurations/P1__id.json",
            risk_score=2,
            risk_label="MEDIUM",
            reason="Workload configuration area",
            policy_type="device_configuration",
            severity="MEDIUM",
        )

        env = {
            "ENABLE_PR_AI_SUMMARY": "true",
            "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT": "gpt",
            "AZURE_OPENAI_API_KEY": "key",
            "PR_AI_REQUEST_MAX_ATTEMPTS": "1",
            "PR_AI_REQUEST_TIMEOUT_SECONDS": "10",
        }

        seen_payloads: list[dict] = []

        def _fake_urlopen(request, timeout=0):
            payload = json.loads(request.data.decode("utf-8"))
            seen_payloads.append(payload)
            return _FakeResponse(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": "AI summary ready"},
                        }
                    ]
                }
            )

        with patch.dict(os.environ, env, clear=False), patch.object(
            self.module, "_load_policy_excerpt", return_value="{}"
        ), patch.object(self.module, "urlopen", side_effect=_fake_urlopen):
            content, error = self.module._call_azure_openai(
                changes=[change],
                deterministic_summary="deterministic",
                workload="intune",
                repo_root="/tmp/repo",
                baseline_branch="main",
                drift_branch="drift/intune",
            )

        self.assertEqual(error, None)
        self.assertEqual(content, "AI summary ready")
        self.assertTrue(seen_payloads)
        user_payload = json.loads(seen_payloads[0]["messages"][1]["content"])
        self.assertIn("change_source_assessment", user_payload)
        self.assertEqual(
            user_payload["change_source_assessment"]["dominant_source"],
            "primarily_admin_driven",
        )

    def test_call_azure_openai_retries_with_minimal_prompt_after_truncated_output(self) -> None:
        class _FakeResponse:
            def __init__(self, payload: dict) -> None:
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                return json.dumps(self._payload).encode("utf-8")

        change = self.module.ChangeItem(
            operation="Modified",
            path="tenant-state/intune/Device Configurations/P1__id.json",
            risk_score=3,
            risk_label="HIGH",
            reason="Security or broad policy area",
            policy_type="device_configuration",
            severity="HIGH",
        )

        env = {
            "ENABLE_PR_AI_SUMMARY": "true",
            "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT": "gpt",
            "AZURE_OPENAI_API_KEY": "key",
            "PR_AI_REQUEST_MAX_ATTEMPTS": "1",
            "PR_AI_REQUEST_TIMEOUT_SECONDS": "10",
        }

        seen_payloads: list[dict] = []
        responses = iter(
            [
                {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": "Truncated reviewer text"},
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": "Complete fallback reviewer summary"},
                        }
                    ]
                },
            ]
        )

        def _fake_urlopen(request, timeout=0):
            payload = json.loads(request.data.decode("utf-8"))
            seen_payloads.append(payload)
            return _FakeResponse(next(responses))

        with patch.dict(os.environ, env, clear=False), patch.object(
            self.module, "_load_policy_excerpt", return_value="{}"
        ), patch.object(self.module, "urlopen", side_effect=_fake_urlopen):
            content, error = self.module._call_azure_openai(
                changes=[change],
                deterministic_summary="deterministic",
                workload="intune",
                repo_root="/tmp/repo",
                baseline_branch="main",
                drift_branch="drift/intune",
            )

        self.assertEqual(error, None)
        self.assertEqual(content, "Complete fallback reviewer summary")
        self.assertEqual(len(seen_payloads), 2)
        first_payload = json.loads(seen_payloads[0]["messages"][1]["content"])
        second_payload = json.loads(seen_payloads[1]["messages"][1]["content"])
        self.assertIn("sampled_changes", first_payload)
        self.assertTrue("sampled_changes" in second_payload or "changes" in second_payload)

    def test_build_full_ai_review_thread_content_includes_marker(self) -> None:
        content = self.module._build_full_ai_review_thread_content(
            "intune",
            "Plain-language summary\nEverything looks consistent.",
        )
        self.assertIn("AI reviewer narrative (full)", content)
        self.assertIn("Automation marker: AUTO-AI-REVIEW:intune", content)

    def test_sync_full_ai_review_thread_skips_duplicate_comment(self) -> None:
        existing_content = self.module._build_full_ai_review_thread_content(
            "intune",
            "Plain-language summary\nEverything looks consistent.",
        )
        calls: list[tuple[str, str]] = []

        def request_json(url: str, token: str, method: str = "GET", body: dict[str, object] | None = None):
            calls.append((method, url))
            if method == "GET" and url.endswith("/pullrequests/77/threads?api-version=7.1"):
                return {
                    "value": [
                        {
                            "id": 10,
                            "status": "active",
                            "comments": [{"content": existing_content}],
                        }
                    ]
                }
            raise AssertionError(f"Unexpected request: {method} {url}")

        with patch.object(self.module, "_request_json", side_effect=request_json):
            updated = self.module._sync_full_ai_review_thread(
                repo_api="https://dev.azure.com/org/project/_apis/git/repositories/repo",
                pr_id=77,
                token="token",
                workload="intune",
                ai_summary="Plain-language summary\nEverything looks consistent.",
            )

        self.assertFalse(updated)
        self.assertEqual(calls, [("GET", "https://dev.azure.com/org/project/_apis/git/repositories/repo/pullrequests/77/threads?api-version=7.1")])

    def test_sync_full_ai_review_thread_creates_thread_when_missing(self) -> None:
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def request_json(url: str, token: str, method: str = "GET", body: dict[str, object] | None = None):
            calls.append((method, url, body))
            if method == "GET" and url.endswith("/pullrequests/77/threads?api-version=7.1"):
                return {"value": []}
            if method == "POST" and url.endswith("/pullrequests/77/threads?api-version=7.1"):
                return {"id": 10}
            raise AssertionError(f"Unexpected request: {method} {url}")

        with patch.object(self.module, "_request_json", side_effect=request_json):
            updated = self.module._sync_full_ai_review_thread(
                repo_api="https://dev.azure.com/org/project/_apis/git/repositories/repo",
                pr_id=77,
                token="token",
                workload="intune",
                ai_summary="Plain-language summary\nEverything looks consistent.",
            )

        self.assertTrue(updated)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "GET")
        self.assertEqual(calls[1][0], "POST")
        post_body = calls[1][2] or {}
        self.assertIn("comments", post_body)

    def test_assignment_signature_uses_group_display_name_when_available(self) -> None:
        payload = {
            "assignments": [
                {
                    "source": "direct",
                    "intent": "apply",
                    "target": {
                        "@odata.type": "#microsoft.graph.groupAssignmentTarget",
                        "groupId": "9d7195ed-f42e-4cbe-9659-2c3c9f55cdd9",
                        "groupDisplayName": "Intune_U_TK_Test",
                    },
                }
            ]
        }
        entries = self.module._assignment_entries(payload)
        self.assertEqual(len(entries), 1)
        signature = self.module._assignment_signature(entries[0])
        self.assertIn("group=Intune_U_TK_Test (9d7195ed-f42e-4cbe-9659-2c3c9f55cdd9)", signature)

    def test_has_matching_detected_change_comment_true_when_change_and_risk_match(self) -> None:
        comments = [
            {
                "content": (
                    "Detected change (auto): Modified: assignment targets added: group=Intune_U_TK_Test\n\n"
                    "Risk context: MEDIUM (device_configuration): Workload configuration area"
                )
            }
        ]
        matched = self.module._has_matching_detected_change_comment(
            comments=comments,
            change_summary="Modified: assignment targets added: group=Intune_U_TK_Test",
            risk_summary="MEDIUM (device_configuration): Workload configuration area",
        )
        self.assertTrue(matched)

    def test_has_matching_detected_change_comment_false_for_stale_comment(self) -> None:
        comments = [
            {
                "content": (
                    "Detected change (auto): Modified: assignment targets added: group=9d7195ed-f42e\n\n"
                    "Risk context: MEDIUM (device_configuration): Workload configuration area"
                )
            }
        ]
        matched = self.module._has_matching_detected_change_comment(
            comments=comments,
            change_summary="Modified: assignment targets added: group=Intune_U_TK_Test (9d7195ed-f42e)\n",
            risk_summary="MEDIUM (device_configuration): Workload configuration area",
        )
        self.assertFalse(matched)

    def test_entra_enrichment_only_json_change_true(self) -> None:
        old_excerpt = """
        {
          "id": "obj-1",
          "displayName": "App",
          "requiredResourceAccess": [{"resourceAppId": "00000003-0000-0000-c000-000000000000"}],
          "requiredResourceAccessResolved": [{"resourceDisplayName": "Microsoft Graph"}],
          "resolutionStatus": {"requiredResourceAccess": {"unresolvedPermissionCount": 0}}
        }
        """
        new_excerpt = """
        {
          "id": "obj-1",
          "displayName": "App",
          "requiredResourceAccess": [{"resourceAppId": "00000003-0000-0000-c000-000000000000"}],
          "requiredResourceAccessResolved": [{"resourceDisplayName": "Unresolved"}],
          "resolutionStatus": {"requiredResourceAccess": {"unresolvedPermissionCount": 6}}
        }
        """
        self.assertTrue(self.module._is_entra_enrichment_only_json_change(old_excerpt, new_excerpt))

    def test_entra_enrichment_only_json_change_false_when_config_changes(self) -> None:
        old_excerpt = """
        {
          "displayName": "App",
          "requiredResourceAccess": [{"resourceAppId": "00000003-0000-0000-c000-000000000000"}]
        }
        """
        new_excerpt = """
        {
          "displayName": "App",
          "requiredResourceAccess": [{"resourceAppId": "11111111-0000-0000-c000-000000000000"}]
        }
        """
        self.assertFalse(self.module._is_entra_enrichment_only_json_change(old_excerpt, new_excerpt))

    def test_filter_operational_noise_changes_ignores_entra_enrichment_only_paths(self) -> None:
        change = self.module.ChangeItem(
            operation="Modified",
            path="tenant-state/entra/App Registrations/Test App__id.json",
            risk_score=3,
            risk_label="HIGH",
            reason="Security or broad policy area",
            policy_type="identity_security",
            severity="HIGH",
        )
        excerpts = {
            ("main", change.path): (
                '{"displayName":"App","requiredResourceAccess":[{"resourceAppId":"00000003-0000-0000-c000-000000000000"}],'
                '"requiredResourceAccessResolved":[{"resourceDisplayName":"Microsoft Graph"}],"resolutionStatus":{"x":0}}'
            ),
            ("drift/entra", change.path): (
                '{"displayName":"App","requiredResourceAccess":[{"resourceAppId":"00000003-0000-0000-c000-000000000000"}],'
                '"requiredResourceAccessResolved":[{"resourceDisplayName":"Unresolved"}],"resolutionStatus":{"x":1}}'
            ),
        }

        def _fake_load(repo_root: str, branch: str, path: str, max_chars: int = 0) -> str:
            return excerpts.get((branch, path), "")

        with patch.object(self.module, "_load_policy_excerpt", side_effect=_fake_load):
            filtered, ignored = self.module._filter_operational_noise_changes(
                repo_root="/tmp/repo",
                baseline_branch="main",
                drift_branch="drift/entra",
                workload="entra",
                changes=[change],
            )

        self.assertEqual(ignored, 1)
        self.assertEqual(filtered, [])

    def test_deterministic_summary_includes_operational_ignore_count(self) -> None:
        change = self.module.ChangeItem(
            operation="Modified",
            path="tenant-state/intune/Device Configurations/P1__id.json",
            risk_score=3,
            risk_label="HIGH",
            reason="Security or broad policy area",
            policy_type="device_configuration",
            severity="HIGH",
        )
        summary = self.module._build_deterministic_summary(
            [change],
            drift_branch="drift/intune",
            baseline_branch="main",
            ignored_operational_count=2,
        )
        self.assertIn("Operational-Only Changes Ignored", summary)
        self.assertIn("**2**", summary)

    def test_assignment_scope_when_exclusion_target_removed_is_broader(self) -> None:
        old_payload = {
            "assignments": [
                {
                    "source": "direct",
                    "intent": "apply",
                    "target": {
                        "@odata.type": "#microsoft.graph.groupAssignmentTarget",
                        "groupId": "11111111-1111-1111-1111-111111111111",
                        "groupDisplayName": "CA001_INC",
                    },
                },
                {
                    "source": "direct",
                    "intent": "apply",
                    "target": {
                        "@odata.type": "#microsoft.graph.exclusionGroupAssignmentTarget",
                        "groupId": "22222222-2222-2222-2222-222222222222",
                        "groupDisplayName": "CA002_EXC",
                    },
                },
            ]
        }
        new_payload = {
            "assignments": [
                {
                    "source": "direct",
                    "intent": "apply",
                    "target": {
                        "@odata.type": "#microsoft.graph.groupAssignmentTarget",
                        "groupId": "11111111-1111-1111-1111-111111111111",
                        "groupDisplayName": "CA001_INC",
                    },
                }
            ]
        }
        changes = self.module._describe_assignment_changes(old_payload, new_payload)
        self.assertIn("assignment scope: likely broader (fewer exclusion targets)", changes)

    def test_assignment_scope_when_exclusion_target_added_is_narrower(self) -> None:
        old_payload = {
            "assignments": [
                {
                    "source": "direct",
                    "intent": "apply",
                    "target": {
                        "@odata.type": "#microsoft.graph.groupAssignmentTarget",
                        "groupId": "11111111-1111-1111-1111-111111111111",
                        "groupDisplayName": "CA001_INC",
                    },
                }
            ]
        }
        new_payload = {
            "assignments": [
                {
                    "source": "direct",
                    "intent": "apply",
                    "target": {
                        "@odata.type": "#microsoft.graph.groupAssignmentTarget",
                        "groupId": "11111111-1111-1111-1111-111111111111",
                        "groupDisplayName": "CA001_INC",
                    },
                },
                {
                    "source": "direct",
                    "intent": "apply",
                    "target": {
                        "@odata.type": "#microsoft.graph.exclusionGroupAssignmentTarget",
                        "groupId": "22222222-2222-2222-2222-222222222222",
                        "groupDisplayName": "CA002_EXC",
                    },
                },
            ]
        }
        changes = self.module._describe_assignment_changes(old_payload, new_payload)
        self.assertIn("assignment scope: likely narrower (more exclusion targets)", changes)

    def test_call_azure_openai_retries_timeout_then_succeeds(self) -> None:
        class _FakeResponse:
            def __init__(self, payload: dict) -> None:
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                return json.dumps(self._payload).encode("utf-8")

        change = self.module.ChangeItem(
            operation="Modified",
            path="tenant-state/intune/Device Configurations/P1__id.json",
            risk_score=2,
            risk_label="MEDIUM",
            reason="Workload configuration area",
            policy_type="device_configuration",
            severity="MEDIUM",
        )

        env = {
            "ENABLE_PR_AI_SUMMARY": "true",
            "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT": "gpt",
            "AZURE_OPENAI_API_KEY": "key",
            "PR_AI_REQUEST_MAX_ATTEMPTS": "2",
            "PR_AI_REQUEST_TIMEOUT_SECONDS": "10",
        }

        with patch.dict(os.environ, env, clear=False), patch.object(
            self.module, "_load_policy_excerpt", return_value="{}"
        ), patch.object(
            self.module,
            "urlopen",
            side_effect=[
                TimeoutError("The read operation timed out"),
                TimeoutError("The read operation timed out"),
                _FakeResponse(
                    {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": "AI summary ready"},
                            }
                        ]
                    }
                ),
            ],
        ), patch.object(self.module.time, "sleep", return_value=None):
            content, error = self.module._call_azure_openai(
                changes=[change],
                deterministic_summary="deterministic",
                workload="intune",
                repo_root="/tmp/repo",
                baseline_branch="main",
                drift_branch="drift/intune",
            )

        self.assertEqual(error, None)
        self.assertEqual(content, "AI summary ready")

    def test_call_azure_openai_falls_back_after_timeout_retries(self) -> None:
        change = self.module.ChangeItem(
            operation="Modified",
            path="tenant-state/intune/Device Configurations/P1__id.json",
            risk_score=2,
            risk_label="MEDIUM",
            reason="Workload configuration area",
            policy_type="device_configuration",
            severity="MEDIUM",
        )

        env = {
            "ENABLE_PR_AI_SUMMARY": "true",
            "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT": "gpt",
            "AZURE_OPENAI_API_KEY": "key",
            "PR_AI_REQUEST_MAX_ATTEMPTS": "2",
            "PR_AI_REQUEST_TIMEOUT_SECONDS": "10",
        }

        with patch.dict(os.environ, env, clear=False), patch.object(
            self.module, "_load_policy_excerpt", return_value="{}"
        ), patch.object(
            self.module,
            "urlopen",
            side_effect=[
                TimeoutError("The read operation timed out"),
                TimeoutError("The read operation timed out"),
                TimeoutError("The read operation timed out"),
                TimeoutError("The read operation timed out"),
                TimeoutError("The read operation timed out"),
                TimeoutError("The read operation timed out"),
                TimeoutError("The read operation timed out"),
                TimeoutError("The read operation timed out"),
            ],
        ), patch.object(self.module.time, "sleep", return_value=None):
            content, error = self.module._call_azure_openai(
                changes=[change],
                deterministic_summary="deterministic",
                workload="intune",
                repo_root="/tmp/repo",
                baseline_branch="main",
                drift_branch="drift/intune",
            )

        self.assertEqual(error, None)
        self.assertIsNotNone(content)
        self.assertIn("AI fallback used", content)
        self.assertIn("timed out after 2 attempts", content)

    def test_call_azure_openai_uses_minimal_retry_after_timeout(self) -> None:
        class _FakeResponse:
            def __init__(self, payload: dict) -> None:
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                return json.dumps(self._payload).encode("utf-8")

        change = self.module.ChangeItem(
            operation="Modified",
            path="tenant-state/intune/Device Configurations/P1__id.json",
            risk_score=2,
            risk_label="MEDIUM",
            reason="Workload configuration area",
            policy_type="device_configuration",
            severity="MEDIUM",
        )

        env = {
            "ENABLE_PR_AI_SUMMARY": "true",
            "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT": "gpt",
            "AZURE_OPENAI_API_KEY": "key",
            "PR_AI_REQUEST_MAX_ATTEMPTS": "2",
            "PR_AI_REQUEST_TIMEOUT_SECONDS": "10",
            "PR_AI_MINIMAL_CHANGE_LIMIT": "4",
            "PR_AI_MINIMAL_MAX_TOKENS": "350",
        }

        side_effects = [
            TimeoutError("The read operation timed out"),
            TimeoutError("The read operation timed out"),
            TimeoutError("The read operation timed out"),
            TimeoutError("The read operation timed out"),
            _FakeResponse(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": "Recovered via minimal retry"},
                        }
                    ]
                }
            ),
        ]

        with patch.dict(os.environ, env, clear=False), patch.object(
            self.module, "_load_policy_excerpt", return_value="{}"
        ), patch.object(
            self.module, "urlopen", side_effect=side_effects
        ), patch.object(self.module.time, "sleep", return_value=None):
            content, error = self.module._call_azure_openai(
                changes=[change],
                deterministic_summary="deterministic",
                workload="intune",
                repo_root="/tmp/repo",
                baseline_branch="main",
                drift_branch="drift/intune",
            )

        self.assertEqual(error, None)
        self.assertEqual(content, "Recovered via minimal retry")

    def test_call_azure_openai_switches_token_param_when_unsupported(self) -> None:
        class _FakeResponse:
            def __init__(self, payload: dict) -> None:
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                return json.dumps(self._payload).encode("utf-8")

        change = self.module.ChangeItem(
            operation="Modified",
            path="tenant-state/intune/Device Configurations/P1__id.json",
            risk_score=2,
            risk_label="MEDIUM",
            reason="Workload configuration area",
            policy_type="device_configuration",
            severity="MEDIUM",
        )

        env = {
            "ENABLE_PR_AI_SUMMARY": "true",
            "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT": "gpt-5.3-chat",
            "AZURE_OPENAI_API_KEY": "key",
            "PR_AI_REQUEST_MAX_ATTEMPTS": "1",
            "PR_AI_REQUEST_TIMEOUT_SECONDS": "10",
        }

        def _fake_urlopen(request, timeout=0):
            payload = json.loads(request.data.decode("utf-8"))
            if "max_completion_tokens" in payload:
                return _FakeResponse(
                    {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": "Token param compatibility recovered"},
                            }
                        ]
                    }
                )
            raise HTTPError(
                request.full_url,
                400,
                "Bad Request",
                hdrs=None,
                fp=io.BytesIO(
                    b'{"error":{"message":"Unsupported parameter: \'max_tokens\' is not supported with this model. Use \'max_completion_tokens\' instead."}}'
                ),
            )

        with patch.dict(os.environ, env, clear=False), patch.object(
            self.module, "_load_policy_excerpt", return_value="{}"
        ), patch.object(
            self.module, "urlopen", side_effect=_fake_urlopen
        ), patch.object(self.module.time, "sleep", return_value=None):
            content, error = self.module._call_azure_openai(
                changes=[change],
                deterministic_summary="deterministic",
                workload="intune",
                repo_root="/tmp/repo",
                baseline_branch="main",
                drift_branch="drift/intune",
            )

        self.assertEqual(error, None)
        self.assertEqual(content, "Token param compatibility recovered")

    def test_call_azure_openai_uses_compact_retry_after_timeout(self) -> None:
        class _FakeResponse:
            def __init__(self, payload: dict) -> None:
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                return json.dumps(self._payload).encode("utf-8")

        changes: list = []
        for idx in range(13):
            changes.append(
                self.module.ChangeItem(
                    operation="Modified",
                    path=f"tenant-state/intune/Device Configurations/P{idx}__id.json",
                    risk_score=2,
                    risk_label="MEDIUM",
                    reason="Workload configuration area",
                    policy_type="device_configuration",
                    severity="MEDIUM",
                )
            )

        env = {
            "ENABLE_PR_AI_SUMMARY": "true",
            "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT": "gpt",
            "AZURE_OPENAI_API_KEY": "key",
            "PR_AI_REQUEST_MAX_ATTEMPTS": "2",
            "PR_AI_REQUEST_TIMEOUT_SECONDS": "10",
            "PR_AI_COMPACT_CHANGE_LIMIT": "10",
            "PR_AI_COMPACT_MAX_TOKENS": "400",
        }

        side_effects = [
            TimeoutError("The read operation timed out"),
            TimeoutError("The read operation timed out"),
            TimeoutError("The read operation timed out"),
            TimeoutError("The read operation timed out"),
            _FakeResponse(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": "Recovered via compact retry"},
                        }
                    ]
                }
            ),
        ]

        with patch.dict(os.environ, env, clear=False), patch.object(
            self.module, "_load_policy_excerpt", return_value="{}"
        ), patch.object(
            self.module, "urlopen", side_effect=side_effects
        ), patch.object(self.module.time, "sleep", return_value=None):
            content, error = self.module._call_azure_openai(
                changes=changes,
                deterministic_summary="deterministic",
                workload="intune",
                repo_root="/tmp/repo",
                baseline_branch="main",
                drift_branch="drift/intune",
            )

        self.assertEqual(error, None)
        self.assertEqual(content, "Recovered via compact retry")


if __name__ == "__main__":
    unittest.main()
