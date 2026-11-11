from __future__ import annotations

import base64
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "queue_post_merge_restore.py"


def load_module():
    # Preload common helper so the script can import it.
    common_path = MODULE_PATH.parent / "common.py"
    common_spec = importlib.util.spec_from_file_location("common", common_path)
    if common_spec is not None and common_spec.loader is not None:
        common_mod = importlib.util.module_from_spec(common_spec)
        sys.modules["common"] = common_mod
        common_spec.loader.exec_module(common_mod)

    module_name = "queue_post_merge_restore"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


ALLOWED = {"reviewer@contoso.com", "abc-123"}


def _marker(path: str) -> str:
    encoded = base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")
    return f"Automation marker: AUTO-CHANGE-TICKET:{encoded}"


class QueuePostMergeRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def test_ticket_path_from_content_decodes_marker(self) -> None:
        path = "tenant-state/intune/Device Configurations/macOS - WiFi TEST_macOSWiFiConfiguration__id.json"
        content = f"Header\n{_marker(path)}\nBody"
        self.assertEqual(self.module.common.ticket_path_from_content(content), path)

    def test_rejected_ticket_paths_uses_latest_decision(self) -> None:
        accepted_path = "tenant-state/intune/Settings Catalog/A.json"
        rejected_path = "tenant-state/intune/Settings Catalog/B.json"
        rev = {"uniqueName": "Reviewer@Contoso.com"}
        threads = [
            {
                "comments": [
                    {"id": 1, "parentCommentId": 0, "content": _marker(accepted_path)},
                    {"id": 2, "parentCommentId": 0, "content": "/reject", "author": rev},
                    {"id": 3, "parentCommentId": 0, "content": "/accept", "author": rev},
                ]
            },
            {
                "comments": [
                    {"id": 1, "parentCommentId": 0, "content": _marker(rejected_path)},
                    {"id": 2, "parentCommentId": 0, "content": "/accept", "author": rev},
                    {"id": 3, "parentCommentId": 0, "content": "/reject", "author": rev},
                ]
            },
        ]
        self.assertEqual(self.module._rejected_ticket_paths(threads, ALLOWED), [rejected_path])

    def test_decisions_from_unlisted_authors_are_ignored(self) -> None:
        path = "tenant-state/intune/Settings Catalog/A.json"
        threads = [
            {
                "comments": [
                    {"id": 1, "parentCommentId": 0, "content": _marker(path)},
                    {"id": 2, "parentCommentId": 0, "content": "/reject", "author": {"uniqueName": "intruder@contoso.com"}},
                    {"id": 3, "parentCommentId": 0, "content": "/reject", "author": {"uniqueName": "reviewer@contoso.com"}, "isDeleted": True},
                    {"id": 4, "parentCommentId": 0, "content": "Auto-action: /reject detected.", "author": {"uniqueName": "bot"}},
                ]
            }
        ]
        self.assertEqual(self.module._rejected_ticket_paths(threads, ALLOWED), [])
        self.assertFalse(self.module._has_reject_signal(threads, ALLOWED))
        self.assertFalse(self.module._has_reject_signal(threads, set()))

    def test_prose_is_not_a_decision(self) -> None:
        threads = [{"comments": [{"id": 1, "content": "Reject reasons below, but overall I would accept.",
                                  "author": {"uniqueName": "reviewer@contoso.com"}}]}]
        self.assertFalse(self.module._has_reject_signal(threads, ALLOWED))
        threads[0]["comments"][0]["content"] = "  /reject"
        self.assertTrue(self.module._has_reject_signal(threads, ALLOWED))

    def test_reject_vote_requires_allowed_reviewer(self) -> None:
        common = sys.modules["common"]
        pr = {"reviewers": [{"uniqueName": "intruder@contoso.com", "vote": -10}]}
        self.assertFalse(common.has_allowed_reject_vote(pr, ALLOWED))
        pr["reviewers"].append({"id": "ABC-123", "vote": -10})
        self.assertTrue(common.has_allowed_reject_vote(pr, ALLOWED))

    def test_queue_restore_pipeline_includes_selective_params(self) -> None:
        captured: dict[str, object] = {}

        def _fake_request(url: str, headers: dict[str, str], method: str = "GET", body: dict | None = None):
            captured["url"] = url
            captured["method"] = method
            captured["body"] = body or {}
            return {"id": 123}

        with patch.object(self.module.common, "request_json", side_effect=_fake_request):
            self.module.common.queue_restore_pipeline(
                "https://dev.azure.com/org",
                "proj",
                {"Authorization": "Bearer x"},
                self.module.common.RestoreRequest(
                    definition_id=42,
                    baseline_branch="main",
                    dry_run=False,
                    restore_mode="selective",
                    restore_paths_csv="tenant-state/intune/Device Configurations/macOS - WiFi TEST.json",
                ),
            )

        body = captured["body"]
        self.assertIsInstance(body, dict)
        template = body["templateParameters"]
        self.assertEqual(template["restoreMode"], "selective")
        self.assertIn("restorePathsCsv", template)


if __name__ == "__main__":
    unittest.main()
