"""Append-only, hash-chained in-memory event store."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

from risk_ledger.hashing import canonical_digest
from risk_ledger.models import EventType, LedgerEvent, SessionStatus, TerminalOutcome

GENESIS_HASH = "0" * 64


class VersionConflictError(RuntimeError):
    """Raised when optimistic stream versioning detects stale authority."""


class EventIntegrityError(RuntimeError):
    """Raised when a sequence or audit hash is invalid."""


def _hash_payload(event: LedgerEvent) -> dict[str, Any]:
    return event.model_dump(mode="json", exclude={"event_hash"})


def new_event(
    *,
    session_id: str,
    sequence: int,
    event_type: EventType,
    status: SessionStatus,
    outcome: TerminalOutcome | None = None,
    previous_hash: str = GENESIS_HASH,
    occurred_at: datetime | None = None,
    **fields: Any,
) -> LedgerEvent:
    """Create one immutable event whose hash commits to its complete payload."""

    timestamp = occurred_at or datetime.now(UTC)
    event_identity = {
        "session": session_id,
        "sequence": sequence,
        "type": event_type,
        "at": timestamp,
    }
    event_id = f"EVENT-{canonical_digest(event_identity)[:24]}"
    unhashed = LedgerEvent(
        event_id=event_id,
        session_id=session_id,
        sequence=sequence,
        event_type=event_type,
        occurred_at=timestamp,
        status=status,
        outcome=outcome,
        previous_hash=previous_hash,
        event_hash=GENESIS_HASH,
        **fields,
    )
    return unhashed.model_copy(update={"event_hash": canonical_digest(_hash_payload(unhashed))})


def verify_event(event: LedgerEvent) -> bool:
    return event.event_hash == canonical_digest(_hash_payload(event))


def verify_stream(events: tuple[LedgerEvent, ...]) -> bool:
    previous_hash = GENESIS_HASH
    for expected_sequence, event in enumerate(events, start=1):
        if event.sequence != expected_sequence or event.previous_hash != previous_hash:
            return False
        if not verify_event(event):
            return False
        previous_hash = event.event_hash
    return True


class EventStore:
    """Process-local MVP store; records are tuples and can only be appended."""

    def __init__(self) -> None:
        self._streams: dict[str, tuple[LedgerEvent, ...]] = {}
        self._lock = threading.RLock()

    def read(self, session_id: str) -> tuple[LedgerEvent, ...]:
        with self._lock:
            return self._streams.get(session_id, ())

    def append(
        self,
        session_id: str,
        *,
        expected_version: int,
        events: tuple[LedgerEvent, ...],
    ) -> tuple[LedgerEvent, ...]:
        if not events:
            raise EventIntegrityError("At least one event is required")
        with self._lock:
            current = self._streams.get(session_id, ())
            if len(current) != expected_version:
                raise VersionConflictError("Event stream version changed")
            previous_hash = current[-1].event_hash if current else GENESIS_HASH
            next_sequence = len(current) + 1
            for event in events:
                if event.session_id != session_id:
                    raise EventIntegrityError("Event belongs to a different session")
                if event.sequence != next_sequence or event.previous_hash != previous_hash:
                    raise EventIntegrityError("Event sequence or hash link is invalid")
                if not verify_event(event):
                    raise EventIntegrityError("Event payload hash is invalid")
                next_sequence += 1
                previous_hash = event.event_hash
            updated = current + events
            self._streams = {**self._streams, session_id: updated}
            return updated
