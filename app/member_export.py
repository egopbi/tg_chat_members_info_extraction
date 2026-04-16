"""Best-effort member export orchestration."""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .avatar_store import AvatarStore
from .csv_writer import CSVWriter, CsvExportRow
from .models import FieldResult, RetryPolicy

logger = logging.getLogger(__name__)


class ParticipantLike(Protocol):
    id: int
    first_name: str | None
    last_name: str | None
    username: str | None
    phone: str | None
    photo: Any | None


class FullUserLike(Protocol):
    about: str | None
    birthday: Any | None
    personal_channel_id: int | None
    phone: str | None


class TelegramGateway(Protocol):
    async def get_me(self) -> Any: ...

    def iter_participants(self, chat: Any) -> AsyncIterator[ParticipantLike]: ...

    async def get_full_user(self, user: ParticipantLike) -> FullUserLike: ...

    async def get_entity(self, peer: Any) -> Any: ...

    async def download_profile_photo(self, entity: Any, file: Path) -> str | None: ...


@dataclass(frozen=True, slots=True)
class MemberExportRow:
    user_id: int
    first_name: str
    last_name: str
    username: str
    phone_number: FieldResult[str]
    about: FieldResult[str]
    birthday: FieldResult[str]
    photo_path: FieldResult[str]
    linked_channel_url: FieldResult[str]

    def to_csv_row(self) -> CsvExportRow:
        return CsvExportRow(
            user_id=self.user_id,
            first_name=self.first_name,
            last_name=self.last_name,
            username=self.username,
            phone_number=self.phone_number,
            about=self.about,
            birthday=self.birthday,
            photo_path=self.photo_path,
            linked_channel_url=self.linked_channel_url,
        )


@dataclass(frozen=True, slots=True)
class ExportProgressSnapshot:
    run_id: str
    total: int | None
    processed: int
    exported: int
    skipped: int
    deduplicated: int
    failed: int
    observed: int = 0
    is_final: bool = False


@dataclass(frozen=True, slots=True)
class ExportSummary:
    run_id: str
    chat_label: str | None
    current_user_id: int
    csv_path: Path
    avatars_dir: Path
    rows: tuple[MemberExportRow, ...]
    total_seen: int
    exported_count: int
    skipped_current_account: int
    deduplicated_count: int
    failed_user_ids: tuple[int, ...]
    warnings: tuple[str, ...]


def _now_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def _emit_progress(
    progress_callback: Callable[[ExportProgressSnapshot], Any] | None,
    *,
    run_id: str,
    total: int | None,
    processed: int,
    observed: int,
    exported: int,
    skipped: int,
    deduplicated: int,
    failed: int,
    is_final: bool = False,
) -> None:
    if progress_callback is None:
        return
    progress_callback(
        ExportProgressSnapshot(
            run_id=run_id,
            total=total,
            processed=processed,
            observed=observed,
            exported=exported,
            skipped=skipped,
            deduplicated=deduplicated,
            failed=failed,
            is_final=is_final,
        )
    )


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _extract_user_id(user: Any) -> int:
    user_id = getattr(user, "id", None)
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("participant is missing a valid user id")
    return user_id


def _text_result(value: Any, *, unavailable_if_missing: bool = False) -> FieldResult[str]:
    text = _clean_text(value)
    if text:
        return FieldResult.from_value(text)
    return FieldResult.unavailable() if unavailable_if_missing else FieldResult.empty()


def _birthday_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, date):
        return value.isoformat()
    day = getattr(value, "day", None)
    month = getattr(value, "month", None)
    year = getattr(value, "year", None)
    if day is None or month is None:
        return str(value).strip()
    if year is not None:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return f"{int(month):02d}-{int(day):02d}"


def _birthday_result(value: Any) -> FieldResult[str]:
    text = _birthday_to_text(value)
    return FieldResult.from_value(text) if text else FieldResult.empty()


