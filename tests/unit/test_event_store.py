import pytest

from risk_ledger.events import (
    EventIntegrityError,
    EventStore,
    VersionConflictError,
    new_event,
    verify_stream,
)
from risk_ledger.models import EventType, SessionStatus


@pytest.mark.unit
def test_event_store_is_append_only_and_optimistically_versioned() -> None:
    store = EventStore()
    event = new_event(
        session_id="session-12345678",
        sequence=1,
        event_type=EventType.SESSION_CREATED,
        status=SessionStatus.CREATED,
    )

    stream = store.append("session-12345678", expected_version=0, events=(event,))

    assert stream == (event,)
    assert store.read("session-12345678") == (event,)
    with pytest.raises(VersionConflictError):
        store.append("session-12345678", expected_version=0, events=(event,))


@pytest.mark.unit
def test_audit_chain_detects_payload_tampering_and_sequence_gaps() -> None:
    event = new_event(
        session_id="session-12345678",
        sequence=1,
        event_type=EventType.SESSION_CREATED,
        status=SessionStatus.CREATED,
    )
    tampered = event.model_copy(update={"reason": "changed after append"})
    gap = new_event(
        session_id="session-gap-1234",
        sequence=2,
        event_type=EventType.SESSION_CREATED,
        status=SessionStatus.CREATED,
    )

    assert verify_stream((event,)) is True
    assert verify_stream((tampered,)) is False
    with pytest.raises(EventIntegrityError):
        EventStore().append("session-gap-1234", expected_version=0, events=(gap,))
