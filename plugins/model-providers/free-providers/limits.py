"""Tell "this key is out of allowance" apart from "slow down for a moment".

Hermes treats every HTTP 429 as a brief rate limit: it waits (up to ten minutes
when the provider asks) and retries the SAME key once before moving on. That is
right for a per-minute throttle and wrong for a free allowance that returns next
week. Free-tier providers word the second case in their own ways, so the wording
is matched here and reported to Hermes as a billing-type failure, which makes
its credential pool move to the next key at once.
"""

from __future__ import annotations

import re

# Seconds. A provider asking for a longer pause than this is not a momentary throttle.
LONG_WAIT = 60

_QUOTA = re.compile(
    r"free[ _-]?(tier|allowance|quota|credits?|plan)[^.]{0,40}(limit|used up|exhausted|exceeded|reached|depleted)"
    r"|allowance[^.]{0,40}(used up|exhausted|exceeded)"
    r"|quota[^.]{0,30}(exceeded|exhausted|reached|used up)"
    r"|exceeded your[^.]{0,20}quota"
    r"|(daily|weekly|monthly)[ _-](limit|quota|allowance|cap)"
    r"|per[ _-](day|week|month)\b|\b(tpd|rpd)\b"
    r"|out of (credits?|quota|tokens)"
    r"|insufficient[ _-](credits?|balance|quota|funds)"
    r"|credits?[^.]{0,20}(exhausted|depleted|used up|ran out)"
    r"|free_tier_limit|quota_exhausted|insufficient_quota|usage_limit",
    re.IGNORECASE)

# "Try again in 7.6s", "per minute": the key is fine, the pace is not.
_MOMENTARY = re.compile(
    r"per[ _-](minute|second|min|sec)\b|\b(rpm|tpm|rps)\b|try again in \d+(\.\d+)?\s*(ms|milliseconds?|s|sec|seconds?)\b",
    re.IGNORECASE)


def retry_after_seconds(error) -> float | None:
    """The pause the provider asked for in its ``Retry-After`` header, if it gave one in seconds."""
    headers = getattr(getattr(error, "response", None), "headers", None)
    try:
        value = headers.get("retry-after") if headers is not None else None
        return float(value) if value is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def key_is_spent(status_code, error_code: str = "", message: str = "", retry_after: float | None = None) -> bool:
    """True when the failure means this key cannot serve requests for a long while."""
    if status_code not in (402, 403, 429):
        return False
    text = f"{error_code or ''} {message or ''}"
    if _QUOTA.search(text):
        return True
    if status_code != 429 or _MOMENTARY.search(text):
        return False
    return retry_after is not None and retry_after > LONG_WAIT
