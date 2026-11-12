"""Which provider errors mean "this key is spent" (switch now) and which mean "slow down" (wait)."""

import importlib.util
import unittest
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "model-providers" / "free-providers" / "limits.py"
_spec = importlib.util.spec_from_file_location("free_providers_limits", _PATH)
limits = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(limits)


class _Err:
    def __init__(self, retry_after=None):
        self.response = type("R", (), {"headers": {} if retry_after is None else {"retry-after": retry_after}})()


class SpentKeyTest(unittest.TestCase):
    def test_token_harbor_free_allowance_message(self):
        # The exact error from the log of 2026-11-12: Hermes waited 600 s on it.
        message = ("Your free allowance for ':free' models is used up. It comes back on 18 Nov 2026 at 10:42 UTC. "
                   "Your account is not limited: remove ':free' from the model name")
        self.assertTrue(limits.key_is_spent(429, "free_tier_limit_reached", message))

    def test_other_quota_wordings(self):
        for code, message in ((429, "You exceeded your current quota, please check your plan"),
                              (429, "Rate limit reached for model x on tokens per day (TPD): Limit 100000"),
                              (402, "Insufficient credits"), (403, "quota_exhausted"),
                              (429, "Daily limit reached for free models")):
            self.assertTrue(limits.key_is_spent(code, "", message), message)

    def test_a_momentary_throttle_keeps_the_key(self):
        for message in ("Rate limit reached for model x on tokens per minute (TPM). Please try again in 7.66s.",
                        "Too many requests", "Rate limit exceeded: 40 RPM"):
            self.assertFalse(limits.key_is_spent(429, "rate_limit_exceeded", message), message)

    def test_a_long_requested_wait_counts_as_spent_but_never_for_per_minute_limits(self):
        self.assertTrue(limits.key_is_spent(429, "", "Too many requests", retry_after=600))
        self.assertFalse(limits.key_is_spent(429, "", "Too many requests", retry_after=5))
        self.assertFalse(limits.key_is_spent(429, "", "40 requests per minute allowed", retry_after=600))

    def test_other_failures_are_left_to_hermes(self):
        self.assertFalse(limits.key_is_spent(500, "", "quota exceeded"))
        self.assertFalse(limits.key_is_spent(401, "", "invalid api key"))

    def test_retry_after_header(self):
        self.assertEqual(limits.retry_after_seconds(_Err("600")), 600.0)
        self.assertIsNone(limits.retry_after_seconds(_Err()))
        self.assertIsNone(limits.retry_after_seconds(_Err("Wed, 21 Oct 2026 07:28:00 GMT")))
        self.assertIsNone(limits.retry_after_seconds(object()))


if __name__ == "__main__":
    unittest.main()
