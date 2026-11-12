"""OpenCode Accounts: ``hermes opencode`` and ``/opencode``.

The account store lives with the provider override
(``plugins/model-providers/opencode-accounts/opencode_accounts.py``) because
Hermes loads model-provider plugins on their own path, before general plugins.
This plugin only adds the commands.
"""

from __future__ import annotations


def register(ctx) -> None:
    from . import commands

    ctx.register_command(
        "opencode", commands.slash,
        description="Show or switch the active OpenCode account",
        args_hint="[use <label>]")
    ctx.register_cli_command(
        name="opencode", help="Manage and switch OpenCode accounts (API keys)",
        setup_fn=commands.setup_parser, handler_fn=commands.handle,
        description="Save several OpenCode Console API keys under labels and choose which one Hermes uses.")
