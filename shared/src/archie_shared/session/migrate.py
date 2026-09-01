"""Legacy session-log structures retained for one-shot migration.

These types decode pre-034 ``MessageEntry`` lines only. Runtime session writes
use canonical events and ``SessionEventBus``; migration logic is added here in
M7.
"""

import msgspec


class MessageMetadata(msgspec.Struct):
    """Legacy per-message metadata retained for migration decoding."""

    model: str
    backend: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float = 0.0
    interrupted: bool = False


class MessageEntry(msgspec.Struct):
    """Legacy JSONL message entry retained for migration decoding."""

    id: str
    when: str
    role: str
    content: str
    metadata: MessageMetadata | None = None
