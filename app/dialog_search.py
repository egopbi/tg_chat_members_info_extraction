"""Unicode-safe dialog discovery and ranking helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import datetime, timezone
from typing import Any
import unicodedata

from telethon import TelegramClient

from .models import DialogCandidate


def normalize_dialog_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _as_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dialog_title(dialog: Any) -> str:
    title = getattr(dialog, "name", None) or getattr(dialog, "title", None)
    if title:
        return str(title)
    entity = getattr(dialog, "entity", None)
    entity_title = getattr(entity, "title", None)
    return str(entity_title or "")


def classify_dialog_entity(entity: Any) -> str | None:
    if entity is None:
        return None
    if getattr(entity, "broadcast", False):
        return None
    if getattr(entity, "megagroup", False):
        return "forum" if getattr(entity, "forum", False) else "supergroup"
    if hasattr(entity, "participants_count"):
        return "group"
    return None


def build_dialog_candidate(dialog: Any) -> DialogCandidate | None:
    entity = getattr(dialog, "entity", None)
    entity_type = classify_dialog_entity(entity)
    if entity_type is None:
        return None

    title = _dialog_title(dialog)
    if not title.strip():
        return None

    peer_id = getattr(dialog, "id", None)
    if peer_id in (None, 0):
        peer_id = getattr(entity, "id", None)
    if peer_id in (None, 0):
        return None

    username = getattr(entity, "username", None)
    participants_count = getattr(entity, "participants_count", None)
    last_message_date = _as_utc_datetime(
        getattr(dialog, "date", None)
        or getattr(getattr(dialog, "message", None), "date", None)
    )

    return DialogCandidate(
        title=title,
        entity_type=entity_type,
        peer_id=int(peer_id),
        username=username,
        participants_count=(
            int(participants_count)
            if participants_count is not None
            else None
        ),
        last_message_date=last_message_date,
    )


def rank_dialog_candidates(
    query: str,
    candidates: Sequence[DialogCandidate],
) -> list[DialogCandidate]:
    normalized_query = normalize_dialog_title(query)
    if not normalized_query:
        return []

    ranked: list[tuple[int, int, DialogCandidate]] = []
    for index, candidate in enumerate(candidates):
        normalized_candidate = normalize_dialog_title(candidate.title)
        if candidate.title == query:
            rank = 0
        elif normalized_candidate == normalized_query:
            rank = 1
        elif normalized_query in normalized_candidate:
            rank = 2
        else:
            continue
        ranked.append((rank, index, candidate))

    ranked.sort(key=lambda item: (item[0], item[1]))
    return [candidate for _, _, candidate in ranked]


def search_dialog_candidates(
    query: str,
    dialogs: Iterable[DialogCandidate],
) -> list[DialogCandidate]:
    return rank_dialog_candidates(query, list(dialogs))


async def iter_dialog_candidates(client: TelegramClient) -> AsyncIterator[DialogCandidate]:
    async for dialog in client.iter_dialogs():
        candidate = build_dialog_candidate(dialog)
        if candidate is not None:
            yield candidate


async def find_dialog_candidates(
    client: TelegramClient,
    query: str,
) -> list[DialogCandidate]:
    candidates = [candidate async for candidate in iter_dialog_candidates(client)]
    return rank_dialog_candidates(query, candidates)
