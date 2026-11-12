"""Behaviour tests for the NanoGPT account store and commands.

Run from the plugin directory: ``python -m unittest discover -s tests -v``.
Hermes itself is not imported: its seams (home, .env, pool, network) are stubbed,
so nothing here can touch a real Hermes home.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PLUGINS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGINS / "model-providers" / "nanogpt"))
sys.path.insert(0, str(PLUGINS))

import nanogpt_accounts as store  # noqa: E402

SUB_USAGE = {"active": True, "state": "active", "routing": {"paidSpendPolicyAllowsBalance": False},
             "weeklyInputTokens": {"used": 25, "remaining": 75}}
PAYGO_USAGE = {"active": False}


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env: dict[str, str] = {}
        self.usage: dict[str, dict] = {}
        self.pool_drops = 0
        self._patch("_home", lambda: Path(self.tmp.name))
        self._patch("read_secret", lambda name: self.env.get(name, ""))
        self._patch("_write_secret", self.env.__setitem__)
        self._patch("_delete_secret", lambda name: self.env.pop(name, None))
        self._patch("subscription_usage", lambda key, timeout=4.0: self.usage.get(key))
        self._patch("balance", lambda key, timeout=4.0: {"usd_balance": "12.5"})
        self._patch("_drop_pool_rows", self._drop)
        store._cache.clear()

    def _patch(self, name, value):
        original = getattr(store, name)
        setattr(store, name, value)
        self.addCleanup(setattr, store, name, original)

    def _drop(self):
        self.pool_drops += 1
        return 0

    def test_first_account_becomes_active_and_key_goes_to_env_only(self):
        self.usage["sk-sub"] = SUB_USAGE
        account = store.add_account("Sub", "sk-sub")
        self.assertEqual((account.label, account.kind, account.active), ("sub", "subscription", True))
        self.assertEqual(self.env["NANOGPT_API_KEY_SUB"], "sk-sub")
        self.assertNotIn("sk-sub", store.store_path().read_text(encoding="utf-8"))

    def test_switching_is_explicit_and_second_account_does_not_take_over(self):
        self.usage.update({"sk-sub": SUB_USAGE, "sk-pay": PAYGO_USAGE})
        store.add_account("sub", "sk-sub")
        store.add_account("paygo", "sk-pay")
        self.assertEqual(store.active_account().label, "sub")
        store.use_account("paygo")
        self.assertEqual((store.active_account().label, store.active_account().key), ("paygo", "sk-pay"))

    def test_adopts_existing_env_keys_without_copying_them_and_empties_the_pool(self):
        self.env.update({"NANOGPT_API_KEY": "sk-sub", "HERMES_CUSTOM_NANOGPT_API_KEY": "sk-pay"})
        self.usage.update({"sk-sub": SUB_USAGE, "sk-pay": PAYGO_USAGE})
        adopted = store.adopt_existing_keys()
        self.assertEqual({a.label: a.env_var for a in adopted},
                         {"sub": "NANOGPT_API_KEY", "paygo": "HERMES_CUSTOM_NANOGPT_API_KEY"})
        self.assertEqual(self.pool_drops, 1)
        self.assertEqual(store.adopt_existing_keys(), [])  # second run adopts nothing new
        self.assertEqual(len(self.env), 2)

    def test_removing_adopted_account_keeps_the_original_env_var(self):
        self.env["NANOGPT_API_KEY"] = "sk-sub"
        store.adopt_existing_keys()
        store.add_account("extra", "sk-extra")
        store.remove_account("extra")
        self.assertNotIn("NANOGPT_API_KEY_EXTRA", self.env)
        store.remove_account(store.list_accounts()[0].label)
        self.assertEqual(self.env, {"NANOGPT_API_KEY": "sk-sub"})

    def test_rejects_duplicates_and_bad_labels(self):
        store.add_account("sub", "sk-sub")
        for label, key in (("sub", "sk-other"), ("other", "sk-sub"), ("bad label!", "sk-x"), ("x", "")):
            with self.assertRaises(store.AccountError):
                store.add_account(label, key)
        with self.assertRaises(store.AccountError):
            store.use_account("missing")

    def test_paygo_key_on_a_subscribed_account_is_recognised(self):
        routed = {"active": True, "routing": {"paidSpendPolicyAllowsBalance": True,
                                              "recommendedMode": "paygo", "subscriptionQuotaAvailable": True}}
        self.assertEqual(store.kind_from_usage(routed), store.KIND_PAYGO)
        routed["routing"]["recommendedMode"] = "subscription"
        self.assertEqual(store.kind_from_usage(routed), store.KIND_MIXED)
        self.assertEqual(store.kind_from_usage(None), store.KIND_UNKNOWN)
        for mode, kind in (("payg_only", store.KIND_PAYGO), ("subscription_only", store.KIND_SUBSCRIPTION),
                           ("both", store.KIND_MIXED)):
            self.assertEqual(store.kind_from_usage({"active": True, "routing": {"billingMode": mode}}), kind)

    def test_subscription_only_keys_list_the_subscription_catalog(self):
        self.assertEqual(store.models_base_url(store.KIND_SUBSCRIPTION), store.SUBSCRIPTION_BASE_URL)
        for kind in (store.KIND_PAYGO, store.KIND_MIXED, store.KIND_UNKNOWN):
            self.assertEqual(store.models_base_url(kind), store.BASE_URL)

    def test_slash_command_switches_and_refuses_to_take_keys_in_chat(self):
        from importlib import import_module

        commands = import_module("nanogpt-accounts.commands")
        self.usage.update({"sk-sub": SUB_USAGE, "sk-pay": PAYGO_USAGE})
        store.add_account("sub", "sk-sub")
        store.add_account("paygo", "sk-pay")
        self.assertIn("now uses 'paygo'", commands.slash("use paygo"))
        self.assertEqual(store.active_account().label, "paygo")
        self.assertIn("* paygo", commands.slash(""))
        self.assertNotIn("sk-pay", commands.slash(""))
        self.assertIn("terminal", commands.slash("add work sk-secret"))
        self.assertEqual(len(store.list_accounts()), 2)
        self.assertIn("No NanoGPT account named", commands.slash("use nope"))
        self.assertIn("$12.50", commands.slash("status"))


if __name__ == "__main__":
    unittest.main()
