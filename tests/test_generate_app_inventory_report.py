from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate_app_inventory_report.py"
sys.path.insert(0, str(MODULE_PATH.parent))  # script imports its sibling report_utils.py


def load_module():
    spec = importlib.util.spec_from_file_location("generate_app_inventory_report", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AppRowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def test_rows_have_every_column_and_type_specific_values(self) -> None:
        root = Path("/backup")
        reg = self.m.app_registration_row(
            root / "App Registrations" / "a.json",
            {"id": "1", "appId": "a1", "signInAudience": "AzureADMyOrg", "createdDateTime": "2024-01-01"},
            root,
        )
        ent = self.m.enterprise_app_row(
            root / "Enterprise Applications" / "e__2.json",
            {"id": "2", "servicePrincipalType": "Application", "accountEnabled": True},
            root,
        )
        for row in (reg, ent):
            self.assertEqual(list(row), self.m.FIELDNAMES)
        self.assertEqual((reg["AppType"], reg["SignInAudience"], reg["ServicePrincipalType"]), ("AppRegistration", "AzureADMyOrg", ""))
        self.assertEqual((ent["AppType"], ent["ServicePrincipalType"], ent["AccountEnabled"]), ("EnterpriseApplication", "Application", "True"))
        self.assertEqual(ent["DisplayName"], "e__2")  # falls back to the file name
        self.assertEqual(reg["SourceFile"], "App Registrations/a.json")


if __name__ == "__main__":
    unittest.main()
