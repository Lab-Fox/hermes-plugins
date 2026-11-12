"""Session Context: the agent half is empty on purpose.

The work is in ``dashboard/plugin_api.py`` (read-only routes over the session
database) and ``desktop/plugin.js`` (the Desktop chip and Context tab). This
file exists so the package can be listed in ``plugins.enabled``, which is what
lets Hermes mount the routes.
"""


def register(ctx) -> None:
    return None
