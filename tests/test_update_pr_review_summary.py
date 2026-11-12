from __future__ import annotations

import io
import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "update_pr_review_summary.py"
sys.path.insert(0, str(MODULE_PATH.parent))  # the script imports its pr_* sibling modules

PR_MODULES = ("pr_base", "pr_changes", "pr_semantic", "pr_change_source", "pr_ai", "pr_description", "pr_threads", "pr_review", "pr_i18n")


def _bind_pr_modules(cls) -> None:
    """Expose the pr_* modules as cls.pr_ai etc. so tests patch the module that owns each name."""
    for name in PR_MODULES:
        setattr(cls, name, sys.modules[name])


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
        _bind_pr_modules(cls)

    def test_semantic_change_ignores_resolution_status_noise(self) -> None:
        old_excerpt = '{"displayName":"App","resolutionStatus":{"owners":{"error":"Temporary failure resolving"}}}'
        new_excerpt = '{"displayName":"App","resolutionStatus":{"owners":{"error":""}}}'
        semantic = self.pr_semantic._extract_semantic_change(old_excerpt, new_excerpt)
        self.assertEqual(semantic, "No semantic key changes detected")

    def test_existing_change_fingerprint_parses_auto_block(self) -> None:
        description = (
            "Intro text\n\n"
            "<!-- AUTO-REVIEW-SUMMARY:START -->\n"
            "## Automated Review Summary (entra)\n\n"
            "- **Change Fingerprint:** `A1B2c3D4e5F6`\n"
            "<!-- AUTO-REVIEW-SUMMARY:END -->\n"
        )
        fingerprint = self.pr_description._existing_change_fingerprint(description)
        self.assertEqual(fingerprint, "a1b2c3d4e5f6")

    def test_existing_change_fingerprint_returns_empty_when_missing(self) -> None:
        description = "## Automated Review Summary\n- **Change Fingerprint:** `abcdef012345`"
        fingerprint = self.pr_description._existing_change_fingerprint(description)
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
        version = self.pr_description._existing_summary_version(description)
        self.assertEqual(version, "2026-03-19b")

    def test_existing_summary_version_returns_empty_when_missing(self) -> None:
        description = (
            "<!-- AUTO-REVIEW-SUMMARY:START -->\n"
            "## Automated Review Summary (intune)\n"
            "<!-- AUTO-REVIEW-SUMMARY:END -->\n"
        )
        self.assertEqual(self.pr_description._existing_summary_version(description), "")

    def test_auto_block_body_extracts_marked_content(self) -> None:
        description = (
            "Header\n\n"
            "<!-- AUTO-REVIEW-SUMMARY:START -->\n"
            "Line A\nLine B\n"
            "<!-- AUTO-REVIEW-SUMMARY:END -->\n"
        )
        body = self.pr_description._auto_block_body(description)
        self.assertIn("Line A", body)
        self.assertIn("Line B", body)

    def test_auto_block_body_empty_when_markers_missing(self) -> None:
        self.assertEqual(self.pr_description._auto_block_body("no markers"), "")

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
        updated = self.pr_description._upsert_auto_block(description, block)
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
        updated = self.pr_description._upsert_auto_block(description, block)
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
        self.assertTrue(self.pr_description._auto_block_contains_ai_fallback(body))

    def test_auto_block_contains_ai_fallback_true_for_unavailable_marker(self) -> None:
        body = "...\n_AI summary unavailable: Azure OpenAI is not configured_\n..."
        self.assertTrue(self.pr_description._auto_block_contains_ai_fallback(body))

    def test_auto_block_contains_ai_fallback_false_for_normal_ai_text(self) -> None:
        body = "### AI Reviewer Narrative\nEverything looks consistent."
        self.assertFalse(self.pr_description._auto_block_contains_ai_fallback(body))

    def test_preferred_aoai_token_param_uses_max_completion_tokens_for_gpt5(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            token_param = self.pr_ai._preferred_aoai_token_param("gpt-5.3-chat")
        self.assertEqual(token_param, "max_completion_tokens")

    def test_preferred_aoai_token_param_uses_max_tokens_for_non_gpt5(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            token_param = self.pr_ai._preferred_aoai_token_param("gpt-4.1")
        self.assertEqual(token_param, "max_tokens")

    def test_preferred_aoai_token_param_honors_override(self) -> None:
        with patch.dict(os.environ, {"AZURE_OPENAI_TOKEN_PARAM": "max_tokens"}, clear=False):
            token_param = self.pr_ai._preferred_aoai_token_param("gpt-5.3-chat")
        self.assertEqual(token_param, "max_tokens")

    def test_preferred_aoai_temperature_omits_for_gpt5(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            temperature = self.pr_ai._preferred_aoai_temperature("gpt-5.3-chat")
        self.assertEqual(temperature, None)

    def test_preferred_aoai_temperature_defaults_to_zero_for_non_gpt5(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            temperature = self.pr_ai._preferred_aoai_temperature("gpt-4.1")
        self.assertEqual(temperature, 0.0)

    def test_preferred_aoai_temperature_honors_override(self) -> None:
        with patch.dict(os.environ, {"AZURE_OPENAI_TEMPERATURE": "0.7"}, clear=False):
            temperature = self.pr_ai._preferred_aoai_temperature("gpt-5.3-chat")
        self.assertEqual(temperature, 0.7)

    def test_reviewer_instruction_requests_infrastructure_vs_admin_distinction(self) -> None:
        instruction = self.pr_ai._reviewer_instruction()
        self.assertIn("platform-managed or vendor-driven infrastructure drift", instruction)
        self.assertIn("tenant-admin changes", instruction)
        self.assertIn("mixed or insufficient", instruction)

    def test_minimal_reviewer_instruction_requests_change_source_classification(self) -> None:
        instruction = self.pr_ai._minimal_reviewer_instruction()
        self.assertIn("infrastructure/platform-driven", instruction)
        self.assertIn("admin-driven", instruction)
        self.assertIn("mixed/uncertain", instruction)

    def test_format_ai_narrative_normalizes_headings_bullets_and_blank_runs(self) -> None:
        raw = "Plain language summary:\r\n• first\n\n\n\n  • second  \noperational impact\nkept as is"
        self.assertEqual(
            self.pr_ai._format_ai_narrative_markdown(raw),
            "#### Plain-Language Summary\n- first\n\n- second\n#### Operational Impact\nkept as is",
        )
        self.assertEqual(self.pr_ai._format_ai_narrative_markdown(""), "")

    def test_split_sections_and_budgets(self) -> None:
        formatted = "intro dropped\n#### Operational Impact\nbody a\n#### Rollback Considerations\n"
        sections = self.pr_ai._split_sections(formatted)
        self.assertEqual(sections, [("#### Operational Impact", "body a"), ("#### Rollback Considerations", "")])
        budgets = self.pr_ai._initial_budgets(sections, 100)
        self.assertEqual(budgets, [24, 24])  # every section keeps at least the 24-character minimum
        long_sections = [("#### Operational Impact", "x" * 500), ("#### Rollback Considerations", "y" * 500)]
        big = self.pr_ai._initial_budgets(long_sections, 400)
        self.assertTrue(all(24 <= b <= 500 for b in big) and big[0] > big[1])  # heavier section gets more room
        self.assertEqual(self.pr_ai._split_sections("no headings here"), [])

    def test_compact_narrative_never_exceeds_the_limit_and_keeps_headings(self) -> None:
        text = "\n".join(f"{h}\n" + "x " * 300 for h in ("Plain-language summary", "Operational impact", "Rollback considerations"))
        for limit in (60, 150, 400):
            compact = self.pr_ai._compact_ai_narrative_markdown(text, limit)
            self.assertLessEqual(len(compact), limit)
        compact = self.pr_ai._compact_ai_narrative_markdown(text, 400)
        for heading in ("#### Plain-Language Summary", "#### Operational Impact", "#### Rollback Considerations"):
            self.assertIn(heading, compact)
        self.assertEqual(self.pr_ai._compact_ai_narrative_markdown(text, 0), "")

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
        compact = self.pr_ai._compact_ai_narrative_markdown(text, 420)
        self.assertLessEqual(len(compact), 420)
        self.assertIn("#### Plain-Language Summary", compact)
        self.assertIn("#### Operational Impact", compact)
        self.assertIn("#### Risk Assessment Rationale", compact)
        self.assertIn("#### Recommended Reviewer Checks", compact)
        self.assertIn("#### Rollback Considerations", compact)

    def test_extract_ai_text_handles_content_shapes_and_refusals(self) -> None:
        extract = self.pr_ai._extract_ai_text_from_payload

        def reply(content, finish="stop", **message):
            return {"choices": [{"finish_reason": finish, "message": {"content": content, **message}}]}

        self.assertEqual(extract(reply("  hi ")), ("hi", ""))
        self.assertEqual(extract(reply(["a", {"text": " b "}, {"value": "c"}, {"text": ""}, 5])), ("a\nb\nc", ""))
        self.assertEqual(extract(reply("", refusal="no")), ("", "model refusal: no"))
        self.assertEqual(extract(reply("", finish="")), ("", "empty message content"))
        self.assertEqual(extract(reply("")), ("", "finish_reason=stop"))
        self.assertEqual(extract(reply("partial", finish="length")), ("", "finish_reason=length (partial content suppressed)"))
        self.assertEqual(extract({"choices": []}), ("", "no choices"))

    def test_extract_ai_text_from_payload_rejects_truncated_response(self) -> None:
        text, error = self.pr_ai._extract_ai_text_from_payload(
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
        change = self.pr_changes.ChangeItem(
            operation="Added",
            path="tenant-state/entra/Enterprise Applications/Microsoft Foo__id.json",
            risk_score=3,
            risk_label="HIGH",
            reason="Security or broad policy area",
            policy_type="identity_security",
            severity="HIGH",
        )
        source = self.pr_change_source._classify_change_source(change, "New configuration object added")
        self.assertEqual(source["label"], "likely_infrastructure_driven")

    def test_classify_change_source_marks_assignment_change_as_admin(self) -> None:
        change = self.pr_changes.ChangeItem(
            operation="Modified",
            path="tenant-state/entra/Conditional Access/CA Policy__id.json",
            risk_score=3,
            risk_label="HIGH",
            reason="Security or broad policy area",
            policy_type="conditional_access",
            severity="HIGH",
        )
        source = self.pr_change_source._classify_change_source(
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
        semantic = self.pr_semantic._extract_semantic_change(
            "", new_excerpt, "tenant-state/entra/App Registrations/Headless.json"
        )
        self.assertIn("New configuration object added", semantic)
        self.assertIn("requiredResourceAccess present", semantic)
        self.assertIn("appRoles present", semantic)
        self.assertIn("passwordCredentials present", semantic)
        self.assertIn("signInAudience=AzureADMultipleOrgs", semantic)

    def test_extract_semantic_change_new_app_without_security_fields_is_generic(self) -> None:
        new_excerpt = json.dumps({"displayName": "Headless"})
        semantic = self.pr_semantic._extract_semantic_change(
            "", new_excerpt, "tenant-state/entra/App Registrations/Headless.json"
        )
        self.assertEqual(semantic, "New configuration object added")

    def test_extract_semantic_change_new_intune_policy_includes_assignments_and_settings(self) -> None:
        new_excerpt = json.dumps(
            {
                "name": "CQRE - ASTRAL Test policy",
                "description": "Test policy for ASTRAL deployment.",
                "platforms": "windows10",
                "settingCount": 1,
                "technologies": "mdm",
                "settings": [
                    {
                        "id": "0",
                        "settingInstance": {
                            "@odata.type": "#microsoft.graph.deviceManagementConfigurationChoiceSettingInstance",
                            "settingDefinitionId": "device_vendor_msft_policy_config_games_allowadvancedgamingservices",
                            "choiceSettingValue": {
                                "value": "device_vendor_msft_policy_config_games_allowadvancedgamingservices_0",
                                "children": [],
                            },
                        },
                    }
                ],
                "assignments": [
                    {
                        "source": "direct",
                        "target": {
                            "@odata.type": "#microsoft.graph.allLicensedUsersAssignmentTarget",
                            "deviceAndAppManagementAssignmentFilterId": None,
                            "deviceAndAppManagementAssignmentFilterType": "none",
                        },
                    },
                    {
                        "source": "direct",
                        "target": {
                            "@odata.type": "#microsoft.graph.exclusionGroupAssignmentTarget",
                            "deviceAndAppManagementAssignmentFilterId": None,
                            "deviceAndAppManagementAssignmentFilterType": "none",
                            "groupName": "Intune_U_TK_Test",
                            "groupType": "StaticMembership",
                        },
                    },
                ],
            }
        )
        semantic = self.pr_semantic._extract_semantic_change(
            "", new_excerpt, "tenant-state/intune/Settings Catalog/CQRE - ASTRAL Test policy.json"
        )
        self.assertIn("New configuration object added", semantic)
        self.assertIn("test policy", semantic)
        self.assertIn("assigned to all licensed users, excludes group Intune_U_TK_Test", semantic)
        self.assertIn("1 setting (games_allowadvancedgamingservices)", semantic)

    def test_extract_semantic_change_new_intune_policy_without_signals_is_generic(self) -> None:
        new_excerpt = json.dumps({"name": "Basic Policy", "platforms": "windows10"})
        semantic = self.pr_semantic._extract_semantic_change(
            "", new_excerpt, "tenant-state/intune/Settings Catalog/Basic Policy.json"
        )
        self.assertEqual(semantic, "New configuration object added")

    def test_classify_change_source_app_reg_permission_changes_are_admin_driven(self) -> None:
        change = self.pr_changes.ChangeItem(
            operation="Modified",
            path="tenant-state/entra/App Registrations/App.json",
            risk_score=3,
            risk_label="HIGH",
            reason="Security or broad policy area",
            policy_type="identity_security",
            severity="HIGH",
        )
        source = self.pr_change_source._classify_change_source(change, "requiredResourceAccess present")
        self.assertEqual(source["label"], "likely_admin_driven")

    def test_classify_change_source_enterprise_app_permission_changes_are_admin_driven(self) -> None:
        change = self.pr_changes.ChangeItem(
            operation="Modified",
            path="tenant-state/entra/Enterprise Applications/App.json",
            risk_score=3,
            risk_label="HIGH",
            reason="Security or broad policy area",
            policy_type="identity_security",
            severity="HIGH",
        )
        source = self.pr_change_source._classify_change_source(change, "oauth2PermissionScopes changed")
        self.assertEqual(source["label"], "likely_admin_driven")

    def test_reviewer_instruction_warns_against_downgrading_app_identity_risk(self) -> None:
        instruction = self.pr_ai._reviewer_instruction()
        self.assertIn("App Registrations and Enterprise Applications", instruction)
        self.assertIn("do not downgrade risk to LOW", instruction)
        self.assertIn("passwordCredentials", instruction)

    def test_build_change_source_assessment_marks_split_signals_as_mixed(self) -> None:
        assessment = self.pr_change_source._build_change_source_assessment(
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

    def test_ai_summary_is_opt_in_even_when_azure_openai_is_configured(self) -> None:
        # No request may leave the pipeline unless ENABLE_PR_AI_SUMMARY is explicitly "true".
        env = {"AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com", "AZURE_OPENAI_DEPLOYMENT": "d",
               "AZURE_OPENAI_API_KEY": "k"}
        for flag in (None, "", "false", "$(ENABLE_PR_AI_SUMMARY)"):
            scoped = dict(env) if flag is None else {**env, "ENABLE_PR_AI_SUMMARY": flag}
            with patch.dict(os.environ, scoped, clear=False), \
                    patch.object(self.pr_ai, "urlopen", side_effect=AssertionError("network call")):
                if flag is None:
                    os.environ.pop("ENABLE_PR_AI_SUMMARY", None)
                result = self.pr_review._call_azure_openai(
                    changes=[], deterministic_summary="d", workload="intune",
                    repo_root="/tmp/repo", baseline_branch="main", drift_branch="drift/intune",
                )
            self.assertEqual(result, (None, None), flag)

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

        change = self.pr_changes.ChangeItem(
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
            self.pr_review, "_load_policy_excerpt", return_value="{}"
        ), patch.object(self.pr_ai, "urlopen", side_effect=_fake_urlopen):
            content, error = self.pr_review._call_azure_openai(
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

        change = self.pr_changes.ChangeItem(
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
            self.pr_review, "_load_policy_excerpt", return_value="{}"
        ), patch.object(self.pr_ai, "urlopen", side_effect=_fake_urlopen):
            content, error = self.pr_review._call_azure_openai(
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
        content = self.pr_threads._build_full_ai_review_thread_content(
            "intune",
            "Plain-language summary\nEverything looks consistent.",
        )
        self.assertIn("AI reviewer narrative (full)", content)
        self.assertIn("Automation marker: AUTO-AI-REVIEW:intune", content)

    def test_sync_full_ai_review_thread_skips_duplicate_comment(self) -> None:
        existing_content = self.pr_threads._build_full_ai_review_thread_content(
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

        with patch.object(self.pr_threads, "_request_json", side_effect=request_json):
            updated = self.pr_threads._sync_full_ai_review_thread(
                pr=self.pr_base.PrRef("https://dev.azure.com/org/project/_apis/git/repositories/repo", 77, "token"),
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

        with patch.object(self.pr_threads, "_request_json", side_effect=request_json):
            updated = self.pr_threads._sync_full_ai_review_thread(
                pr=self.pr_base.PrRef("https://dev.azure.com/org/project/_apis/git/repositories/repo", 77, "token"),
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
                        "groupId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                        "groupDisplayName": "TestGroup_Compliance",
                    },
                }
            ]
        }
        entries = self.pr_semantic._assignment_entries(payload)
        self.assertEqual(len(entries), 1)
        signature = self.pr_semantic._assignment_signature(entries[0])
        self.assertIn("group=TestGroup_Compliance (aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee)", signature)

    def test_has_matching_detected_change_comment_true_when_change_and_risk_match(self) -> None:
        comments = [
            {
                "content": (
                    "Detected change (auto): Modified: assignment targets added: group=TestGroup_Compliance\n\n"
                    "Risk context: MEDIUM (device_configuration): Workload configuration area"
                )
            }
        ]
        matched = self.pr_threads._has_matching_detected_change_comment(
            comments=comments,
            change_summary="Modified: assignment targets added: group=TestGroup_Compliance",
            risk_summary="MEDIUM (device_configuration): Workload configuration area",
        )
        self.assertTrue(matched)

    def test_has_matching_detected_change_comment_false_for_stale_comment(self) -> None:
        comments = [
            {
                "content": (
                    "Detected change (auto): Modified: assignment targets added: group=aaaaaaaa-bbbb\n\n"
                    "Risk context: MEDIUM (device_configuration): Workload configuration area"
                )
            }
        ]
        matched = self.pr_threads._has_matching_detected_change_comment(
            comments=comments,
            change_summary="Modified: assignment targets added: group=TestGroup_Compliance (aaaaaaaa-bbbb)\n",
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
        self.assertTrue(self.pr_semantic._is_entra_enrichment_only_json_change(old_excerpt, new_excerpt))

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
        self.assertFalse(self.pr_semantic._is_entra_enrichment_only_json_change(old_excerpt, new_excerpt))

    def test_filter_operational_noise_changes_ignores_entra_enrichment_only_paths(self) -> None:
        change = self.pr_changes.ChangeItem(
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
        change = self.pr_changes.ChangeItem(
            operation="Modified",
            path="tenant-state/intune/Device Configurations/P1__id.json",
            risk_score=3,
            risk_label="HIGH",
            reason="Security or broad policy area",
            policy_type="device_configuration",
            severity="HIGH",
        )
        summary = self.pr_changes._build_deterministic_summary(
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
        changes = self.pr_semantic._describe_assignment_changes(old_payload, new_payload)
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
        changes = self.pr_semantic._describe_assignment_changes(old_payload, new_payload)
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

        change = self.pr_changes.ChangeItem(
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
            self.pr_review, "_load_policy_excerpt", return_value="{}"
        ), patch.object(
            self.pr_ai,
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
        ), patch("time.sleep", return_value=None):
            content, error = self.pr_review._call_azure_openai(
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
        change = self.pr_changes.ChangeItem(
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
            self.pr_review, "_load_policy_excerpt", return_value="{}"
        ), patch.object(
            self.pr_ai,
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
        ), patch("time.sleep", return_value=None):
            content, error = self.pr_review._call_azure_openai(
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

        change = self.pr_changes.ChangeItem(
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
            self.pr_review, "_load_policy_excerpt", return_value="{}"
        ), patch.object(
            self.pr_ai, "urlopen", side_effect=side_effects
        ), patch("time.sleep", return_value=None):
            content, error = self.pr_review._call_azure_openai(
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

        change = self.pr_changes.ChangeItem(
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
            self.pr_review, "_load_policy_excerpt", return_value="{}"
        ), patch.object(
            self.pr_ai, "urlopen", side_effect=_fake_urlopen
        ), patch("time.sleep", return_value=None):
            content, error = self.pr_review._call_azure_openai(
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
                self.pr_changes.ChangeItem(
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
            self.pr_review, "_load_policy_excerpt", return_value="{}"
        ), patch.object(
            self.pr_ai, "urlopen", side_effect=side_effects
        ), patch("time.sleep", return_value=None):
            content, error = self.pr_review._call_azure_openai(
                changes=changes,
                deterministic_summary="deterministic",
                workload="intune",
                repo_root="/tmp/repo",
                baseline_branch="main",
                drift_branch="drift/intune",
            )

        self.assertEqual(error, None)
        self.assertEqual(content, "Recovered via compact retry")

    def test_reviewer_instruction_includes_language_when_non_english(self) -> None:
        text = self.pr_ai._reviewer_instruction(language="fr")
        self.assertIn("Respond entirely in fr.", text)

    def test_reviewer_instruction_omits_language_for_english(self) -> None:
        text = self.pr_ai._reviewer_instruction(language="en")
        self.assertNotIn("Respond entirely in", text)

    def test_reviewer_system_prompt_includes_language_when_non_english(self) -> None:
        text = self.pr_ai._reviewer_system_prompt(language="de")
        self.assertIn("Respond in de.", text)

    def test_reviewer_system_prompt_omits_language_for_english(self) -> None:
        text = self.pr_ai._reviewer_system_prompt(language="en")
        self.assertNotIn("Respond in", text)

    def test_minimal_reviewer_instruction_includes_language_when_non_english(self) -> None:
        text = self.pr_ai._minimal_reviewer_instruction(language="es")
        self.assertIn("Respond entirely in es.", text)

    def test_minimal_reviewer_instruction_omits_language_for_english(self) -> None:
        text = self.pr_ai._minimal_reviewer_instruction(language="en")
        self.assertNotIn("Respond entirely in", text)

    def test_t_returns_english_when_language_is_en(self) -> None:
        self.assertEqual(self.pr_i18n._t("Change Metrics"), "Change Metrics")

    def test_t_translates_known_text_for_german(self) -> None:
        import os
        with patch.dict(os.environ, {"PR_SUMMARY_LANGUAGE": "de"}, clear=False):
            self.assertEqual(self.pr_i18n._t("Change Metrics"), "Änderungsmetriken")

    def test_t_returns_original_for_unknown_text(self) -> None:
        import os
        with patch.dict(os.environ, {"PR_SUMMARY_LANGUAGE": "de"}, clear=False):
            self.assertEqual(self.pr_i18n._t("Totally unknown key"), "Totally unknown key")

    def test_t_ignores_case_on_language_env_var(self) -> None:
        # Default is already set by prior tests or module load; test directly via _TRANSLATIONS lookup
        self.assertEqual(self.pr_i18n._TRANSLATIONS["Operation"]["de"], "Operation")

    def test_deterministic_summary_translates_headers_for_french(self) -> None:
        import os
        with patch.dict(os.environ, {"PR_SUMMARY_LANGUAGE": "fr"}, clear=False):
            summary = self.pr_changes._build_deterministic_summary(
                changes=[],
                drift_branch="drift/test",
                baseline_branch="main",
            )
        self.assertIn("### Métriques de changement", summary)
        self.assertIn("**Périmètre:**", summary)

    def test_deterministic_summary_translates_risk_table_for_polish(self) -> None:
        import os
        item = self.pr_changes.ChangeItem(
            operation="Added",
            path="tenant-state/intune/compliance policies/test.json",
            risk_score=3,
            risk_label="HIGH",
            reason="Security or broad policy area",
            policy_type="compliance_policy",
            severity="HIGH",
        )
        with patch.dict(os.environ, {"PR_SUMMARY_LANGUAGE": "pl"}, clear=False):
            summary = self.pr_changes._build_deterministic_summary(
                changes=[item],
                drift_branch="drift/test",
                baseline_branch="main",
            )
        self.assertIn("| **Poziom ryzyka** | **Liczba** |", summary)
        self.assertIn("| WYSOKIE | 1 |", summary)
        self.assertIn("Najważniejsze elementy ryzyka", summary)


class PublishDescriptionTests(unittest.TestCase):
    """Description size fallback: full -> compact -> ultra."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()
        _bind_pr_modules(cls)

    def _publish(self, patch_side_effect, deterministic="D" * 50, current=""):
        calls: list[str] = []

        def fake_request(url, token, method="GET", body=None):
            calls.append(body["description"])
            effect = patch_side_effect[len(calls) - 1]
            if isinstance(effect, Exception):
                raise effect
            return {}

        with patch.object(self.pr_description, "_request_json", side_effect=fake_request):
            update = self.pr_description._publish_description(
                workload="intune",
                deterministic=deterministic,
                ai_summary=None,
                ai_error="offline",
                current_description=current,
                patch_url="https://x/pr",
                patch_title="t",
                token="tok",
            )
        return update, calls

    def test_full_description_fits(self) -> None:
        update, calls = self._publish([{}])
        self.assertTrue(update.summary_updated)
        self.assertFalse(update.compacted)
        self.assertEqual(len(calls), 1)

    def test_limit_error_falls_back_to_compact(self) -> None:
        update, calls = self._publish([RuntimeError("PATCH failed: description too long"), {}], deterministic="D" * 3000)
        self.assertTrue(update.summary_updated)
        self.assertTrue(update.compacted)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])

    def test_compact_limit_error_falls_back_to_ultra(self) -> None:
        limit = RuntimeError("PATCH failed: description too long")
        update, calls = self._publish([limit, limit, {}], deterministic="D" * 3000)
        self.assertTrue(update.summary_updated)
        self.assertEqual(len(calls), 3)
        self.assertIn(self.pr_description.COMPACT_AI_THREAD_NOTE, calls[2])

    def test_non_limit_error_propagates(self) -> None:
        with self.assertRaises(RuntimeError):
            self._publish([RuntimeError("HTTP 500 boom")])


class EnforceTicketThreadsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()
        _bind_pr_modules(cls)

    def _change(self, path: str):
        return self.pr_changes.ChangeItem(
            operation="Modified", path=path, risk_score=2, risk_label="MEDIUM",
            reason="r", policy_type="device_configuration", severity="MEDIUM",
        )

    def _thread(self, thread_id: int, path: str, status: int = 1, extra: tuple[str, ...] = ()):
        marker = self.pr_threads._ticket_marker_for_path(path)
        comments = [{"id": 1, "content": marker}] + [{"id": 2 + i, "content": c} for i, c in enumerate(extra)]
        return {"id": thread_id, "status": status, "comments": comments}

    def test_creates_closes_and_reopens_threads(self) -> None:
        new_path = "tenant-state/intune/Scripts/new.json"
        again_path = "tenant-state/intune/Scripts/again.json"
        stale_path = "tenant-state/intune/Scripts/stale.json"
        threads = [
            self._thread(10, again_path, status=self.module.common.THREAD_STATUS_FIXED),
            self._thread(11, stale_path),
        ]
        calls = {"created": [], "status": [], "comments": []}
        with patch.object(self.pr_threads, "_request_json", return_value={"value": threads}), patch.object(self.pr_threads, "_create_ticket_thread", side_effect=lambda **kw: calls["created"].append(kw["path"])
        ), patch.object(self.pr_threads, "_set_thread_status", side_effect=lambda pr, thread_id, status: calls["status"].append((thread_id, status))
        ), patch.object(self.pr_threads, "_add_thread_comment", side_effect=lambda **kw: calls["comments"].append(kw["thread_id"])
        ):
            created, closed = self.pr_threads._enforce_change_ticket_threads(
                self.pr_base.PrRef("https://x/repo", 5, "tok"),
                [self._change(new_path), self._change(again_path), self._change("tenant-state/reports/a.md")],
                r"[A-Z]+-\d+",
                {},
            )
        common = self.module.common
        self.assertEqual((created, closed), (1, 1))
        self.assertEqual(calls["created"], [new_path])
        self.assertIn((11, common.THREAD_STATUS_CLOSED), calls["status"])  # stale thread closed
        self.assertIn((10, common.THREAD_STATUS_ACTIVE), calls["status"])  # resolved thread re-opened
        self.assertEqual(calls["comments"].count(10), 2)  # reopen note + latest detected change


class LoadPolicyExcerptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()
        _bind_pr_modules(cls)

    def test_reads_file_from_origin_branch_and_truncates(self) -> None:
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            def git(*args):
                subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=td, check=True, capture_output=True)

            git("init", "-q", "-b", "main")
            Path(td, "a.json").write_text('  {"k": "0123456789"}\n')
            git("add", ".")
            git("commit", "-q", "-m", "x")
            git("update-ref", "refs/remotes/origin/main", "HEAD")
            load = self.pr_base._load_policy_excerpt
            self.assertEqual(load(td, "refs/heads/main", "a.json"), '{"k": "0123456789"}')
            self.assertEqual(load(td, "main", "a.json", max_chars=5), '{"k":...(truncated)')
            self.assertEqual(load(td, "main", "missing.json"), "")
            self.assertEqual(load("/nonexistent-dir", "main", "a.json"), "")


class RefreshSummaryFlowTests(unittest.TestCase):
    """Orchestration of main(): which collaborators run, in which order, for each outcome."""

    ENV = {
        "SYSTEM_ACCESSTOKEN": "tok", "SYSTEM_COLLECTIONURI": "https://dev.azure.com/org/", "SYSTEM_TEAMPROJECT": "proj",
        "BUILD_REPOSITORY_ID": "repo", "REQUIRE_CHANGE_TICKETS": "",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()
        _bind_pr_modules(cls)

    def _run(self, *, pr=None, current=False, require_gate=False, fail_ai=False):
        m = self.module
        calls: list[str] = []
        pr = {"pullRequestId": 7, "title": "T"} if pr is None else pr
        full_pr = {"description": "old", "isDraft": True, "title": "T"}

        def rec(name, result=None):
            def fn(*a, **k):
                calls.append(name)
                if name == "ai" and fail_ai:
                    raise RuntimeError("boom")
                return result
            return fn

        update = self.pr_description.DescriptionUpdate(True, "new desc", False, False)
        env = dict(self.ENV, REQUIRE_CHANGE_TICKETS="true" if require_gate else "")
        argv = ["x", "--repo-root", "/r", "--workload", "intune", "--backup-folder", "ts",
                "--reports-subdir", "reports", "--drift-branch", "drift/intune", "--baseline-branch", "main"]
        with patch.dict(os.environ, env), patch.object(sys, "argv", argv), \
                patch.object(m, "_find_active_rolling_pr", rec("find", pr or None)), \
                patch.object(m, "_fetch_drift_branch", rec("fetch", True)), \
                patch.object(m, "_collect_changes", rec("collect", ([], 0))), \
                patch.object(m, "_request_json", rec("get_pr", full_pr)), \
                patch.object(m, "_summary_is_current", rec("current?", current)), \
                patch.object(m, "_call_azure_openai", rec("ai", ("ai text", None))), \
                patch.object(m, "_publish_description", rec("description", update)), \
                patch.object(m, "_sync_summary_threads", rec("threads")), \
                patch.object(m, "_publish_draft_pr", rec("draft", True)), \
                patch.object(m._Run, "run_ticket_gate", rec("gate")):
            code = m.main()
        return code, calls

    def test_no_active_pr_does_nothing(self) -> None:
        code, calls = self._run(pr={})
        self.assertEqual((code, calls), (0, ["find"]))

    def test_up_to_date_summary_skips_ai_and_description(self) -> None:
        code, calls = self._run(current=True)
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["find", "fetch", "collect", "get_pr", "current?", "draft", "gate"])

    def test_changed_summary_runs_the_full_pipeline_in_order(self) -> None:
        code, calls = self._run(current=False)
        self.assertEqual(code, 0)
        self.assertEqual(
            calls, ["find", "fetch", "collect", "get_pr", "current?", "ai", "description", "threads", "draft", "gate"]
        )

    def test_ticket_gate_off_logs_and_does_not_touch_threads(self) -> None:
        run = self.module._Run("intune", "/r", "https://repo", "tok", 7, "main", "drift/intune", [], "T", False, False)
        with patch.object(self.module, "_enforce_change_ticket_threads") as enforce:
            run.run_ticket_gate()
        enforce.assert_not_called()

    def test_ticket_gate_on_enforces_threads_for_the_changes(self) -> None:
        change = self.pr_changes.ChangeItem("Modified", "tenant-state/intune/Scripts/a.json", 2, "MEDIUM", "r", "script", "MEDIUM")
        run = self.module._Run("intune", "/r", "https://repo", "tok", 7, "main", "drift/intune", [change], "T", False, True)
        with patch.object(self.module, "_build_ticket_change_context", return_value={"p": ("c", "r")}), \
                patch.object(self.module, "_enforce_change_ticket_threads", return_value=(1, 0)) as enforce:
            run.run_ticket_gate()
        kwargs = enforce.call_args.kwargs
        self.assertEqual((kwargs["pr"].pr_id, kwargs["changes"], kwargs["change_context"]), (7, [change], {"p": ("c", "r")}))
        self.assertEqual(kwargs["ticket_pattern"], r"[A-Z][A-Z0-9]+-\d+")

    def test_failure_is_non_fatal_unless_ticket_gate_is_required(self) -> None:
        self.assertEqual(self._run(fail_ai=True)[0], 0)
        self.assertEqual(self._run(fail_ai=True, require_gate=True)[0], 1)


class _FakeUrlResponse:
    def __init__(self, payload) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


class AzureOpenAIRouteTests(unittest.TestCase):
    """_AzureOpenAIClient._try_route: how each failure maps to retry, next route, or a signal."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()
        _bind_pr_modules(cls)

    def _client(self, attempts=3):
        return self.pr_ai._AzureOpenAIClient("https://x.openai.azure.com", "gpt", "key", "2024", attempts)

    def _http_error(self, code, body=""):
        import io

        return HTTPError("https://x", code, "reason", {}, io.BytesIO(body.encode()))

    def _try(self, outcomes, *, attempts=3, token_param="max_tokens", temperature=0.2):
        """Run _try_route with scripted urlopen outcomes; returns (result, number of calls, sleeps)."""
        calls, sleeps = [], []

        def fake_urlopen(request, timeout):
            calls.append(request)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return _FakeUrlResponse(outcome)

        client = self._client(attempts)
        route = {"name": "deployments", "url": "https://x/y", "body": {}}
        with patch.object(self.pr_ai, "urlopen", fake_urlopen), patch("time.sleep", sleeps.append):
            result = client._try_route(route, 10, token_param, temperature)
        return result, len(calls), sleeps

    def test_success_returns_content(self) -> None:
        ok = {"choices": [{"finish_reason": "stop", "message": {"content": "hello"}}]}
        self.assertEqual(self._try([ok])[0], ("hello", "", None))

    def test_response_without_text_is_an_error_not_a_retry(self) -> None:
        empty = {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}
        (content, error, signal), calls, _ = self._try([empty])
        self.assertEqual((content, signal, calls), ("", None, 1))
        self.assertTrue(error.startswith("deployments:"))

    def test_unsupported_token_param_and_temperature_are_signalled(self) -> None:
        err = self._http_error(400, "Unsupported parameter: 'max_tokens' is not supported")
        self.assertEqual(self._try([err])[0][2], "token_param")
        err = self._http_error(400, "Unsupported value: 'temperature' does not support 0.2")
        self.assertEqual(self._try([err])[0][2], "temperature")
        # No temperature was sent, so a temperature complaint is not a signal.
        err = self._http_error(400, "Unsupported value: 'temperature' does not support 0.2")
        self.assertIsNone(self._try([err], temperature=None)[0][2])

    def test_auth_errors_stop_and_404_moves_on(self) -> None:
        for code in (401, 403):
            (_, error, signal), calls, _ = self._try([self._http_error(code, "denied")])
            self.assertEqual((signal, calls), ("stop", 1))
            self.assertIn("denied", error)
        (_, _, signal), calls, _ = self._try([self._http_error(404)])
        self.assertEqual((signal, calls), (None, 1))

    def test_transient_http_errors_retry_with_backoff_then_succeed(self) -> None:
        ok = {"choices": [{"finish_reason": "stop", "message": {"content": "done"}}]}
        result, calls, sleeps = self._try([self._http_error(503), self._http_error(429), ok])
        self.assertEqual((result, calls, sleeps), (("done", "", None), 3, [1, 2]))

    def test_retries_are_bounded(self) -> None:
        (content, error, signal), calls, sleeps = self._try([self._http_error(503, "busy"), self._http_error(503, "busy")], attempts=2)
        self.assertEqual((content, signal, calls, len(sleeps)), ("", None, 2, 1))
        self.assertIn("busy", error)

    def test_timeouts_retry_then_report_attempt_count(self) -> None:
        (content, error, signal), calls, _ = self._try([TimeoutError("timed out")] * 3, attempts=3)
        self.assertEqual((content, signal, calls), ("", None, 3))
        self.assertIn("timed out after 3 attempts", error)

    def test_other_network_errors_are_reported_without_retry(self) -> None:
        (_, error, signal), calls, _ = self._try([ConnectionError("refused")])
        self.assertEqual((signal, calls), (None, 1))
        self.assertIn("refused", error)


if __name__ == "__main__":
    unittest.main()
