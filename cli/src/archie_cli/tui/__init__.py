"""The multi-session Textual TUI that ``archie start`` and ``archie attach`` open (084).

``app.ArchieApp`` is the window; ``client`` talks to the orchestrator, ``session`` holds
each session's state without widgets, and the remaining modules are the widgets that
show it.
"""
