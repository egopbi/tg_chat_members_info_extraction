"""Core dataclass contracts for runtime state and export rows."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Generic, Literal, TypeVar

FieldStatus = Literal["value", "empty", "unavailable", "error"]
T = TypeVar("T")


def _ensure_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _datetime_to_text(value: datetime) -> str:
    _ensure_aware(value, "datetime")
    return value.isoformat()


def _datetime_from_text(value: str, label: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    _ensure_aware(parsed, label)
    return parsed


def _is_blank_text(value: Any) -> bool:
    return isinstance(value, str) and not value.strip()


@dataclass(frozen=True, slots=True)
class FieldResult(Generic[T]):
    status: FieldStatus
    value: T | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"value", "empty", "unavailable", "error"}:
            raise ValueError(f"Unsupported field status: {self.status}")
        if self.status == "value":
            if self.value is None or _is_blank_text(self.value):
                raise ValueError("value status requires a value")
            if self.error_message is not None:
                raise ValueError("value status cannot include an error message")
            return
        if self.status == "error":
            if self.error_message is None or not self.error_message.strip():
                raise ValueError("error status requires an error message")
            return
        if self.value is not None:
            raise ValueError(f"{self.status} status cannot include a value")
        if self.error_message is not None:
            raise ValueError(f"{self.status} status cannot include an error message")

    @classmethod
    def from_value(cls, value: T) -> "FieldResult[T]":
        return cls(status="value", value=value)

    @classmethod
    def empty(cls) -> "FieldResult[T]":
        return cls(status="empty")

    @classmethod
    def unavailable(cls) -> "FieldResult[T]":
        return cls(status="unavailable")

    @classmethod
    def error(cls, error_message: str) -> "FieldResult[T]":
        return cls(status="error", error_message=error_message)

    @property
    def has_value(self) -> bool:
        return self.status == "value"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "value": self.value,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FieldResult[Any]":
        return cls(
            status=payload["status"],
            value=payload.get("value"),
            error_message=payload.get("error_message"),
        )


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_waits: int = 4
    initial_wait_seconds: float = 1.0
    backoff_factor: float = 2.0
    max_wait_seconds: float = 4.0

    def __post_init__(self) -> None:
        if self.max_waits < 0:
            raise ValueError("max_waits must be non-negative")
        if self.max_waits > 4:
            raise ValueError("max_waits must not exceed 4")
        if self.initial_wait_seconds <= 0:
            raise ValueError("initial_wait_seconds must be positive")
        if self.backoff_factor < 1:
            raise ValueError("backoff_factor must be at least 1")
        if self.max_wait_seconds <= 0:
            raise ValueError("max_wait_seconds must be positive")

    def should_retry(self, waits_used: int) -> bool:
        return waits_used < self.max_waits

    def wait_seconds_for_attempt(self, attempt_number: int) -> float:
        if attempt_number < 1:
            raise ValueError("attempt_number must be at least 1")
        raw_wait = self.initial_wait_seconds * (self.backoff_factor ** (attempt_number - 1))
        return min(raw_wait, self.max_wait_seconds)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RetryPolicy":
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class SessionMeta:
    session_name: str
    api_id: int
    api_hash: str
    created_at: datetime
    updated_at: datetime
    account_label: str | None = None
    phone_number: str | None = None
    is_active: bool = False

    def __post_init__(self) -> None:
        if not self.session_name.strip():
            raise ValueError("session_name must not be empty")
        if self.api_id <= 0:
            raise ValueError("api_id must be positive")
        if not self.api_hash.strip():
            raise ValueError("api_hash must not be empty")
        _ensure_aware(self.created_at, "created_at")
        _ensure_aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must be greater than or equal to created_at")
        if self.account_label is not None and not self.account_label.strip():
            raise ValueError("account_label must not be blank")
        if self.phone_number is not None and not self.phone_number.strip():
            raise ValueError("phone_number must not be blank")

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_name": self.session_name,
            "api_id": self.api_id,
            "api_hash": self.api_hash,
            "created_at": _datetime_to_text(self.created_at),
            "updated_at": _datetime_to_text(self.updated_at),
            "account_label": self.account_label,
            "phone_number": self.phone_number,
            "is_active": self.is_active,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SessionMeta":
        return cls(
            session_name=payload["session_name"],
            api_id=int(payload["api_id"]),
            api_hash=payload["api_hash"],
            created_at=_datetime_from_text(payload["created_at"], "created_at"),
            updated_at=_datetime_from_text(payload["updated_at"], "updated_at"),
            account_label=payload.get("account_label"),
            phone_number=payload.get("phone_number"),
            is_active=bool(payload.get("is_active", False)),
        )


@dataclass(frozen=True, slots=True)
class ActiveSessionState:
    session_name: str
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.session_name.strip():
            raise ValueError("session_name must not be empty")
        _ensure_aware(self.updated_at, "updated_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_name": self.session_name,
            "updated_at": _datetime_to_text(self.updated_at),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ActiveSessionState":
        return cls(
            session_name=payload["session_name"],
            updated_at=_datetime_from_text(payload["updated_at"], "updated_at"),
        )


@dataclass(frozen=True, slots=True)
class MemberExportRow:
    user_id: int
    display_name: str
    username: FieldResult[str] = field(default_factory=FieldResult.empty)
    first_name: FieldResult[str] = field(default_factory=FieldResult.empty)
    last_name: FieldResult[str] = field(default_factory=FieldResult.empty)
    phone_number: FieldResult[str] = field(default_factory=FieldResult.empty)
    about: FieldResult[str] = field(default_factory=FieldResult.empty)
    birthday: FieldResult[str] = field(default_factory=FieldResult.empty)
    linked_channel_url: FieldResult[str] = field(default_factory=FieldResult.empty)
    photo_path: FieldResult[str] = field(default_factory=FieldResult.empty)
    export_created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("user_id must be positive")
        if not self.display_name.strip():
            raise ValueError("display_name must not be empty")
        _ensure_aware(self.export_created_at, "export_created_at")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["export_created_at"] = _datetime_to_text(self.export_created_at)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MemberExportRow":
        return cls(
            user_id=int(payload["user_id"]),
            display_name=payload["display_name"],
            username=FieldResult.from_dict(payload["username"]),
            first_name=FieldResult.from_dict(payload["first_name"]),
            last_name=FieldResult.from_dict(payload["last_name"]),
            phone_number=FieldResult.from_dict(payload["phone_number"]),
            about=FieldResult.from_dict(payload["about"]),
            birthday=FieldResult.from_dict(payload["birthday"]),
            linked_channel_url=FieldResult.from_dict(payload["linked_channel_url"]),
            photo_path=FieldResult.from_dict(payload["photo_path"]),
            export_created_at=_datetime_from_text(
                payload["export_created_at"], "export_created_at"
            ),
        )
