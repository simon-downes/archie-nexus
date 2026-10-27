"""Kiro backend helpers shared by the agent and host: credit pricing and file locations."""

from __future__ import annotations

import calendar
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from archie_shared.schemas import KiroCreditsConfig

log = logging.getLogger(__name__)

KIRO_MODEL_PREFIX = "kiro/"
_CREDITS_RE = re.compile(r'"credits":\s*-?\d')


def kiro_model_key(model_id: str) -> str:
    """Archie model key for a Kiro model id (``gpt-5.6-luna`` → ``kiro/gpt-5.6-luna``)."""
    return f"{KIRO_MODEL_PREFIX}{model_id}"


def kiro_model_id(model_key: str) -> str | None:
    """Kiro model id for an Archie ``kiro/`` key, or None for other keys."""
    if not model_key.startswith(KIRO_MODEL_PREFIX):
        return None
    return model_key[len(KIRO_MODEL_PREFIX) :] or None


def kiro_logs_dir(home: Path) -> Path:
    return home / "logs" / "kiro"


def frame_log_path(home: Path, session_id: str) -> Path:
    """Raw ACP frame log for a session."""
    return kiro_logs_dir(home) / f"{session_id}.acp.jsonl"


def startup_error_path(home: Path, session_id: str) -> Path:
    """File holding the reason a Kiro session failed to start."""
    return kiro_logs_dir(home) / f"{session_id}.startup-error"


def period_start(now: datetime, resets_on: int) -> datetime:
    """Start (00:00 UTC) of the credit period containing ``now``.

    The reset day is clamped to the month's length, so ``resets_on: 31`` resets on the
    30th in a 30-day month.
    """
    now = now.astimezone(UTC)
    year, month = now.year, now.month
    day = min(resets_on, calendar.monthrange(year, month)[1])
    start = datetime(year, month, day, tzinfo=UTC)
    if start <= now:
        return start
    month -= 1
    if month == 0:
        year, month = year - 1, 12
    day = min(resets_on, calendar.monthrange(year, month)[1])
    return datetime(year, month, day, tzinfo=UTC)


def credit_cost(credits: float, used_before: float, cfg: KiroCreditsConfig) -> float:
    """USD cost of ``credits`` given ``used_before`` credits already used this period.

    Credits within the included allowance cost ``rate_usd``; the rest cost
    ``overage_rate_usd``. A turn that crosses the allowance is split.
    """
    if credits <= 0:
        return 0.0
    remaining = max(0.0, cfg.included - max(0.0, used_before))
    included = min(credits, remaining)
    overage = credits - included
    return round(included * cfg.rate_usd + overage * cfg.overage_rate_usd, 6)


@dataclass
class CreditLedger:
    """Running credit total for the current period; prices each turn."""

    cfg: KiroCreditsConfig
    used: float = 0.0
    start: datetime | None = None

    def charge(self, credits: float, now: datetime) -> float:
        """Price a turn and add it to the period total (resetting at a period boundary)."""
        start = period_start(now, self.cfg.resets_on)
        if self.start is None or start != self.start:
            if self.start is not None:
                self.used = 0.0
            self.start = start
        cost = credit_cost(credits, self.used, self.cfg)
        self.used += max(0.0, credits)
        return cost


def credits_used_since(sessions_dir: Path, start: datetime) -> float:
    """Sum ``credits`` of ``llm_request`` lines with ``sent_at`` ≥ ``start``.

    Only session logs modified since ``start`` are read, and only lines that carry a
    numeric ``credits`` field are decoded.
    """
    total = 0.0
    threshold = start.timestamp()
    for path in _session_logs(sessions_dir):
        try:
            if path.stat().st_mtime < threshold:
                continue
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if '"llm_request"' not in line or not _CREDITS_RE.search(line):
                        continue
                    total += _line_credits(line, start)
        except OSError as exc:
            log.warning("Cannot read %s for Kiro credits: %s", path, exc)
    return total


def _session_logs(sessions_dir: Path) -> Iterable[Path]:
    if not sessions_dir.is_dir():
        return ()
    return sorted(sessions_dir.glob("*.jsonl"))


def _line_credits(line: str, start: datetime) -> float:
    try:
        record = json.loads(line)
        sent_at = datetime.fromisoformat(record["sent_at"])
        credits = float(record["credits"])
    except (ValueError, KeyError, TypeError):
        return 0.0
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)
    return credits if sent_at >= start else 0.0