def _phone_result(*sources: Any) -> FieldResult[str]:
    for source in sources:
        if source is None:
            continue
        phone = _clean_text(getattr(source, "phone", None))
        if phone:
            return FieldResult.from_value(phone)
    return FieldResult.unavailable()


def _explicit_wait_seconds(exc: Exception) -> float | None:
    for attr in ("seconds", "value", "wait_seconds", "retry_after"):
        raw = getattr(exc, attr, None)
        if isinstance(raw, (int, float)) and raw > 0:
            return float(raw)
    return None


def _is_retryable_exception(exc: Exception) -> bool:
    explicit_wait = _explicit_wait_seconds(exc)
    if explicit_wait is not None:
        return True
    name = type(exc).__name__.lower()
    return any(
        token in name
        for token in (
            "floodwait",
            "floodpremiumwait",
            "slowmodewait",
            "timeout",
            "connection",
            "network",
        )
    )


def _is_unavailable_exception(exc: Exception) -> bool:
    if isinstance(exc, (PermissionError, LookupError)):
        return True
    name = type(exc).__name__.lower()
    return any(
        token in name
        for token in (
            "private",
            "forbidden",
            "unauthorized",
            "access",
            "notfound",
            "not_found",
            "notparticipant",
            "not_participant",
            "unavailable",
        )
        )


def _retry_wait_seconds(
    exc: Exception,
    *,
    policy: RetryPolicy,
    waits_used: int,
    jitter: Callable[[float], float] | None,
) -> float:
    explicit_wait = _explicit_wait_seconds(exc)
    if explicit_wait is not None:
        return explicit_wait
    base_wait = policy.wait_seconds_for_attempt(waits_used)
    jitter_fn = jitter or (lambda upper: random.uniform(0.0, upper))
    return min(
        policy.max_wait_seconds,
        base_wait + jitter_fn(min(base_wait / 2, 0.5)),
    )


async def _retry_async(
    action: Callable[[], Any],
    *,
    action_name: str,
    policy: RetryPolicy,
    sleep: Callable[[float], Any] = asyncio.sleep,
    jitter: Callable[[float], float] | None = None,
) -> Any:
    waits_used = 0
    while True:
        try:
            return await action()
        except Exception as exc:  # pragma: no cover - exercised by tests through fakes
            if not _is_retryable_exception(exc) or waits_used >= policy.max_waits:
                logger.exception("%s failed without a retry path", action_name)
                raise
            waits_used += 1
            wait_seconds = _retry_wait_seconds(
                exc,
                policy=policy,
                waits_used=waits_used,
                jitter=jitter,
            )
            logger.warning(
                "%s hit %s and will retry in %.2fs (attempt %d/%d)",
                action_name,
                type(exc).__name__,
                wait_seconds,
                waits_used,
                policy.max_waits,
                exc_info=True,
            )
            await sleep(wait_seconds)


async def _iter_participants_with_retry(
    gateway: TelegramGateway,
    chat: Any,
    *,
    policy: RetryPolicy,
    sleep: Callable[[float], Any],
    jitter: Callable[[float], float] | None,
) -> AsyncIterator[ParticipantLike]:
    waits_used = 0
    while True:
        try:
            async for participant in gateway.iter_participants(chat):
                yield participant
            return
        except Exception as exc:  # pragma: no cover - exercised by tests through fakes
            if not _is_retryable_exception(exc) or waits_used >= policy.max_waits:
                logger.exception("Participant iteration failed without a retry path")
                raise
            waits_used += 1
            wait_seconds = _retry_wait_seconds(
                exc,
                policy=policy,
                waits_used=waits_used,
                jitter=jitter,
            )
            logger.warning(
                "Participant iteration hit %s and will retry in %.2fs (attempt %d/%d)",
                type(exc).__name__,
                wait_seconds,
                waits_used,
                policy.max_waits,
                exc_info=True,
            )
            await sleep(wait_seconds)


