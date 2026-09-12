"""Single source of truth for the AIGent build version.

Keep it in step with `pyproject.toml` and `desktop/package.json`; the desktop
auto-update compares this value with the published release tag.
"""

__version__ = "0.6.0"
