"""Provider Keys: ``hermes keys`` and ``/keys``.

The key store and provider table live with the provider plugin
(``plugins/model-providers/free-providers/``) because Hermes loads
model-provider plugins on their own path, before general plugins. This plugin
only adds the commands.
"""

from __future__ import annotations


def register(ctx) -> None:
    from . import commands

    ctx.register_command(
        "keys", commands.slash,
        description="Show or switch the active API key of a model provider",
        args_hint="[use <provider> <label> | models all|free | auto <provider|all> on|off | providers]")
    ctx.register_cli_command(
        name="keys", help="Manage and switch API keys for free-tier and built-in providers",
        setup_fn=commands.setup_parser, handler_fn=commands.handle,
        description="Save several API keys per provider under labels and choose which one Hermes uses.")