async def _current_user_id(gateway: TelegramGateway, *, policy: RetryPolicy, sleep: Callable[[float], Any], jitter: Callable[[float], float] | None) -> int:
    logger.debug("Fetching current Telegram account id")
    me = await _retry_async(
        lambda: gateway.get_me(),
        action_name="Fetch current Telegram account",
        policy=policy,
        sleep=sleep,
        jitter=jitter,
    )
    return _extract_user_id(me)


def _display_name(user: ParticipantLike) -> str:
    parts = [_clean_text(getattr(user, "first_name", None)), _clean_text(getattr(user, "last_name", None))]
    return " ".join(part for part in parts if part).strip()


async def _resolve_linked_channel_url(
    gateway: TelegramGateway,
    full_user: FullUserLike,
    *,
    policy: RetryPolicy,
    sleep: Callable[[float], Any],
    jitter: Callable[[float], float] | None,
) -> FieldResult[str]:
    linked_channel_id = getattr(full_user, "personal_channel_id", None)
    if linked_channel_id is None:
        return FieldResult.unavailable()
    try:
        channel = await _retry_async(
            lambda: gateway.get_entity(linked_channel_id),
            action_name=f"Resolve linked channel for {linked_channel_id}",
            policy=policy,
            sleep=sleep,
            jitter=jitter,
        )
    except Exception as exc:
        if _is_unavailable_exception(exc):
            logger.info(
                "Linked channel %s is unavailable for the current user",
                linked_channel_id,
            )
            return FieldResult.unavailable()
        logger.warning(
            "Failed to resolve linked channel %s",
            linked_channel_id,
            exc_info=True,
        )
        return FieldResult.error(str(exc))

    username = _clean_text(getattr(channel, "username", None))
    if not username:
        return FieldResult.empty()
    return FieldResult.from_value(f"https://t.me/{username}")


async def _download_avatar(
    gateway: TelegramGateway,
    avatar_store: AvatarStore,
    run_id: str,
    user: ParticipantLike,
    *,
    policy: RetryPolicy,
    sleep: Callable[[float], Any],
    jitter: Callable[[float], float] | None,
) -> FieldResult[str]:
    if not getattr(user, "photo", None):
        return FieldResult.empty()
    user_id = _extract_user_id(user)
    target_path = avatar_store.avatar_path(
        run_id,
        user_id,
        username=_clean_text(getattr(user, "username", None)) or None,
        display_name=_display_name(user) or None,
    )
    try:
        downloaded = await _retry_async(
            lambda: gateway.download_profile_photo(user, target_path),
            action_name=f"Download avatar for user {user_id}",
            policy=policy,
            sleep=sleep,
            jitter=jitter,
        )
    except Exception as exc:
        if _is_unavailable_exception(exc):
            logger.info("Avatar is unavailable for user %s", user_id)
            return FieldResult.unavailable()
        logger.warning("Avatar download failed for user %s", user_id, exc_info=True)
        return FieldResult.error(str(exc))

    if downloaded is None:
        return FieldResult.unavailable()
    return FieldResult.from_value(str(downloaded))


