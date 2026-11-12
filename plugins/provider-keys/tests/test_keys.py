"""Behaviour tests for the shared provider key store and commands.

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
sys.path.insert(0, str(PLUGINS / "model-providers" / "free-providers"))
sys.path.insert(0, str(PLUGINS))

import provider_keys as store  # noqa: E402


class KeysCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env: dict[str, str] = {}
        self.dropped: list[str] = []
        self._patch("_home", lambda: Path(self.tmp.name))
        self._patch("read_secret", lambda name: self.env.get(name, ""))
        self._patch("_write_secret", self.env.__setitem__)
        self._patch("_delete_secret", lambda name: self.env.pop(name, None))
        self._patch("_drop_pool_rows", lambda provider: self.dropped.append(provider) or 0)
        self._patch("LEGACY_ENV_VARS", {"nvidia": ("NVIDIA_API_KEY",), "openrouter": ("OPENROUTER_API_KEY",)})
        store._cache.clear()

    def _patch(self, name, value):
        original = getattr(store, name)
        setattr(store, name, value)
        self.addCleanup(setattr, store, name, original)

    def test_keys_are_separate_per_provider_and_switching_is_explicit(self):
        store.add_key("nvidia", "free", "nv-free-key")
        store.add_key("nvidia", "paid", "nv-paid-key")
        store.add_key("modelscope", "free", "ms-free-key")
        self.assertEqual(store.active_key("nvidia").label, "free")
        store.use_key("nvidia", "paid")
        self.assertEqual(store.active_key("nvidia").secret, "nv-paid-key")
        self.assertEqual(store.active_key("modelscope").secret, "ms-free-key")
        self.assertEqual(self.env["NVIDIA_API_KEY_PAID"], "nv-paid-key")
        text = store.store_path().read_text(encoding="utf-8")
        self.assertFalse(any(secret in text for secret in self.env.values()))

    def test_untouched_provider_has_no_active_key(self):
        store.add_key("nvidia", "free", "nv-free-key")
        self.assertIsNone(store.active_key("openrouter"))
        self.assertEqual(store.list_keys("openrouter"), [])

    def test_first_key_adopts_the_existing_hermes_key_and_empties_only_that_pool(self):
        self.env["OPENROUTER_API_KEY"] = "or-existing"
        store.add_key("openrouter", "second", "or-second")
        labels = {k.label: k.env_var for k in store.list_keys("openrouter")}
        self.assertEqual(labels, {"main": "OPENROUTER_API_KEY", "second": "OPENROUTER_API_KEY_SECOND"})
        self.assertEqual(store.active_key("openrouter").label, "main")  # existing key stays in charge
        self.assertEqual(set(self.dropped), {"openrouter"})

    def test_mirrored_provider_copies_the_active_key_into_hermes_env_var(self):
        self._patch("ENV_MIRROR", {"openrouter": "OPENROUTER_API_KEY"})
        self.env["OPENROUTER_API_KEY"] = "or-existing"
        store.add_key("openrouter", "free", "or-free")
        self.assertEqual(self.env["OPENROUTER_API_KEY"], "or-existing")  # adopted key still active
        store.use_key("openrouter", "free")
        self.assertEqual(self.env["OPENROUTER_API_KEY"], "or-free")
        store.use_key("openrouter", "main")  # the original key survived being overwritten
        self.assertEqual(self.env["OPENROUTER_API_KEY"], "or-existing")
        store.remove_key("openrouter", "main")
        self.assertEqual(self.env["OPENROUTER_API_KEY"], "or-free")

    def test_remove_keeps_hermes_own_env_var_and_moves_active(self):
        self.env["NVIDIA_API_KEY"] = "nv-existing"
        store.add_key("nvidia", "extra", "nv-extra")
        store.remove_key("nvidia", "main")
        self.assertEqual(self.env["NVIDIA_API_KEY"], "nv-existing")
        self.assertEqual(store.active_key("nvidia").label, "extra")
        store.remove_key("nvidia", "extra")
        self.assertNotIn("NVIDIA_API_KEY_EXTRA", self.env)

    def test_rejects_duplicates_bad_labels_and_unknown_keys(self):
        store.add_key("groq", "a", "gq-a")
        for label, secret in (("a", "gq-b"), ("b", "gq-a"), ("bad label", "gq-c"), ("c", "")):
            with self.assertRaises(store.KeyError_):
                store.add_key("groq", label, secret)
        with self.assertRaises(store.KeyError_):
            store.use_key("groq", "missing")

    def test_cycle_starts_at_the_active_key_and_leaves_out_skipped_and_switched_off(self):
        for label in ("a", "b", "c", "paid"):
            store.add_key("groq", label, f"gq-{label}")
        store.set_skip("groq", "paid", True)
        store.use_key("groq", "b")
        self.assertEqual([k.label for k in store.cycle_order("groq")], ["b", "c", "a"])
        store.use_key("groq", "paid")  # picked by hand: used, though auto-cycling never lands on it
        self.assertEqual([k.label for k in store.cycle_order("groq")], ["paid", "a", "b", "c"])
        store.set_auto_cycle("groq", False)
        self.assertEqual([k.label for k in store.cycle_order("groq")], ["paid"])
        self.assertTrue(store.auto_cycle("cerebras"))  # on unless switched off

    def test_every_change_to_the_cycle_rebuilds_that_providers_pool(self):
        store.add_key("groq", "a", "gq-a")
        store.add_key("groq", "b", "gq-b")
        self.dropped.clear()
        store.use_key("groq", "b")
        store.set_skip("groq", "a", True)
        store.set_auto_cycle("groq", False)
        store.remove_key("groq", "a")
        self.assertEqual(self.dropped, ["groq"] * 4)

    def test_paid_models_stay_hidden_until_asked_for(self):
        commands = import_module("provider-keys.commands")
        store.add_key("orcarouter", "a", "oc-a")
        self.assertFalse(store.show_paid())
        self.assertIn("include paid models", commands.slash("models all"))
        self.assertTrue(store.show_paid())
        self.assertIn("only free models", commands.slash("models free"))
        self.assertFalse(store.show_paid())
        self.assertIn("never changes key by itself", commands.slash("auto all off"))
        self.assertFalse(store.auto_cycle("orcarouter"))

    def test_commands_switch_list_and_never_take_or_print_secrets(self):
        commands = import_module("provider-keys.commands")
        store.add_key("modelscope", "free", "ms-free-key")
        store.add_key("modelscope", "paid", "ms-paid-key")
        self.assertIn("modelscope now starts with key 'paid'", commands.slash("use modelscope paid"))
        listing = commands.slash("")
        self.assertIn("* paid", listing)
        self.assertNotIn("ms-paid-key", listing)
        self.assertIn("terminal", commands.slash("add groq x gq-secret"))
        self.assertEqual(store.list_keys("groq"), [])
        self.assertIn("not a provider with switchable keys", commands.slash("use nosuch x"))
        self.assertEqual(commands.resolve_provider("Token-Harbor"), "tokenharbor")
        providers = commands.slash("providers")
        for name in ("nvidia", "openrouter", "orcarouter", "tokenharbor", "apinex", "beatapi", "modelscope"):
            self.assertIn(name, providers)


if __name__ == "__main__":
    unittest.main()
