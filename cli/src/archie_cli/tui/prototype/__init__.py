"""Interactive mock-up of a multi-session Archie TUI (``archie tui``).

A prototype for roadmap item 4 (sidebar and session switching) and the wider TUI
overhaul. Historical session logs stand in for running sessions: the app replays
them through the real transcript model and views, and gives each one a mock live
state (working, blocked, idle, error, ended). Nothing connects to the orchestrator
or to an agent, and session logs and ``metrics.db`` are only read.
"""
