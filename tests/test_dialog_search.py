from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.dialog_search import (
    build_dialog_candidate,
    classify_dialog_entity,
    normalize_dialog_title,
    rank_dialog_candidates,
)
from app.models import DialogCandidate


def test_normalize_dialog_title_preserves_unicode_and_collapses_spaces() -> None:
    assert normalize_dialog_title("  ГРУППА 👋   ") == "группа 👋"


def test_build_dialog_candidate_is_unicode_safe_and_classifies_forums() -> None:
    naive_date = datetime(2026, 4, 14, 12, 0)
    forum_entity = SimpleNamespace(
        id=101,
        title="Telegram Forum 👋",
        username="forumchat",
        participants_count=42,
        megagroup=True,
        forum=True,
    )
    dialog = SimpleNamespace(
        id=-100101,
        name="Telegram Forum 👋",
        entity=forum_entity,
        date=naive_date,
        message=SimpleNamespace(date=naive_date),
    )

    candidate = build_dialog_candidate(dialog)

    assert candidate == DialogCandidate(
        title="Telegram Forum 👋",
        entity_type="forum",
        peer_id=-100101,
        username="forumchat",
        participants_count=42,
        last_message_date=datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc),
    )
    assert DialogCandidate.from_dict(candidate.to_dict()) == candidate
    assert classify_dialog_entity(forum_entity) == "forum"


def test_rank_dialog_candidates_prefers_raw_then_normalized_then_substring() -> None:
    raw = DialogCandidate(
        title="Группа 👋",
        entity_type="group",
        peer_id=1,
        participants_count=10,
    )
    normalized = DialogCandidate(
        title="группа 👋",
        entity_type="group",
        peer_id=2,
        participants_count=20,
    )
    substring = DialogCandidate(
        title="Моя группа 👋 в Telegram",
        entity_type="group",
        peer_id=3,
        participants_count=30,
    )

    ranked = rank_dialog_candidates("Группа 👋", [substring, normalized, raw])

    assert ranked == [raw, normalized, substring]
