from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "restore_normalize_payload.py"
sys.path.insert(0, str(MODULE_PATH.parent))  # script imports its sibling common.py

GUID = "12345678-1234-4234-8234-123456789abc"
GROUP = "#microsoft.graph.groupAssignmentTarget"


def load_module():
    spec = importlib.util.spec_from_file_location("restore_normalize_payload", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


class NormalizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def test_strip_labels_keeps_only_target_ids(self) -> None:
        node = {
            "assignments": [{"target": {"@odata.type": GROUP, "groupId": GUID, "groupDisplayName": "Admins"}, "targetDisplayName": "T"}],
            "groupId": GUID,
            "groupType": "x",
        }
        removed = self.m.strip_assignment_display_labels(node)
        self.assertEqual(removed, 3)  # groupDisplayName, targetDisplayName, groupType
        self.assertEqual(node["assignments"][0]["target"], {"@odata.type": GROUP, "groupId": GUID})
        self.assertNotIn("groupType", node)

    def test_group_target_without_valid_id_is_marked_then_pruned(self) -> None:
        node = {"assignments": [{"target": {"@odata.type": GROUP, "groupId": "bad"}}, {"target": {"@odata.type": GROUP, "groupId": GUID}}]}
        self.m.strip_assignment_display_labels(node)
        self.assertEqual(self.m.prune_invalid_assignment_targets(node), 1)
        self.assertEqual(len(node["assignments"]), 1)
        self.assertEqual(self.m.remove_internal_markers(node), 0)  # prune removed the marked target with its marker

    def test_group_id_is_resolved_by_name(self) -> None:
        node = {"target": {"@odata.type": GROUP, "groupDisplayName": "Admins"}}
        self.m.strip_assignment_display_labels(node, lambda name: GUID if name == "Admins" else None)
        self.assertEqual(node["target"]["groupId"], GUID)
        self.assertNotIn(self.m.DROP_MARKER, node["target"])

    def test_encode_payload_json(self) -> None:
        data = {"payloadJson": {"a": 1}}
        self.assertTrue(self.m.encode_payload_json(data))
        self.assertEqual(json.loads(base64.b64decode(data["payloadJson"])), {"a": 1})
        self.assertFalse(self.m.encode_payload_json({"payloadJson": "already"}))
        self.assertFalse(self.m.encode_payload_json([1]))

    def test_resolver_requires_single_match_and_caches(self) -> None:
        calls = []

        def fake_urlopen(request, timeout):
            calls.append(request.full_url)
            return _Response({"value": [{"id": GUID, "displayName": "Admins"}, {"id": "other", "displayName": "Admins"}]})

        with patch.object(self.m.urllib.request, "urlopen", fake_urlopen):
            resolve = self.m.make_group_resolver("token")
            self.assertEqual(resolve("Admins"), GUID)  # one exact match with a valid GUID
            self.assertEqual(resolve("admins "), GUID)  # same cache key
            self.assertEqual(len(calls), 1)
            self.assertIn("displayName+eq+%27Admins%27", calls[0])
        self.assertIsNone(self.m.make_group_resolver("")("Admins"))  # no token, no lookup

        with patch.object(self.m.urllib.request, "urlopen", lambda *a, **k: _Response({"value": [{"id": GUID, "displayName": "a"}, {"id": GUID.replace("1", "2"), "displayName": "b"}]})):
            self.assertIsNone(self.m.make_group_resolver("token")("c"))  # ambiguous

    def test_main_rewrites_changed_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            changed = root / "changed.json"
            changed.write_text(json.dumps({"payloadJson": {"a": 1}, "assignments": [{"target": {"@odata.type": GROUP, "groupId": "bad"}}]}))
            untouched = root / "untouched.json"
            untouched.write_text('{"displayName":"x"}')
            with patch.dict(os.environ, {"RESTORE_PATH_ENV": td, "GRAPH_TOKEN": ""}):
                self.assertEqual(self.m.main(), 0)
            result = json.loads(changed.read_text())
            self.assertEqual(result["assignments"], [])
            self.assertIsInstance(result["payloadJson"], str)
            self.assertEqual(untouched.read_text(), '{"displayName":"x"}')  # not rewritten

    def test_main_skips_missing_folder(self) -> None:
        with patch.dict(os.environ, {"RESTORE_PATH_ENV": "/nonexistent-restore-folder"}):
            self.assertEqual(self.m.main(), 0)


if __name__ == "__main__":
    unittest.main()
