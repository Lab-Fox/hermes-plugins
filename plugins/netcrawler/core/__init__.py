"""NetCrawler core — Hermes-free package.

Everything under ``core`` imports only the standard library and the plugin's
declared third-party dependencies. Hermes APIs (``ctx``, plugin storage) are
injected from the plugin shell (``tools.py`` / ``__init__.py``) so the whole
engine can be exercised by tests without loading Hermes.
"""

__version__ = "1.0.0"