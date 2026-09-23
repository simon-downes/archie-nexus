"""Shared mutation transmission-state contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TransmissionState = Literal["not_started", "started", "completed"]


@dataclass(frozen=True, slots=True)
class TransmissionResult:
    state: TransmissionState
    response: dict | None = None
    error: Exception | None = None
