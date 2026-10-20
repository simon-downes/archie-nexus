"""Exec-only Google Workspace tools: ``mail``, ``calendar``, ``drive``, ``meet`` and ``google``.

Importing the tool modules registers their tools; always import them relatively so
``calendar`` and ``mail`` never resolve to the stdlib modules of the same name.
"""

from . import calendar, discovery, drive, mail, meet

__all__ = ["calendar", "discovery", "drive", "mail", "meet"]