async def _build_row(
    gateway: TelegramGateway,
    participant: ParticipantLike,
    *,
    run_id: str,
    avatar_store: AvatarStore,
    policy: RetryPolicy,
    sleep: Callable[[float], Any],
    jitter: Callable[[float], float] | None,
) -> tuple[MemberExportRow | None, str | None, bool]:
    user_id = _extract_user_id(participant)
    first_name = _clean_text(getattr(participant, "first_name", None))
    last_name = _clean_text(getattr(participant, "last_name", None))
    username = _clean_text(getattr(participant, "username", None))
    participant_phone = _phone_result(participant)
    logger.debug("Building export row for user %s (%s)", user_id, username or "no username")

    try:
        full_user = await _retry_async(
            lambda: gateway.get_full_user(participant),
            action_name=f"Fetch full profile for user {user_id}",
            policy=policy,
            sleep=sleep,
            jitter=jitter,
        )
    except Exception as exc:
        if _is_unavailable_exception(exc):
            logger.info("Profile fields are unavailable for user %s", user_id)
            return (
                MemberExportRow(
                    user_id=user_id,
                    first_name=first_name,
                    last_name=last_name,
                    username=username,
                    phone_number=participant_phone,
                    about=FieldResult.unavailable(),
                    birthday=FieldResult.unavailable(),
                    photo_path=await _download_avatar(
                        gateway,
                        avatar_store,
                        run_id,
                        participant,
                        policy=policy,
                        sleep=sleep,
                        jitter=jitter,
                    ),
                    linked_channel_url=FieldResult.unavailable(),
                ),
                f"user {user_id}: profile fields unavailable ({exc})",
                False,
            )
        logger.warning("Profile field enrichment failed for user %s", user_id, exc_info=True)
        return (
            MemberExportRow(
                user_id=user_id,
                first_name=first_name,
                last_name=last_name,
                username=username,
                phone_number=(
                    participant_phone if participant_phone.has_value else FieldResult.error(str(exc))
                ),
                about=FieldResult.error(str(exc)),
                birthday=FieldResult.error(str(exc)),
                photo_path=await _download_avatar(
                    gateway,
                    avatar_store,
                    run_id,
                    participant,
                    policy=policy,
                    sleep=sleep,
                    jitter=jitter,
                ),
                linked_channel_url=FieldResult.error(str(exc)),
            ),
            f"user {user_id}: profile fields failed ({exc})",
            True,
        )

    about = _text_result(getattr(full_user, "about", None))
    birthday = _birthday_result(getattr(full_user, "birthday", None))
    phone_number = participant_phone if participant_phone.has_value else _phone_result(full_user)
    linked_channel_url = await _resolve_linked_channel_url(
        gateway,
        full_user,
        policy=policy,
        sleep=sleep,
        jitter=jitter,
    )
    photo_path = await _download_avatar(
        gateway,
        avatar_store,
        run_id,
        participant,
        policy=policy,
        sleep=sleep,
        jitter=jitter,
    )

    failed = any(result.status == "error" for result in (about, birthday, linked_channel_url, photo_path))
    warning = None
    if failed:
        warning = f"user {user_id}: one or more enrichments failed"
        logger.warning("One or more enrichments failed for user %s", user_id)
    else:
        logger.debug("Finished export row for user %s", user_id)

    return (
        MemberExportRow(
            user_id=user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
            phone_number=phone_number,
            about=about,
            birthday=birthday,
            photo_path=photo_path,
            linked_channel_url=linked_channel_url,
        ),
        warning,
        failed,
    )


