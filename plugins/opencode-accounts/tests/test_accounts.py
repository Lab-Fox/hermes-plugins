"""Behaviour tests for the OpenCode account store and commands.

Run from the plugin directory: ``python -m unittest discover -s tests -v``.
Hermes itself is not imported: its seams (home, .env, pool) are stubbed, so
nothing here can touch a real Hermes home.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from importlib import import_module
from pathlib import Path

PLUGINS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGINS / "model-providers" / "opencode-accounts"))
sys.path.insert(0, str(PLUGINS))

import opencode_accounts as store  # noqa: E402


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env: dict[str, str] = {}
        self.pool_drops = 0
        self._patch("_home", lambda: Path(self.tmp.name))
        self._patch("read_secret", lambda name: self.env.get(name, ""))
        self._patch("_write_secret", self.env.__setitem__)
        self._patch("_delete_secret", lambda name: self.env.pop(name, None))
        self._patch("_drop_pool_rows", self._drop)
        store._cache.clear()

    def _patch(self, name, value):
        original = getattr(store, name)
        setattr(store, name, value)
        self.addCleanup(setattr, store, name, original)

    def _drop(self):
        self.pool_drops += 1
        return 0

    def test_first_account_is_active_and_switching_is_explicit(self):
        store.add_account("Personal", "oc-personal-key")
        store.add_account("work", "oc-work-key")
        self.assertEqual(store.active_account().label, "personal")
        self.assertEqual(self.env["OPENCODE_API_KEY_PERSONAL"], "oc-personal-key")
        self.assertNotIn("oc-personal-key", store.store_path().read_text(encoding="utf-8"))
        store.use_account("work")
        self.assertEqual(store.active_account().key, "oc-work-key")

    def test_adopts_builtin_env_keys_once_and_empties_the_pools(self):
        self.env.update({"OPENCODE_ZEN_API_KEY": "oc-same", "OPENCODE_GO_API_KEY": "oc-same"})
        adopted = store.adopt_existing_keys()
        self.assertEqual([(a.label, a.env_var) for a in adopted], [("main", "OPENCODE_ZEN_API_KEY")])
        self.assertEqual(self.pool_drops, 1)
        self.assertEqual(store.adopt_existing_keys(), [])

    def test_remove_cleans_up_own_secret_and_moves_active(self):
        store.add_account("a", "oc-key-a")
        store.add_account("b", "oc-key-b")
        store.remove_account("a")
        self.assertEqual(self.env, {"OPENCODE_API_KEY_B": "oc-key-b"})
        self.assertEqual(store.active_account().label, "b")

    def test_rejects_duplicates_bad_labels_and_unknown_accounts(self):
        store.add_account("a", "oc-key-a")
        for label, key in (("a", "oc-other"), ("b", "oc-key-a"), ("no spaces", "oc-x"), ("c", " ")):
            with self.assertRaises(store.AccountError):
                store.add_account(label, key)
        with self.assertRaises(store.AccountError):
            store.use_account("missing")

    def test_slash_command_switches_and_never_accepts_or_prints_keys(self):
        commands = import_module("opencode-accounts.commands")
        store.add_account("personal", "oc-personal-key")
        store.add_account("work", "oc-work-key")
        self.assertIn("now uses 'work'", commands.slash("use work"))
        listing = commands.slash("")
        self.assertIn("* work", listing)
        self.assertNotIn("oc-work-key", listing)
        self.assertIn("terminal", commands.slash("add third oc-secret"))
        self.assertEqual(len(store.list_accounts()), 2)
        self.assertIn("No OpenCode account named", commands.slash("use nope"))


if __name__ == "__main__":
    unittest.main()
