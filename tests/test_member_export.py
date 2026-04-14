from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from app.avatar_store import AvatarStore, safe_filename
from app.member_export import ExportSummary, export_members


@dataclass(slots=True)
class FakeBirthday:
    day: int
    month: int
    year: int | None = None


@dataclass(slots=True)
class FakeParticipant:
    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    photo: object | None = None


@dataclass(slots=True)
class FakeFullUser:
    about: str | None
    birthday: object | None
    personal_channel_id: int | None


@dataclass(slots=True)
class FakeChannel:
    username: str | None


class RetryFloodError(RuntimeError):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"Flood wait {seconds}")
        self.seconds = seconds


class FakeGateway:
    def __init__(self) -> None:
        self.me = FakeParticipant(id=1, first_name="Current", username="current")
        self.participants = [
            self.me,
            FakeParticipant(id=2, first_name="Alice", last_name="Example", username="alice", photo=object()),
            FakeParticipant(id=2, first_name="Alice", last_name="Example", username="alice", photo=object()),
            FakeParticipant(id=3, first_name="Bob", last_name=None, username=None, photo=None),
            FakeParticipant(id=4, first_name="Retry", last_name="User", username="retry", photo=object()),
        ]
        self.full_users = {
            2: FakeFullUser(about="About Alice", birthday=FakeBirthday(1, 2, 2000), personal_channel_id=77),
            3: FakeFullUser(about="", birthday=None, personal_channel_id=88),
        }
        self.channels = {
            77: FakeChannel(username="alice-channel"),
            88: FakeChannel(username=None),
        }
        self.download_calls = 0
        self.full_user_calls = 0
        self.retry_failures = 0

    async def get_me(self) -> FakeParticipant:
        return self.me

    def iter_participants(self, chat: object):
        async def generator():
            for participant in self.participants:
                yield participant

        return generator()

    async def get_full_user(self, user: FakeParticipant) -> FakeFullUser:
        self.full_user_calls += 1
        if user.id == 4:
            self.retry_failures += 1
            if self.retry_failures <= 4:
                raise RetryFloodError(seconds=30)
            raise RuntimeError("full user failed permanently")
        return self.full_users[user.id]

    async def get_entity(self, peer: object) -> FakeChannel:
        return self.channels[int(peer)]

    async def download_profile_photo(self, entity: object, file: Path) -> str | None:
        self.download_calls += 1
        if getattr(entity, "id", None) == 4:
            raise RetryFloodError(seconds=30)
        file.write_bytes(b"avatar-bytes")
        return str(file)


def test_safe_filename_strips_unicode_and_separators() -> None:
    assert safe_filename(42, "Мария 👋", "I/O") == "42_i_o"


def test_export_members_is_best_effort_and_summary_rich(tmp_path: Path) -> None:
    gateway = FakeGateway()
    sleep_calls: list[float] = []

    async def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    summary: ExportSummary = asyncio.run(
        export_members(
            gateway,
            chat=object(),
            runtime_dir=tmp_path / ".runtime",
            run_id="run-001",
            sleep=sleep,
            jitter=lambda _: 0.0,
            chat_label="Test Group",
        )
    )

    assert summary.chat_label == "Test Group"
    assert summary.current_user_id == 1
    assert summary.total_seen == 5
    assert summary.exported_count == 3
    assert summary.skipped_current_account == 1
    assert summary.deduplicated_count == 1
    assert summary.failed_user_ids == (4,)
    assert summary.csv_path.exists()
    assert summary.avatars_dir == tmp_path / ".runtime" / "exports" / "run-001" / "avatars"
    assert summary.warnings
    assert len(sleep_calls) == 8
    assert max(sleep_calls) == 4.0

    rows = {row.user_id: row for row in summary.rows}
    assert rows[2].about.status == "value"
    assert rows[2].birthday.status == "value"
    assert rows[2].linked_channel_url.status == "value"
    assert rows[2].linked_channel_url.value == "https://t.me/alice-channel"
    assert rows[2].photo_path.status == "value"
    assert rows[2].photo_path.value.endswith("2_alice_example_alice.jpg")

    assert rows[4].about.status == "error"
    assert rows[4].birthday.status == "error"
    assert rows[4].linked_channel_url.status == "error"
    assert rows[4].photo_path.status == "error"

    assert rows[3].about.status == "empty"
    assert rows[3].birthday.status == "empty"
    assert rows[3].linked_channel_url.status == "empty"
    assert rows[3].photo_path.status == "empty"

    csv_bytes = summary.csv_path.read_bytes()
    assert csv_bytes.startswith(b"\xef\xbb\xbf")


def test_export_members_stops_after_four_waits_for_one_operation(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.participants = [gateway.me, FakeParticipant(id=4, first_name="Retry", last_name="User", username="retry", photo=None)]

    sleep_calls: list[float] = []

    async def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    summary = asyncio.run(
        export_members(
            gateway,
            chat=object(),
            runtime_dir=tmp_path / ".runtime",
            run_id="run-002",
            sleep=sleep,
            jitter=lambda _: 0.0,
        )
    )

    assert summary.exported_count == 1
    assert summary.failed_user_ids == (4,)
    assert len(sleep_calls) == 4
    assert summary.rows[0].about.status == "error"
    assert summary.rows[0].photo_path.status == "empty"
