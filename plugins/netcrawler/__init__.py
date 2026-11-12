"""NetCrawler plugin registration.

Registers six tools on the ``netcrawler`` toolset and one bundled skill. All the
engine code lives under ``core/`` and is Hermes-free; this module is the only
place that touches ``ctx``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import tools

logger = logging.getLogger("netcrawler")


def register(ctx) -> None:
    tools.init(ctx)
    for name, schema, handler in tools.TOOLS:
        try:
            ctx.register_tool(name=name, toolset="netcrawler", schema=schema, handler=handler)
        except Exception:
            logger.exception("failed to register tool %s", name)

    skill_md = Path(__file__).parent / "skills" / "netcrawler" / "SKILL.md"
    try:
        if skill_md.exists():
            ctx.register_skill("netcrawler", skill_md)
    except Exception:
        logger.exception("failed to register bundled skill")

    logger.info("NetCrawler registered: %d tools", len(tools.TOOLS))