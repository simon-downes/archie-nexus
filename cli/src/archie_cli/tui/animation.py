"""One animation clock for the whole TUI (084 D16).

Widgets that animate (working, starting and stopping session rows and headers, the
activity line, running tool and subagent rows) register with the app's
``AnimationClock`` while they animate and unregister when they stop or unmount. The
clock runs one 8 Hz timer only while at least one widget is registered; each tick
advances ``frame`` and refreshes every registered widget, which draws its glyph from
the frame.

``Animated`` is the mixin those widgets use. A widget outside an app with a clock (a
test app, say) still renders, at frame 0.
"""

from __future__ import annotations

from textual.app import App
from textual.timer import Timer
from textual.widget import Widget

INTERVAL = 1 / 8
"""Seconds between animation frames."""

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
"""Frames of the working spinner, one per tick."""

PENDING_FRAMES = "◌◌◌◌○○○○"
"""Frames of the starting/stopping glyph: a slow pulse between two circles."""


def spinner(frame: int) -> str:
    """The spinner character for ``frame``."""
    return SPINNER[frame % len(SPINNER)]


class AnimationClock:
    """The app's animation timer, running only while a widget is registered.

    Attributes:
        frame: Ticks so far; animated widgets draw from it.
    """

    def __init__(self) -> None:
        self.frame = 0
        self._widgets: set[Widget] = set()
        self._timer: Timer | None = None
        self._running = False

    def start(self, app: App) -> None:
        """Create the (paused) timer on ``app``; call once the app is mounted."""
        self._timer = app.set_interval(INTERVAL, self._tick, pause=True)
        self._update()

    @property
    def running(self) -> bool:
        """True while the timer ticks."""
        return self._running

    @property
    def widgets(self) -> frozenset[Widget]:
        """The registered widgets."""
        return frozenset(self._widgets)

    def add(self, widget: Widget) -> None:
        """Refresh ``widget`` on every tick until ``discard``."""
        self._widgets.add(widget)
        self._update()

    def discard(self, widget: Widget) -> None:
        """Stop refreshing ``widget``; the timer pauses when none is left."""
        self._widgets.discard(widget)
        self._update()

    def _update(self) -> None:
        timer = self._timer
        if timer is None:
            return
        wanted = bool(self._widgets)
        if wanted and not self._running:
            timer.resume()
        elif not wanted and self._running:
            timer.pause()
        self._running = wanted

    def _tick(self) -> None:
        self.frame += 1
        for widget in list(self._widgets):
            if widget.is_attached:
                widget.refresh()
            else:
                self._widgets.discard(widget)  # removed without unmounting cleanly
        self._update()


def clock_of(widget: Widget) -> AnimationClock | None:
    """The clock of the app ``widget`` runs in, or None (no app, or no clock)."""
    try:
        app = widget.app
    except Exception:  # noqa: BLE001 — no active app (built outside one)
        return None
    clock = getattr(app, "animation", None)
    return clock if isinstance(clock, AnimationClock) else None


class Animated(Widget):
    """A widget that is registered with the clock while ``animating`` is True.

    Subclasses define ``animating`` and call ``sync_animation`` whenever it may have
    changed; mounting and unmounting sync on their own.
    """

    @property
    def animating(self) -> bool:
        """True while the widget animates."""
        return False

    @property
    def frame(self) -> int:
        """The clock's current frame (0 without a clock)."""
        clock = clock_of(self)
        return clock.frame if clock is not None else 0

    def sync_animation(self) -> None:
        """Register with the clock while animating and mounted; unregister otherwise."""
        if self.is_mounted:
            self._sync()  # before that, mounting syncs

    def _sync(self) -> None:
        clock = clock_of(self)
        if clock is None:
            return
        if self.animating and self.is_attached:
            clock.add(self)
        else:
            clock.discard(self)

    def _on_mount(self) -> None:
        self._sync()  # ``is_mounted`` only turns true after the mount handlers

    def _on_unmount(self) -> None:
        clock = clock_of(self)
        if clock is not None:
            clock.discard(self)
