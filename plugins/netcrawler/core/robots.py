"""robots.txt parsing and per-host politeness.

Implements the RFC 9309 matching rule people actually expect: the longest
matching rule wins, ``Allow`` beats ``Disallow`` on a tie, ``*`` and ``$`` work,
and ``Crawl-delay`` is honoured when the operator has not disabled robots.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlsplit, urlunsplit


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[str, bool]] = field(default_factory=list)  # (pattern, allow)
    crawl_delay: Optional[float] = None


def _compile(pattern: str) -> re.Pattern[str]:
    pat = re.escape(pattern).replace(r"\*", ".*")
    if pat.endswith(r"\$"):
        pat = pat[:-2] + "$"
    return re.compile("^" + pat)


@dataclass
class RobotsRules:
    """Parsed robots.txt for one host."""

    groups: list[_Group] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    raw: str = ""

    # ---- parsing -----------------------------------------------------------
    @classmethod
    def parse(cls, text: str) -> "RobotsRules":
        rules = cls(raw=text or "")
        current: Optional[_Group] = None
        in_agent_block = False
        for raw_line in (text or "").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key in ("user-agent", "useragent"):
                if current is None or not in_agent_block:
                    current = _Group()
                    rules.groups.append(current)
                    in_agent_block = True
                current.agents.append(value.lower())
                continue
            in_agent_block = False
            if current is None:
                continue
            if key in ("disallow", "allow"):
                if value:
                    current.rules.append((value, key == "allow"))
            elif key in ("crawl-delay", "crawldelay"):
                try:
                    current.crawl_delay = float(value)
                except ValueError:
                    pass
            elif key == "sitemap" and value:
                rules.sitemaps.append(value)
        return rules

    # ---- matching ----------------------------------------------------------
    def _group_for(self, agent: str) -> Optional[_Group]:
        agent = (agent or "").lower()
        best: Optional[_Group] = None
        best_len = -1
        wildcard: Optional[_Group] = None
        for group in self.groups:
            for name in group.agents:
                if name == "*":
                    wildcard = wildcard or group
                elif name and name in agent and len(name) > best_len:
                    best, best_len = group, len(name)
        return best or wildcard

    def allowed(self, url: str, agent: str = "netcrawler") -> bool:
        group = self._group_for(agent)
        if group is None or not group.rules:
            return True
        parts = urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        verdict = True
        best_len = -1
        for pattern, allow in group.rules:
            if _compile(pattern).match(target):
                if len(pattern) > best_len or (len(pattern) == best_len and allow):
                    best_len = len(pattern)
                    verdict = allow
        return verdict

    def crawl_delay(self, agent: str = "netcrawler") -> Optional[float]:
        group = self._group_for(agent)
        return group.crawl_delay if group else None

    def is_empty(self) -> bool:
        return not any(g.rules for g in self.groups)


EMPTY = RobotsRules()


def sitemaps_from_robots(text: str) -> list[str]:
    return RobotsRules.parse(text).sitemaps


def same_origin_url(base: str, path: str) -> str:
    parts = urlsplit(base)
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))