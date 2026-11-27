import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "host"))
import portable_inventory as inventory


class BusinessInventory(unittest.TestCase):
    def setUp(self):
        self.old = {"schema":1,"defaultModel":"a" * 64,"providers":["b" * 64],"sessions":[
            {"id":"1"*64,"cursor":79,"events":80,"history":"e"*64},
        ]}

    def test_added_routes_and_sessions_preserve_old_history(self):
        new = copy.deepcopy(self.old)
        new["providers"].append("f"*64)
        new["sessions"].append(dict(id="0"*64,cursor=-1,events=0,history="c"*64))
        self.assertEqual(inventory.cuts(self.old), {"1"*64:79})
        inventory.assert_preserved(self.old,new)

    def test_any_missing_old_proof_fails(self):
        for field, value in (("sessions",[]),("providers",[]),("defaultModel","c"*64)):
            with self.subTest(field=field):
                with self.assertRaises(inventory.InventoryError):
                    inventory.assert_preserved(self.old, dict(self.old, **{field:value}))
        new = copy.deepcopy(self.old)
        new["sessions"][0]["history"] = "f"*64
        with self.assertRaises(inventory.InventoryError):
            inventory.assert_preserved(self.old,new)
