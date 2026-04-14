from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import (
    ActiveSessionState,
    FieldResult,
    MemberExportRow,
    RetryPolicy,
    SessionMeta,
)


def test_field_result_contracts() -> None:
    value_result = FieldResult.from_value("Alice")
    empty_result = FieldResult.empty()
    unavailable_result = FieldResult.unavailable()
    error_result = FieldResult.error("telegram throttled")

    assert value_result.status == "value"
    assert value_result.value == "Alice"
    assert empty_result.status == "empty"
    assert empty_result.value is None
    assert unavailable_result.status == "unavailable"
    assert error_result.status == "error"
    assert error_result.error_message == "telegram throttled"

    with pytest.raises(ValueError, match="value status requires a value"):
        FieldResult.from_value("")

    with pytest.raises(ValueError, match="value status requires a value"):
        FieldResult.from_value("   ")


def test_field_result_round_trip() -> None:
    payload = FieldResult.from_value("example").to_dict()

    assert FieldResult.from_dict(payload) == FieldResult.from_value("example")


def test_session_meta_round_trip_and_validation() -> None:
    timestamp = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)
    meta = SessionMeta(
        session_name="main profile",
        api_id=123456,
        api_hash="hash",
        created_at=timestamp,
        updated_at=timestamp,
        account_label="Test User",
        phone_number="+10000000000",
        is_active=True,
    )

    assert SessionMeta.from_dict(meta.to_dict()) == meta

    with pytest.raises(ValueError, match="session_name must not be empty"):
        SessionMeta(
            session_name="   ",
            api_id=1,
            api_hash="hash",
            created_at=timestamp,
            updated_at=timestamp,
        )


def test_active_session_state_round_trip() -> None:
    timestamp = datetime(2026, 4, 14, 12, 5, tzinfo=timezone.utc)
    state = ActiveSessionState(session_name="main profile", updated_at=timestamp)

    assert ActiveSessionState.from_dict(state.to_dict()) == state


def test_retry_policy_caps_waits_at_four() -> None:
    policy = RetryPolicy()

    assert policy.max_waits == 4
    assert policy.should_retry(0) is True
    assert policy.should_retry(4) is False
    assert policy.wait_seconds_for_attempt(1) == 1.0
    assert policy.wait_seconds_for_attempt(3) == 4.0

    with pytest.raises(ValueError, match="max_waits must not exceed 4"):
        RetryPolicy(max_waits=5)


def test_member_export_row_round_trip() -> None:
    timestamp = datetime(2026, 4, 14, 12, 10, tzinfo=timezone.utc)
    row = MemberExportRow(
        user_id=42,
        display_name="Alice Example",
        username=FieldResult.from_value("alice"),
        about=FieldResult.empty(),
        linked_channel_url=FieldResult.unavailable(),
        photo_path=FieldResult.error("download failed"),
        export_created_at=timestamp,
    )

    assert MemberExportRow.from_dict(row.to_dict()) == row

    with pytest.raises(ValueError, match="display_name must not be empty"):
        MemberExportRow(user_id=1, display_name=" ")
