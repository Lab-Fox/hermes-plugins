"""NanoGPT Accounts: ``hermes nanogpt`` and ``/nanogpt`` for the nanogpt provider.

The account store lives with the provider profile
(``plugins/model-providers/nanogpt/nanogpt_accounts.py``) because Hermes loads
model-provider plugins on their own path, before general plugins. This plugin
only adds the commands.
"""

from __future__ import annotations


def register(ctx) -> None:
    from . import commands

    ctx.register_command(
        "nanogpt", commands.slash,
        description="Show or switch the active NanoGPT key",
        args_hint="[use <label> | status]")
    ctx.register_cli_command(
        name="nanogpt", help="Manage and switch NanoGPT API keys",
        setup_fn=commands.setup_parser, handler_fn=commands.handle,
        description="Save several NanoGPT keys under labels and choose which one Hermes uses.")