async def export_members(
    gateway: TelegramGateway,
    chat: Any,
    *,
    run_id: str | None = None,
    expected_total: int | None = None,
    runtime_dir: Path | str = ".runtime",
    csv_writer: CSVWriter | None = None,
    avatar_store: AvatarStore | None = None,
    retry_policy: RetryPolicy | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
    jitter: Callable[[float], float] | None = None,
    progress_callback: Callable[[ExportProgressSnapshot], Any] | None = None,
    chat_label: str | None = None,
) -> ExportSummary:
    policy = retry_policy or RetryPolicy()
    store = avatar_store or AvatarStore(runtime_dir)
    writer = csv_writer or CSVWriter()
    run_id = run_id or _now_run_id()
    if expected_total is not None and expected_total < 0:
        raise ValueError("expected_total must be non-negative")

    avatars_dir = store.avatars_dir(run_id)
    csv_path = store.run_dir(run_id) / "members.csv"
    warnings: list[str] = []
    failed_user_ids: list[int] = []
    exported_rows: list[MemberExportRow] = []
    seen_user_ids: set[int] = set()
    progress_seen_user_ids: set[int] = set()
    progress_counted_current_user = False
    total_seen = 0
    processed_unique = 0
    skipped_current = 0
    deduplicated = 0
    exported_count = 0
    failed_count = 0

    logger.info(
        "Starting member export run_id=%s chat_label=%r expected_total=%s",
        run_id,
        chat_label,
        expected_total,
    )

    current_user_id = await _current_user_id(
        gateway,
        policy=policy,
        sleep=sleep,
        jitter=jitter,
    )

    _emit_progress(
        progress_callback,
        run_id=run_id,
        total=expected_total,
        processed=0,
        observed=0,
        exported=0,
        skipped=0,
        deduplicated=0,
        failed=0,
    )

    try:
        async for participant in _iter_participants_with_retry(
            gateway,
            chat,
            policy=policy,
            sleep=sleep,
            jitter=jitter,
        ):
            total_seen += 1
            user_id = _extract_user_id(participant)
            if user_id == current_user_id:
                if not progress_counted_current_user:
                    progress_counted_current_user = True
                    processed_unique += 1
            elif user_id not in progress_seen_user_ids:
                progress_seen_user_ids.add(user_id)
                processed_unique += 1

            if user_id == current_user_id:
                skipped_current += 1
                logger.debug("Skipping current account user_id=%s", user_id)
                _emit_progress(
                    progress_callback,
                    run_id=run_id,
                    total=expected_total,
                    processed=processed_unique,
                    observed=total_seen,
                    exported=exported_count,
                    skipped=skipped_current,
                    deduplicated=deduplicated,
                    failed=failed_count,
                )
                continue
            if user_id in seen_user_ids:
                deduplicated += 1
                logger.debug("Skipping duplicate participant user_id=%s", user_id)
                _emit_progress(
                    progress_callback,
                    run_id=run_id,
                    total=expected_total,
                    processed=processed_unique,
                    observed=total_seen,
                    exported=exported_count,
                    skipped=skipped_current,
                    deduplicated=deduplicated,
                    failed=failed_count,
                )
                continue
            seen_user_ids.add(user_id)

            row, warning, failed = await _build_row(
                gateway,
                participant,
                run_id=run_id,
                avatar_store=store,
                policy=policy,
                sleep=sleep,
                jitter=jitter,
            )
            if row is None:
                continue
            exported_rows.append(row)
            exported_count += 1
            if warning:
                warnings.append(warning)
            if failed:
                failed_user_ids.append(user_id)
                failed_count += 1
            logger.debug(
                "Processed participant user_id=%s exported_count=%d failed=%s",
                user_id,
                exported_count,
                failed,
            )

            _emit_progress(
                progress_callback,
                run_id=run_id,
                total=expected_total,
                processed=processed_unique,
                observed=total_seen,
                exported=exported_count,
                skipped=skipped_current,
                deduplicated=deduplicated,
                failed=failed_count,
            )
    except Exception as exc:
        warnings.append(f"participant iteration stopped after retries: {exc}")
        logger.exception("Participant iteration stopped after retries")

    try:
        logger.info("Writing export CSV to %s", csv_path)
        writer.write(csv_path, [row.to_csv_row() for row in exported_rows])
    finally:
        _emit_progress(
            progress_callback,
            run_id=run_id,
            total=expected_total,
            processed=processed_unique,
            observed=total_seen,
            exported=exported_count,
            skipped=skipped_current,
            deduplicated=deduplicated,
            failed=failed_count,
            is_final=True,
        )
        logger.info(
            "Finished member export run_id=%s exported=%d failed=%d skipped=%d deduplicated=%d total_seen=%d",
            run_id,
            exported_count,
            failed_count,
            skipped_current,
            deduplicated,
            total_seen,
        )

    return ExportSummary(
        run_id=run_id,
        chat_label=chat_label,
        current_user_id=current_user_id,
        csv_path=csv_path,
        avatars_dir=avatars_dir,
        rows=tuple(exported_rows),
        total_seen=total_seen,
        exported_count=len(exported_rows),
        skipped_current_account=skipped_current,
        deduplicated_count=deduplicated,
        failed_user_ids=tuple(failed_user_ids),
        warnings=tuple(warnings),
    )
